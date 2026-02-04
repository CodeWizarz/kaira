from __future__ import annotations

import asyncio
import pprint
import tempfile
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Iterable

import httpx
import pyarrow as pa

from kaira.providers import nse_bhavcopy
from kaira.schemas import OPTION_QUOTES_SCHEMA
from kaira.storage import ParquetDatasetWriter
from kaira.utils.time import utc_now
from kaira.validation.option_quotes import OptionQuoteValidationPolicy, validate_option_quotes


@dataclass(frozen=True)
class PreflightResult:
    ok: bool
    messages: list[str]


def _daterange(start: date, days: int) -> Iterable[date]:
    for offset in range(days):
        yield start - timedelta(days=offset)


def _format_columns(columns: list[str]) -> str:
    return ", ".join(columns)


async def _download_latest_bhavcopy(
    *,
    today: date,
    lookback_days: int,
    timeout_s: float,
) -> tuple[date, bytes]:
    downloader = nse_bhavcopy.NSEBhavcopyDownloader(timeout_s=timeout_s)
    timeout = httpx.Timeout(timeout_s)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        for d in _daterange(today, lookback_days):
            if d.weekday() >= 5:
                continue
            zip_bytes = await downloader.download_zip(client, d)
            if zip_bytes is None:
                continue
            return d, zip_bytes
    raise RuntimeError(
        "No bhavcopy file found in lookback window. "
        "Check NSE availability or increase --lookback-days."
    )


def _print_sample_rows(table: pa.Table, *, max_rows: int) -> None:
    sample = table.slice(0, max_rows)
    data = sample.to_pydict()
    keys = list(data.keys())
    rows = [dict(zip(keys, values)) for values in zip(*data.values(), strict=False)]
    pprint.pprint(rows, width=120)


def run_data_preflight(
    *,
    symbols: list[str],
    lookback_days: int = 10,
    sample_rows: int = 5,
    timeout_s: float = 30.0,
) -> PreflightResult:
    messages: list[str] = []
    try:
        trade_date, zip_bytes = asyncio.run(
            _download_latest_bhavcopy(
                today=date.today(),
                lookback_days=lookback_days,
                timeout_s=timeout_s,
            )
        )
        messages.append(f"Downloaded bhavcopy for {trade_date}.")
    except Exception as exc:
        return PreflightResult(
            ok=False,
            messages=[f"Download failed: {exc}", "Ensure network access and NSE archives availability."],
        )

    try:
        fo_table = nse_bhavcopy._read_zipped_csv(zip_bytes)
    except Exception as exc:
        return PreflightResult(
            ok=False,
            messages=[f"Failed reading bhavcopy zip: {exc}", "Verify the ZIP contains a CSV file."],
        )

    messages.append(f"Detected columns: {_format_columns(fo_table.schema.names)}")

    symbols_set = {s.upper() for s in symbols}
    try:
        option_table = nse_bhavcopy._bhavcopy_to_option_quotes(
            fo_table,
            trade_date=trade_date,
            symbols=symbols_set,
            ingest_ts=utc_now(),
        )
    except Exception as exc:
        return PreflightResult(
            ok=False,
            messages=[f"Schema conversion failed: {exc}", "Confirm bhavcopy schema and symbol filters."],
        )

    if option_table.num_rows == 0:
        return PreflightResult(
            ok=False,
            messages=[
                "No rows after conversion.",
                "Try updating --symbols to match available bhavcopy symbols.",
            ],
        )

    if not option_table.schema.equals(OPTION_QUOTES_SCHEMA, check_metadata=False):
        return PreflightResult(
            ok=False,
            messages=[
                "Converted schema does not match OPTION_QUOTES_SCHEMA.",
                "Inspect schema alignment logic in kaira.schemas.align_table_to_schema.",
            ],
        )

    policy = OptionQuoteValidationPolicy(require_bid_ask=False, require_iv=False)
    try:
        valid, invalid = validate_option_quotes(option_table, policy=policy)
    except Exception as exc:
        return PreflightResult(
            ok=False,
            messages=[f"Validation failed: {exc}", "Check required columns and data types."],
        )

    messages.append(f"Validation rows: {valid.num_rows} valid, {invalid.num_rows} invalid.")
    if invalid.num_rows:
        return PreflightResult(
            ok=False,
            messages=[
                "Validation produced invalid rows.",
                "Inspect data quality flags or update validation policy for expected nulls.",
            ],
        )

    messages.append("Sample rows (converted):")
    _print_sample_rows(valid, max_rows=sample_rows)

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            writer = ParquetDatasetWriter(
                root_dir=Path(tmpdir),
                schema=OPTION_QUOTES_SCHEMA,
                partition_cols=("symbol", "expiry", "ts_date"),
            )
            writer.append(valid)
            written = writer.flush()
            if written <= 0:
                raise RuntimeError("No rows written to Parquet dataset.")
            parquet_files = list(Path(tmpdir).rglob("*.parquet"))
            if not parquet_files:
                raise RuntimeError("Parquet dataset write succeeded but no files found.")
    except Exception as exc:
        return PreflightResult(
            ok=False,
            messages=[f"Parquet writer failed: {exc}", "Verify pyarrow dataset writer configuration."],
        )

    messages.append("Parquet writer check: OK")
    return PreflightResult(ok=True, messages=messages)
