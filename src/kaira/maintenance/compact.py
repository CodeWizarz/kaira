from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from uuid import uuid4

import duckdb

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class CompactConfig:
    min_files: int = 5
    delete_old: bool = False
    dry_run: bool = False
    compression: str = "zstd"


def _parse_partition_value(part: str, key: str) -> str | None:
    prefix = f"{key}="
    if not part.startswith(prefix):
        return None
    return part[len(prefix) :]


def _iter_partitions(dataset_dir: Path):
    root = Path(dataset_dir)
    for path in root.glob("symbol=*/expiry=*/ts_date=*"):
        if not path.is_dir():
            continue
        parts = path.parts[-3:]
        symbol = _parse_partition_value(parts[0], "symbol")
        expiry = _parse_partition_value(parts[1], "expiry")
        ts_date = _parse_partition_value(parts[2], "ts_date")
        if not symbol or not expiry or not ts_date:
            continue
        yield path, symbol, expiry, ts_date


def compact_option_quotes(
    dataset_dir: Path,
    *,
    symbol: str | None = None,
    expiry: date | None = None,
    trade_date_start: date | None = None,
    trade_date_end: date | None = None,
    cfg: CompactConfig | None = None,
) -> None:
    """
    Compact small files within each (symbol, expiry, ts_date) partition.

    Uses DuckDB to read + sort + de-dup out-of-core.
    """
    cfg = cfg or CompactConfig()
    dataset_dir = Path(dataset_dir)
    sym_filter = symbol.upper() if symbol else None
    expiry_filter = expiry.isoformat() if expiry else None

    con = duckdb.connect(database=":memory:", read_only=False)
    try:
        for part_dir, sym, exp, ts_date in _iter_partitions(dataset_dir):
            if sym_filter and sym != sym_filter:
                continue
            if expiry_filter and exp != expiry_filter:
                continue

            part_date = date.fromisoformat(ts_date)
            if trade_date_start and part_date < trade_date_start:
                continue
            if trade_date_end and part_date > trade_date_end:
                continue

            inputs = sorted(part_dir.glob("part-*.parquet"))
            if len(inputs) < cfg.min_files:
                continue

            out_path = part_dir / f"compact-{uuid4().hex}.parquet"
            input_glob = (part_dir / "part-*.parquet").as_posix()

            log.info("Compacting %s (%d files) -> %s", part_dir, len(inputs), out_path.name)
            if cfg.dry_run:
                continue

            sql = f"""
                COPY (
                    WITH ranked AS (
                        SELECT *,
                               row_number() OVER (
                                   PARTITION BY ts, symbol, expiry, strike, right
                                   ORDER BY ingest_ts DESC
                               ) AS rn
                        FROM parquet_scan('{input_glob}', hive_partitioning=1)
                    )
                    SELECT * EXCLUDE (rn)
                    FROM ranked
                    WHERE rn = 1
                    ORDER BY ts, strike, right
                )
                TO '{out_path.as_posix()}'
                (FORMAT PARQUET, COMPRESSION '{cfg.compression}');
            """
            con.execute(sql)

            if cfg.delete_old:
                for p in inputs:
                    p.unlink(missing_ok=True)
    finally:
        con.close()
