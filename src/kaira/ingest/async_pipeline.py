from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

import httpx
import pyarrow as pa

from kaira.providers.nse_option_chain import NSEOptionChainClient, option_quotes_from_nse_option_chain
from kaira.schemas import OPTION_QUOTES_INVALID_SCHEMA, OPTION_QUOTES_SCHEMA, SNAPSHOT_LOG_SCHEMA
from kaira.storage import ParquetDatasetWriter
from kaira.utils.time import ist_trade_date, utc_now
from kaira.validation.option_quotes import OptionQuoteValidationPolicy, validate_option_quotes

log = logging.getLogger(__name__)


try:
    import orjson  # type: ignore

    def _dumps(obj: Any) -> bytes:
        return orjson.dumps(obj)

except Exception:  # pragma: no cover

    def _dumps(obj: Any) -> bytes:
        return json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


Status = Literal["ok", "error"]


@dataclass(frozen=True)
class SnapshotEvent:
    symbol: str
    source: str
    status: Status
    ingest_ts: datetime
    latency_ms: int
    http_status: int | None = None
    payload: dict[str, Any] | None = None
    error: str | None = None


def _snapshot_log_row(ev: SnapshotEvent, *, records: int | None) -> pa.Table:
    ts_date = ist_trade_date(ev.ingest_ts)
    row = {
        "ts": [ev.ingest_ts],
        "ts_date": [ts_date],
        "symbol": [ev.symbol],
        "source": [ev.source],
        "status": [ev.status],
        "http_status": [ev.http_status],
        "latency_ms": [ev.latency_ms],
        "records": [records],
        "error": [ev.error],
        "ingest_ts": [ev.ingest_ts],
    }
    arrays = [pa.array(row[f.name], type=f.type) for f in SNAPSHOT_LOG_SCHEMA]
    return pa.Table.from_arrays(arrays, schema=SNAPSHOT_LOG_SCHEMA)


def _quarantine_payload(quarantine_dir: Path, *, ev: SnapshotEvent) -> None:
    if not ev.payload:
        return
    quarantine_dir = Path(quarantine_dir)
    quarantine_dir.mkdir(parents=True, exist_ok=True)
    ts = ev.ingest_ts.strftime("%Y%m%dT%H%M%S.%fZ")
    path = quarantine_dir / f"{ev.source}-{ev.symbol}-{ts}-{uuid4().hex}.json"
    try:
        path.write_bytes(_dumps(ev.payload))
    except Exception:
        log.exception("Failed writing quarantined payload: %s", path)


async def _collect_nse_live_async(
    *,
    symbols: list[str],
    interval_s: float,
    duration_s: int | None,
    out_dir: Path,
    max_buffer_rows: int,
    quarantine_dir: Path,
) -> None:
    out_dir = Path(out_dir)
    quarantine_dir = Path(quarantine_dir)

    option_writer = ParquetDatasetWriter(
        root_dir=out_dir,
        schema=OPTION_QUOTES_SCHEMA,
        partition_cols=("symbol", "expiry", "ts_date"),
    )
    invalid_writer = ParquetDatasetWriter(
        root_dir=quarantine_dir / "option_quotes_invalid",
        schema=OPTION_QUOTES_INVALID_SCHEMA,
        partition_cols=("symbol", "expiry", "ts_date"),
        sort_keys=("ts", "strike", "right"),
    )
    snapshot_writer = ParquetDatasetWriter(
        root_dir=out_dir.parent / "snapshot_log",
        schema=SNAPSHOT_LOG_SCHEMA,
        partition_cols=("source", "ts_date"),
        sort_keys=("ts", "symbol"),
    )

    policy = OptionQuoteValidationPolicy(require_bid_ask=True, require_iv=True)

    queue: asyncio.Queue[SnapshotEvent | None] = asyncio.Queue(maxsize=256)
    stop = asyncio.Event()

    async def _fetch_loop(symbol: str) -> None:
        sym = symbol.upper()
        source = "nse_option_chain"
        while not stop.is_set():
            start = time.perf_counter()
            ingest_ts = utc_now()
            try:
                log.info("event=starting_download source=%s symbol=%s", source, sym)
                payload = await client.fetch(sym)
                latency_ms = int((time.perf_counter() - start) * 1000)
                log.info(
                    "event=completed_download source=%s symbol=%s status=ok latency_ms=%d",
                    source,
                    sym,
                    latency_ms,
                )
                await queue.put(
                    SnapshotEvent(
                        symbol=sym,
                        source=source,
                        status="ok",
                        ingest_ts=ingest_ts,
                        latency_ms=latency_ms,
                        payload=payload,
                    )
                )
            except httpx.HTTPStatusError as e:
                latency_ms = int((time.perf_counter() - start) * 1000)
                log.error(
                    "event=network_failure source=%s symbol=%s status=error http_status=%s latency_ms=%d error=%s",
                    source,
                    sym,
                    e.response.status_code,
                    latency_ms,
                    str(e),
                )
                await queue.put(
                    SnapshotEvent(
                        symbol=sym,
                        source=source,
                        status="error",
                        ingest_ts=ingest_ts,
                        latency_ms=latency_ms,
                        http_status=e.response.status_code,
                        error=str(e),
                    )
                )
            except Exception as e:
                latency_ms = int((time.perf_counter() - start) * 1000)
                log.error(
                    "event=network_failure source=%s symbol=%s status=error latency_ms=%d error=%s",
                    source,
                    sym,
                    latency_ms,
                    repr(e),
                )
                await queue.put(
                    SnapshotEvent(
                        symbol=sym,
                        source=source,
                        status="error",
                        ingest_ts=ingest_ts,
                        latency_ms=latency_ms,
                        error=repr(e),
                    )
                )

            # keep a roughly constant cadence
            elapsed = time.perf_counter() - start
            sleep_s = interval_s - elapsed
            if sleep_s > 0:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=sleep_s)
                except TimeoutError:
                    pass

    async def _consumer() -> None:
        while True:
            ev = await queue.get()
            if ev is None:
                return

            records = None
            if ev.status == "ok" and ev.payload is not None:
                try:
                    recs = (ev.payload.get("records") or {}).get("data") or []
                    if isinstance(recs, list):
                        records = len(recs)
                except Exception:
                    records = None

            snapshot_writer.append(_snapshot_log_row(ev, records=records))

            if ev.status != "ok" or ev.payload is None:
                continue

            try:
                table = option_quotes_from_nse_option_chain(ev.payload, symbol=ev.symbol, ingest_ts=ev.ingest_ts)
                log.info(
                    "event=rows_parsed source=%s symbol=%s rows=%d ingest_ts=%s",
                    ev.source,
                    ev.symbol,
                    table.num_rows,
                    ev.ingest_ts.isoformat(),
                )
                valid, invalid = validate_option_quotes(table, policy=policy)

                option_writer.append(valid)
                log.info(
                    "event=rows_written dataset=option_quotes source=%s symbol=%s rows=%d",
                    ev.source,
                    ev.symbol,
                    valid.num_rows,
                )
                if invalid.num_rows:
                    log.warning(
                        "event=schema_mismatch source=%s symbol=%s invalid_rows=%d",
                        ev.source,
                        ev.symbol,
                        invalid.num_rows,
                    )
                    log.warning(
                        "event=dropped_rows source=%s symbol=%s dropped_rows=%d",
                        ev.source,
                        ev.symbol,
                        invalid.num_rows,
                    )
                    invalid_writer.append(invalid)
                    _quarantine_payload(quarantine_dir / "payloads", ev=ev)

                if option_writer.buffered_rows >= max_buffer_rows:
                    option_writer.flush()
                if snapshot_writer.buffered_rows >= 25_000:
                    snapshot_writer.flush()
                if invalid_writer.buffered_rows >= 100_000:
                    invalid_writer.flush()
            except Exception:
                log.error(
                    "event=parsing_failure source=%s symbol=%s error=failed_parsing",
                    ev.source,
                    ev.symbol,
                    exc_info=True,
                )
                _quarantine_payload(quarantine_dir / "payloads", ev=ev)

    async with NSEOptionChainClient() as client:
        consumer_task = asyncio.create_task(_consumer())
        fetch_tasks = [asyncio.create_task(_fetch_loop(s)) for s in symbols]

        try:
            if duration_s is None:
                await asyncio.gather(*fetch_tasks)
            else:
                try:
                    await asyncio.sleep(duration_s)
                finally:
                    stop.set()
                    await asyncio.gather(*fetch_tasks, return_exceptions=True)
        finally:
            await queue.put(None)
            await consumer_task

    option_writer.flush()
    snapshot_writer.flush()
    invalid_writer.flush()


def collect_nse_live(
    *,
    symbols: list[str],
    interval_s: float,
    duration_s: int | None,
    out_dir: Path,
    max_buffer_rows: int,
    quarantine_dir: Path,
) -> None:
    """
    Collect NSE option-chain snapshots (async) and write a partitioned Parquet dataset.

    Output:
      - option quotes: `out_dir` (default: data/silver/option_quotes)
      - snapshot log: sibling `snapshot_log` dataset
      - quarantine: `quarantine_dir/option_quotes_invalid` and raw payloads
    """
    try:
        asyncio.run(
            _collect_nse_live_async(
                symbols=symbols,
                interval_s=interval_s,
                duration_s=duration_s,
                out_dir=out_dir,
                max_buffer_rows=max_buffer_rows,
                quarantine_dir=quarantine_dir,
            )
        )
    except KeyboardInterrupt:
        log.warning("Interrupted by user; flushing buffers")
