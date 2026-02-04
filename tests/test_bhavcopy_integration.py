from __future__ import annotations

from datetime import date
from pathlib import Path

import duckdb
import pytest

from kaira.providers.nse_bhavcopy import backfill_nse_fo_bhavcopy


@pytest.mark.slow
def test_bhavcopy_end_to_end(tmp_path: Path) -> None:
    out_dir = tmp_path / "dataset"

    backfill_nse_fo_bhavcopy(
        start=date(2024, 1, 2),
        end=date(2024, 1, 2),
        symbols=["NIFTY"],
        out_dir=out_dir,
    )

    parquet_glob = str(out_dir / "**" / "*.parquet")
    with duckdb.connect(database=":memory:") as con:
        row_count = con.execute("SELECT COUNT(*) FROM read_parquet(?)", [parquet_glob]).fetchone()[0]

    assert row_count > 0
