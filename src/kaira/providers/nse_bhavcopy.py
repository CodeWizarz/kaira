from __future__ import annotations

import asyncio
import io
import logging
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

import httpx
import pyarrow as pa
import pyarrow.csv as pacsv
import pyarrow.compute as pc

from kaira.schemas import OPTION_QUOTES_SCHEMA
from kaira.storage import ParquetDatasetWriter
from kaira.utils.ids import instrument_id, option_id
from kaira.utils.time import utc_now
from kaira.validation.option_quotes import OptionQuoteValidationPolicy, validate_option_quotes

log = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))


def _daterange(start: date, end: date) -> Iterable[date]:
    d = start
    while d <= end:
        yield d
        d = d + timedelta(days=1)


def _is_weekend(d: date) -> bool:
    return d.weekday() >= 5


def nse_fo_bhavcopy_url(d: date) -> str:
    # Example:
    # https://archives.nseindia.com/content/historical/DERIVATIVES/2024/JAN/fo01JAN2024bhav.csv.zip
    day = f"{d.day:02d}"
    mon = d.strftime("%b").upper()
    year = f"{d.year:04d}"
    return f"https://archives.nseindia.com/content/historical/DERIVATIVES/{year}/{mon}/fo{day}{mon}{year}bhav.csv.zip"


def _parse_expiry(s: str) -> date:
    # "30-Jan-2026" (sometimes)
    return datetime.strptime(s, "%d-%b-%Y").date()


def _bhavcopy_to_option_quotes(
    fo_table: pa.Table,
    *,
    trade_date: date,
    symbols: set[str],
    ingest_ts: datetime,
    source: str = "nse_fo_bhavcopy",
) -> pa.Table:
    """
    Convert FO bhavcopy rows to canonical option-quotes schema.

    Bhavcopy is EOD; bid/ask/iv are not available and are stored as nulls.
    """
    required_cols = {"INSTRUMENT", "SYMBOL", "EXPIRY_DT", "STRIKE_PR", "OPTION_TYP", "OPEN_INT", "TRD_QTY", "CLOSE"}
    missing = sorted(required_cols - set(fo_table.schema.names))
    if missing:
        raise ValueError(f"Bhavcopy missing columns: {missing}")

    mask = pc.equal(fo_table["INSTRUMENT"], "OPTIDX")
    mask = pc.and_(mask, pc.is_in(fo_table["SYMBOL"], value_set=pa.array(sorted(symbols))))
    fo_table = fo_table.filter(mask)

    sym = fo_table["SYMBOL"].to_pylist()
    exp = fo_table["EXPIRY_DT"].to_pylist()
    strike = fo_table["STRIKE_PR"].to_pylist()
    opt = fo_table["OPTION_TYP"].to_pylist()
    oi = fo_table["OPEN_INT"].to_pylist()
    vol = fo_table["TRD_QTY"].to_pylist()
    close = fo_table["CLOSE"].to_pylist()

    ts_ist = datetime(trade_date.year, trade_date.month, trade_date.day, 15, 30, 0, tzinfo=IST)
    ts = ts_ist.astimezone(timezone.utc)
    ts_date = trade_date

    rows: dict[str, list] = {name: [] for name in OPTION_QUOTES_SCHEMA.names}

    for i in range(len(sym)):
        s = str(sym[i]).upper()
        expiry = _parse_expiry(str(exp[i]))
        try:
            k = int(round(float(strike[i])))
        except Exception:
            continue
        right = str(opt[i]).upper()
        right = "C" if right in {"CE", "C"} else "P" if right in {"PE", "P"} else None
        if right is None:
            continue

        oid = option_id(symbol=s, expiry=expiry, strike=k, right=right)
        iid = instrument_id(symbol=s, expiry=expiry, strike=k, right=right)

        rows["ts"].append(ts)
        rows["ts_date"].append(ts_date)
        rows["symbol"].append(s)
        rows["expiry"].append(expiry)
        rows["strike"].append(k)
        rows["right"].append(right)
        rows["instrument_id"].append(iid)
        rows["option_id"].append(oid)
        rows["underlying"].append(None)
        rows["last"].append(float(close[i]) if close[i] is not None else None)
        rows["bid"].append(None)
        rows["ask"].append(None)
        rows["bid_qty"].append(None)
        rows["ask_qty"].append(None)
        rows["iv"].append(None)
        rows["oi"].append(int(oi[i]) if oi[i] is not None else None)
        rows["volume"].append(int(vol[i]) if vol[i] is not None else None)
        rows["source"].append(source)
        rows["ingest_ts"].append(ingest_ts)

    arrays = [pa.array(rows[f.name], type=f.type) for f in OPTION_QUOTES_SCHEMA]
    return pa.Table.from_arrays(arrays, schema=OPTION_QUOTES_SCHEMA)


@dataclass(frozen=True)
class NSEBhavcopyDownloader:
    timeout_s: float = 30.0
    concurrency: int = 8

    async def download_zip(self, client: httpx.AsyncClient, d: date) -> bytes | None:
        url = nse_fo_bhavcopy_url(d)
        headers = {
            "accept": "application/zip,application/octet-stream,*/*",
            "user-agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/121.0.0.0 Safari/537.36"
            ),
        }
        resp = await client.get(url, headers=headers)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.content


def _read_zipped_csv(zip_bytes: bytes) -> pa.Table:
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
        if not names:
            raise ValueError("No CSV found in zip")
        with zf.open(names[0]) as f:
            csv_bytes = f.read()
    return pacsv.read_csv(pa.py_buffer(csv_bytes))


async def _backfill_bhavcopy_async(
    *,
    start: date,
    end: date,
    symbols: list[str],
    out_dir: Path,
) -> None:
    out_dir = Path(out_dir)
    symbols_set = {s.upper() for s in symbols}
    downloader = NSEBhavcopyDownloader()
    writer = ParquetDatasetWriter(
        root_dir=out_dir,
        schema=OPTION_QUOTES_SCHEMA,
        partition_cols=("symbol", "expiry", "ts_date"),
    )

    policy = OptionQuoteValidationPolicy(require_bid_ask=False, require_iv=False)

    sem = asyncio.Semaphore(downloader.concurrency)
    queue: asyncio.Queue[pa.Table | None] = asyncio.Queue(maxsize=16)
    timeout = httpx.Timeout(downloader.timeout_s)

    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        async def _producer(d: date) -> None:
            if _is_weekend(d):
                return
            async with sem:
                try:
                    log.info("event=starting_download source=nse_fo_bhavcopy trade_date=%s", d)
                    zip_bytes = await downloader.download_zip(client, d)
                    if zip_bytes is None:
                        log.info("Bhavcopy not found (holiday?): %s", d)
                        return
                    log.info(
                        "event=completed_download source=nse_fo_bhavcopy trade_date=%s bytes=%d",
                        d,
                        len(zip_bytes),
                    )
                    fo = await asyncio.to_thread(_read_zipped_csv, zip_bytes)
                    table = await asyncio.to_thread(
                        _bhavcopy_to_option_quotes,
                        fo,
                        trade_date=d,
                        symbols=symbols_set,
                        ingest_ts=utc_now(),
                    )
                    log.info(
                        "event=rows_parsed source=nse_fo_bhavcopy trade_date=%s rows=%d",
                        d,
                        table.num_rows,
                    )
                    await queue.put(table)
                except httpx.HTTPError as e:
                    log.error(
                        "event=network_failure source=nse_fo_bhavcopy trade_date=%s error=%s",
                        d,
                        str(e),
                    )
                except Exception:
                    log.error(
                        "event=parsing_failure source=nse_fo_bhavcopy trade_date=%s error=failed_processing",
                        d,
                        exc_info=True,
                    )

        async def _consumer() -> None:
            while True:
                item = await queue.get()
                if item is None:
                    return
                valid, invalid = validate_option_quotes(item, policy=policy)
                if invalid.num_rows:
                    trade_date = None
                    if item.num_rows:
                        try:
                            trade_date = item.column("ts_date")[0].as_py()
                        except Exception:
                            trade_date = None
                    log.warning(
                        "event=schema_mismatch source=nse_fo_bhavcopy trade_date=%s invalid_rows=%d",
                        trade_date,
                        invalid.num_rows,
                    )
                    log.warning(
                        "event=dropped_rows source=nse_fo_bhavcopy trade_date=%s dropped_rows=%d",
                        trade_date,
                        invalid.num_rows,
                    )
                writer.append(valid)
                log.info(
                    "event=rows_written dataset=option_quotes source=nse_fo_bhavcopy trade_date=%s rows=%d",
                    valid.column("ts_date")[0].as_py() if valid.num_rows else None,
                    valid.num_rows,
                )
                if writer.buffered_rows >= 2_000_000:
                    writer.flush()

        dates = [d for d in _daterange(start, end)]
        consumer_task = asyncio.create_task(_consumer())
        async with asyncio.TaskGroup() as tg:
            for d in dates:
                tg.create_task(_producer(d))

        await queue.put(None)
        await consumer_task

    writer.flush()


def backfill_nse_fo_bhavcopy(*, start: date, end: date, symbols: list[str], out_dir: Path) -> None:
    """
    Backfill EOD NSE FO bhavcopy (free, official) into the canonical Parquet dataset.

    Limitations:
      - bhavcopy is EOD only
      - no bid/ask, no IV (those remain null)
    """
    asyncio.run(_backfill_bhavcopy_async(start=start, end=end, symbols=symbols, out_dir=out_dir))
