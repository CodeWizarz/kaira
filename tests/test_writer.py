from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

import pyarrow as pa
import pyarrow.dataset as ds

from kaira.schemas import OPTION_QUOTES_SCHEMA
from kaira.storage import ParquetDatasetWriter


def test_parquet_dataset_writer_writes_partitioned_dataset() -> None:
    ts = datetime(2026, 2, 4, 10, 0, 0, tzinfo=timezone.utc)
    rows = [
        {
            "ts": ts,
            "ts_date": date(2026, 2, 4),
            "symbol": "NIFTY",
            "expiry": date(2026, 2, 5),
            "strike": 18000,
            "right": "C",
            "instrument_id": 1,
            "option_id": "NIFTY|2026-02-05|18000|C",
            "underlying": 22000.0,
            "last": 10.0,
            "bid": 9.5,
            "ask": 10.5,
            "bid_qty": 100,
            "ask_qty": 120,
            "iv": 0.2,
            "oi": 10,
            "volume": 5,
            "source": "test",
            "ingest_ts": ts,
        }
    ]
    table = pa.Table.from_pylist(rows).cast(OPTION_QUOTES_SCHEMA, safe=False)

    with TemporaryDirectory() as td:
        root = Path(td) / "option_quotes"
        w = ParquetDatasetWriter(root_dir=root, schema=OPTION_QUOTES_SCHEMA, partition_cols=("symbol", "expiry", "ts_date"))
        w.append(table)
        w.flush()

        files = list(root.rglob("*.parquet"))
        assert files, "Expected parquet files to be written"

        dataset = ds.dataset(str(root), format="parquet", partitioning="hive")
        roundtrip = dataset.to_table()
        assert roundtrip.num_rows == 1

