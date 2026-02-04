from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Sequence

import duckdb
import pyarrow as pa


@dataclass(frozen=True)
class OptionQuoteQuery:
    symbol: str | None = None
    expiries: Sequence[date] | None = None
    rights: Sequence[str] | None = None
    strikes: tuple[int, int] | None = None
    ts_start: datetime | None = None
    ts_end: datetime | None = None
    trade_date_start: date | None = None
    trade_date_end: date | None = None


def read_option_quotes_arrow(
    dataset_dir: Path,
    *,
    query: OptionQuoteQuery | None = None,
    columns: Sequence[str] | None = None,
) -> pa.Table:
    """
    Fast backtest reads using DuckDB predicate pushdown + hive partition pruning.

    `dataset_dir` should be the Parquet dataset root, e.g. `data/silver/option_quotes`.
    """
    dataset_dir = Path(dataset_dir)
    query = query or OptionQuoteQuery()

    glob_path = str(dataset_dir / "**" / "*.parquet")
    cols_sql = ", ".join(columns) if columns else "*"

    where = ["1=1"]
    params: list[object] = [glob_path]

    if query.symbol:
        where.append("symbol = ?")
        params.append(query.symbol.upper())

    if query.expiries:
        where.append(f"expiry IN ({', '.join(['?'] * len(query.expiries))})")
        params.extend(list(query.expiries))

    if query.rights:
        rights = [r.upper() for r in query.rights]
        where.append(f"right IN ({', '.join(['?'] * len(rights))})")
        params.extend(rights)

    if query.strikes:
        lo, hi = query.strikes
        where.append("strike BETWEEN ? AND ?")
        params.extend([int(lo), int(hi)])

    if query.trade_date_start:
        where.append("ts_date >= ?")
        params.append(query.trade_date_start)

    if query.trade_date_end:
        where.append("ts_date <= ?")
        params.append(query.trade_date_end)

    if query.ts_start:
        where.append("ts >= ?")
        params.append(query.ts_start)

    if query.ts_end:
        where.append("ts <= ?")
        params.append(query.ts_end)

    sql = f"""
        SELECT {cols_sql}
        FROM parquet_scan(?, hive_partitioning=1)
        WHERE {' AND '.join(where)}
    """

    con = duckdb.connect(database=":memory:", read_only=False)
    try:
        rel = con.execute(sql, params).fetch_arrow_table()
        return rel
    finally:
        con.close()

