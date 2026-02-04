from __future__ import annotations

from datetime import datetime
from typing import Iterable

import pyarrow as pa


def option_quotes_schema() -> pa.Schema:
    # Canonical fact table:
    #   one row per (ts, symbol, expiry, strike, right)
    return pa.schema(
        [
            pa.field("ts", pa.timestamp("ms", tz="UTC"), nullable=False),
            pa.field("ts_date", pa.date32(), nullable=False),  # IST trade date for partitioning
            pa.field("symbol", pa.string(), nullable=False),
            pa.field("expiry", pa.date32(), nullable=False),
            pa.field("strike", pa.int32(), nullable=False),
            pa.field("right", pa.string(), nullable=False),  # "C" / "P"
            pa.field("instrument_id", pa.int64(), nullable=False),
            pa.field("option_id", pa.string(), nullable=False),  # stable string id for debug/joins
            pa.field("underlying", pa.float32()),
            pa.field("last", pa.float32()),
            pa.field("bid", pa.float32()),
            pa.field("ask", pa.float32()),
            pa.field("bid_qty", pa.int32()),
            pa.field("ask_qty", pa.int32()),
            pa.field("iv", pa.float32()),  # decimal (0.15 == 15%)
            pa.field("oi", pa.int64()),
            pa.field("volume", pa.int64()),
            pa.field("source", pa.string(), nullable=False),
            pa.field("ingest_ts", pa.timestamp("ms", tz="UTC"), nullable=False),
        ]
    )


OPTION_QUOTES_SCHEMA = option_quotes_schema()


OPTION_QUOTES_INVALID_SCHEMA = OPTION_QUOTES_SCHEMA.append(pa.field("dq_flags", pa.int32(), nullable=False))


SNAPSHOT_LOG_SCHEMA = pa.schema(
    [
        pa.field("ts", pa.timestamp("ms", tz="UTC"), nullable=False),
        pa.field("ts_date", pa.date32(), nullable=False),
        pa.field("symbol", pa.string(), nullable=False),
        pa.field("source", pa.string(), nullable=False),
        pa.field("status", pa.string(), nullable=False),  # "ok" / "error"
        pa.field("http_status", pa.int16()),
        pa.field("latency_ms", pa.int32()),
        pa.field("records", pa.int32()),
        pa.field("error", pa.string()),
        pa.field("ingest_ts", pa.timestamp("ms", tz="UTC"), nullable=False),
    ]
)


def _nulls(length: int, typ: pa.DataType) -> pa.Array:
    return pa.nulls(length, type=typ)


def align_table_to_schema(table: pa.Table, schema: pa.Schema) -> pa.Table:
    """
    Make `table` match `schema`:
      - missing columns are added as nulls
      - extra columns are dropped
      - columns are cast when safe=False (provider noise happens)
      - column order matches schema
    """
    length = table.num_rows
    columns: list[pa.Array] = []
    names = table.schema.names
    name_to_index = {name: i for i, name in enumerate(names)}

    for field in schema:
        if field.name not in name_to_index:
            columns.append(_nulls(length, field.type))
            continue

        col = table.column(name_to_index[field.name])
        if not col.type.equals(field.type):
            col = col.cast(field.type, safe=False)
        columns.append(col)

    return pa.Table.from_arrays(columns, schema=schema)


def ensure_utc_ms(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        raise ValueError("ts must be timezone-aware (UTC)")
    return ts


def ensure_all_present(values: Iterable[str], *, field: str) -> None:
    missing = [v for v in values if not v]
    if missing:
        raise ValueError(f"Missing {field} values: {missing[:5]}")
