#!/usr/bin/env python3
from __future__ import annotations

import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable

import httpx

from kaira.providers.nse_bhavcopy import (
    _bhavcopy_to_option_quotes,
    _read_zipped_csv,
    nse_fo_bhavcopy_url,
)
from kaira.query.duckdb_reader import OptionQuoteQuery, read_option_quotes_arrow
from kaira.schemas import OPTION_QUOTES_SCHEMA
from kaira.storage import ParquetDatasetWriter
from kaira.utils.time import utc_now


MAX_RUNTIME_S = 30.0
DOWNLOAD_TIMEOUT_S = 8.0
MAX_LOOKBACK_DAYS = 10
SYMBOLS = ["NIFTY"]


@dataclass
class StepResult:
    name: str
    started: float

    def done(self) -> None:
        elapsed = time.monotonic() - self.started
        print(f"[ok] {self.name} ({elapsed:.2f}s)")


def _guard(deadline: float, context: str) -> None:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise RuntimeError(f"Timeout reached while {context} (>{MAX_RUNTIME_S:.0f}s)")


def _run_step(name: str, fn, *, deadline: float):
    print(f"[run] {name}...")
    step = StepResult(name=name, started=time.monotonic())
    try:
        _guard(deadline, f"starting {name}")
        result = fn()
    except Exception as exc:
        elapsed = time.monotonic() - step.started
        print(f"[fail] {name} after {elapsed:.2f}s")
        raise RuntimeError(f"Step '{name}' failed: {exc}") from exc
    step.done()
    return result


def _iter_candidate_dates(today: date) -> Iterable[date]:
    d = today - timedelta(days=1)
    checked = 0
    while checked < MAX_LOOKBACK_DAYS:
        if d.weekday() < 5:
            yield d
            checked += 1
        d -= timedelta(days=1)


def preflight(deadline: float) -> None:
    def _inner() -> None:
        _guard(deadline, "preflight")
        missing = []
        for module in ("pyarrow", "duckdb", "httpx"):
            try:
                __import__(module)
            except Exception:
                missing.append(module)
        if missing:
            raise RuntimeError(f"Missing dependencies: {', '.join(missing)}")
        _ = OPTION_QUOTES_SCHEMA
        if not SYMBOLS:
            raise RuntimeError("No symbols configured for download")

    _run_step("preflight check", _inner, deadline=deadline)


def download_one_day(deadline: float) -> tuple[date, bytes]:
    def _inner() -> tuple[date, bytes]:
        _guard(deadline, "download")
        headers = {
            "accept": "application/zip,application/octet-stream,*/*",
            "user-agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/121.0.0.0 Safari/537.36"
            ),
        }
        attempts: list[str] = []
        with httpx.Client(timeout=DOWNLOAD_TIMEOUT_S, follow_redirects=True) as client:
            for d in _iter_candidate_dates(date.today()):
                _guard(deadline, f"downloading {d}")
                url = nse_fo_bhavcopy_url(d)
                try:
                    resp = client.get(url, headers=headers)
                except Exception as exc:
                    attempts.append(f"{d}: request failed ({exc})")
                    continue
                if resp.status_code == 404:
                    attempts.append(f"{d}: 404 not found")
                    continue
                if resp.status_code >= 400:
                    attempts.append(f"{d}: HTTP {resp.status_code}")
                    continue
                return d, resp.content
        details = "\n".join(attempts) if attempts else "no attempts made"
        raise RuntimeError(f"No bhavcopy found in last {MAX_LOOKBACK_DAYS} trading days.\n{details}")

    return _run_step("download one trading day", _inner, deadline=deadline)


def process_bhavcopy(deadline: float, trade_date: date, zip_bytes: bytes):
    def _inner():
        _guard(deadline, "processing")
        table = _read_zipped_csv(zip_bytes)
        processed = _bhavcopy_to_option_quotes(
            table,
            trade_date=trade_date,
            symbols=set(SYMBOLS),
            ingest_ts=utc_now(),
        )
        if processed.num_rows == 0:
            raise RuntimeError("Processed table is empty after filtering symbols")
        return processed

    return _run_step("process bhavcopy", _inner, deadline=deadline)


def write_parquet(deadline: float, table) -> Path:
    def _inner() -> Path:
        _guard(deadline, "writing parquet")
        tmp_dir = Path(tempfile.mkdtemp(prefix="kaira-smoke-"))
        writer = ParquetDatasetWriter(
            root_dir=tmp_dir,
            schema=OPTION_QUOTES_SCHEMA,
            partition_cols=("symbol", "expiry", "ts_date"),
        )
        writer.append(table)
        writer.flush()
        return tmp_dir

    return _run_step("write parquet", _inner, deadline=deadline)


def read_with_duckdb(deadline: float, dataset_dir: Path, trade_date: date) -> int:
    def _inner() -> int:
        _guard(deadline, "duckdb read")
        table = read_option_quotes_arrow(
            dataset_dir,
            query=OptionQuoteQuery(trade_date_start=trade_date, trade_date_end=trade_date),
            columns=["symbol", "ts_date"],
        )
        return table.num_rows

    return _run_step("read parquet with DuckDB", _inner, deadline=deadline)


def main() -> int:
    start = time.monotonic()
    deadline = start + MAX_RUNTIME_S

    try:
        preflight(deadline)
        trade_date, zip_bytes = download_one_day(deadline)
        processed = process_bhavcopy(deadline, trade_date, zip_bytes)
        dataset_dir = write_parquet(deadline, processed)
        row_count = read_with_duckdb(deadline, dataset_dir, trade_date)
    except Exception as exc:
        elapsed = time.monotonic() - start
        print(f"[error] Smoke test failed after {elapsed:.2f}s: {exc}")
        return 1

    elapsed = time.monotonic() - start
    print(f"[done] Row count for {trade_date}: {row_count}")
    print(f"[done] Completed in {elapsed:.2f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
