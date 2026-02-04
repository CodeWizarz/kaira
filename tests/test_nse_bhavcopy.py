from __future__ import annotations

import asyncio
import csv
import io
import zipfile
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory

import pyarrow as pa
import pyarrow.csv as pacsv
import pyarrow.dataset as ds
from pytest import MonkeyPatch

from kaira.providers import nse_bhavcopy


def _build_csv_text(rows: list[dict[str, object]], headers: list[str]) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=headers)
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return buffer.getvalue()


def _zip_bytes(csv_text: str, *, name: str = "bhav.csv") -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(name, csv_text)
    return buffer.getvalue()


def _sample_row(volume_col: str = "TRD_QTY") -> dict[str, object]:
    return {
        "INSTRUMENT": "OPTIDX",
        "SYMBOL": "NIFTY",
        "EXPIRY_DT": "30-Jan-2026",
        "STRIKE_PR": "18000",
        "OPTION_TYP": "CE",
        "OPEN_INT": "100",
        volume_col: "250",
        "CLOSE": "100.5",
    }


def test_bhavcopy_schema_alias_trd_qty_normalizes() -> None:
    headers = ["INSTRUMENT", "SYMBOL", "EXPIRY_DT", "STRIKE_PR", "OPTION_TYP", "OPEN_INT", "TOTTRDQTY", "CLOSE"]
    csv_text = _build_csv_text([_sample_row("TOTTRDQTY")], headers)
    table = pacsv.read_csv(pa.py_buffer(csv_text.encode()))

    result = nse_bhavcopy._bhavcopy_to_option_quotes(
        table,
        trade_date=date(2024, 1, 2),
        symbols={"NIFTY"},
        ingest_ts=nse_bhavcopy.utc_now(),
    )

    assert result.num_rows == 1
    assert result.column("volume").to_pylist() == [250]


def test_bhavcopy_corrupted_zip_skips_safely(monkeypatch: MonkeyPatch) -> None:
    async def fake_download_zip(self: object, client: object, d: date) -> bytes | None:
        return b"not-a-zip-file"

    monkeypatch.setattr(nse_bhavcopy.NSEBhavcopyDownloader, "download_zip", fake_download_zip)

    with TemporaryDirectory() as td:
        out_dir = Path(td) / "bhavcopy"
        asyncio.run(
            nse_bhavcopy._backfill_bhavcopy_async(
                start=date(2024, 1, 2),
                end=date(2024, 1, 2),
                symbols=["NIFTY"],
                out_dir=out_dir,
            )
        )
        assert list(out_dir.rglob("*.parquet")) == []


def test_bhavcopy_empty_dataset_does_not_crash(monkeypatch: MonkeyPatch) -> None:
    headers = ["INSTRUMENT", "SYMBOL", "EXPIRY_DT", "STRIKE_PR", "OPTION_TYP", "OPEN_INT", "TRD_QTY", "CLOSE"]
    csv_text = _build_csv_text([], headers)
    zip_bytes = _zip_bytes(csv_text)

    async def fake_download_zip(self: object, client: object, d: date) -> bytes | None:
        return zip_bytes

    monkeypatch.setattr(nse_bhavcopy.NSEBhavcopyDownloader, "download_zip", fake_download_zip)

    with TemporaryDirectory() as td:
        out_dir = Path(td) / "bhavcopy"
        asyncio.run(
            nse_bhavcopy._backfill_bhavcopy_async(
                start=date(2024, 1, 2),
                end=date(2024, 1, 2),
                symbols=["NIFTY"],
                out_dir=out_dir,
            )
        )
        assert list(out_dir.rglob("*.parquet")) == []


def test_bhavcopy_parquet_write_read_roundtrip(monkeypatch: MonkeyPatch) -> None:
    headers = ["INSTRUMENT", "SYMBOL", "EXPIRY_DT", "STRIKE_PR", "OPTION_TYP", "OPEN_INT", "TRD_QTY", "CLOSE"]
    csv_text = _build_csv_text([_sample_row()], headers)
    zip_bytes = _zip_bytes(csv_text)

    async def fake_download_zip(self: object, client: object, d: date) -> bytes | None:
        return zip_bytes

    monkeypatch.setattr(nse_bhavcopy.NSEBhavcopyDownloader, "download_zip", fake_download_zip)

    with TemporaryDirectory() as td:
        out_dir = Path(td) / "bhavcopy"
        asyncio.run(
            nse_bhavcopy._backfill_bhavcopy_async(
                start=date(2024, 1, 2),
                end=date(2024, 1, 2),
                symbols=["NIFTY"],
                out_dir=out_dir,
            )
        )

        dataset = ds.dataset(str(out_dir), format="parquet", partitioning="hive")
        table = dataset.to_table()
        assert table.num_rows == 1
        assert table.column("symbol").to_pylist() == ["NIFTY"]
