from __future__ import annotations

import logging
from datetime import date
from pathlib import Path
from typing import Annotated, Optional

import typer

from kaira.config import AppConfig
from kaira.doctor.data import run_data_preflight
from kaira.ingest.async_pipeline import collect_nse_live
from kaira.maintenance.compact import CompactConfig, compact_option_quotes
from kaira.providers.nse_bhavcopy import backfill_nse_fo_bhavcopy

app = typer.Typer(add_completion=False, help="kaira: Indian index options data pipeline")
collect_app = typer.Typer(add_completion=False, help="Live collection / streaming ingestion")
backfill_app = typer.Typer(add_completion=False, help="Historical backfills")
maint_app = typer.Typer(add_completion=False, help="Maintenance / optimization")
doctor_app = typer.Typer(add_completion=False, help="Preflight checks")

app.add_typer(collect_app, name="collect")
app.add_typer(backfill_app, name="backfill")
app.add_typer(maint_app, name="maint")
app.add_typer(doctor_app, name="doctor")


def _setup_logging(verbosity: int) -> None:
    level = logging.WARNING
    if verbosity == 1:
        level = logging.INFO
    elif verbosity >= 2:
        level = logging.DEBUG
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def _parse_date(s: str) -> date:
    try:
        return date.fromisoformat(s)
    except ValueError as e:
        raise typer.BadParameter("Expected YYYY-MM-DD") from e


@collect_app.command("nse-live")
def collect_nse_live_cmd(
    symbols: Annotated[list[str], typer.Option(help="Symbols, e.g. NIFTY BANKNIFTY")] = ["NIFTY", "BANKNIFTY"],
    interval_s: Annotated[float, typer.Option(help="Polling interval in seconds")] = 2.0,
    duration_s: Annotated[Optional[int], typer.Option(help="Run duration (seconds); omit to run until Ctrl+C")] = None,
    out_dir: Annotated[Path, typer.Option(help="Output dataset root (silver layer)")] = Path("data/silver/option_quotes"),
    max_buffer_rows: Annotated[int, typer.Option(help="Flush when buffered rows exceed this")] = 250_000,
    verbosity: Annotated[int, typer.Option("-v", count=True, help="Increase log verbosity")] = 0,
) -> None:
    _setup_logging(verbosity)
    cfg = AppConfig()
    cfg.data.root.mkdir(parents=True, exist_ok=True)
    typer.echo(f"Collecting NSE option-chain snapshots -> {out_dir}")
    collect_nse_live(
        symbols=symbols,
        interval_s=interval_s,
        duration_s=duration_s,
        out_dir=out_dir,
        max_buffer_rows=max_buffer_rows,
        quarantine_dir=cfg.data.quarantine,
    )


@backfill_app.command("nse-bhavcopy")
def backfill_bhavcopy_cmd(
    start: Annotated[str, typer.Option(help="Start date (YYYY-MM-DD)")] = "2024-01-01",
    end: Annotated[str, typer.Option(help="End date (YYYY-MM-DD)")] = "2024-12-31",
    symbols: Annotated[list[str], typer.Option(help="Symbols, e.g. NIFTY BANKNIFTY")] = ["NIFTY", "BANKNIFTY"],
    out_dir: Annotated[Path, typer.Option(help="Output dataset root (silver layer)")] = Path("data/silver/option_quotes"),
    verbosity: Annotated[int, typer.Option("-v", count=True, help="Increase log verbosity")] = 0,
) -> None:
    _setup_logging(verbosity)
    start_d = _parse_date(start)
    end_d = _parse_date(end)
    typer.echo(f"Backfilling NSE FO bhavcopy {start_d}..{end_d} -> {out_dir}")
    backfill_nse_fo_bhavcopy(start=start_d, end=end_d, symbols=symbols, out_dir=out_dir)


@maint_app.command("compact-option-quotes")
def compact_option_quotes_cmd(
    dataset_dir: Annotated[Path, typer.Option(help="Dataset root")] = Path("data/silver/option_quotes"),
    symbol: Annotated[Optional[str], typer.Option(help="Symbol filter (e.g. NIFTY)")] = None,
    expiry: Annotated[Optional[str], typer.Option(help="Expiry filter (YYYY-MM-DD)")] = None,
    trade_date_start: Annotated[Optional[str], typer.Option(help="Trade date start (YYYY-MM-DD)")] = None,
    trade_date_end: Annotated[Optional[str], typer.Option(help="Trade date end (YYYY-MM-DD)")] = None,
    min_files: Annotated[int, typer.Option(help="Only compact partitions with >= this many files")] = 5,
    delete_old: Annotated[bool, typer.Option(help="Delete original part-*.parquet after compact")] = False,
    dry_run: Annotated[bool, typer.Option(help="Print what would happen; don't write")] = False,
    verbosity: Annotated[int, typer.Option("-v", count=True, help="Increase log verbosity")] = 0,
) -> None:
    _setup_logging(verbosity)
    cfg = CompactConfig(min_files=min_files, delete_old=delete_old, dry_run=dry_run)
    expiry_d = _parse_date(expiry) if expiry else None
    td_start = _parse_date(trade_date_start) if trade_date_start else None
    td_end = _parse_date(trade_date_end) if trade_date_end else None
    compact_option_quotes(
        dataset_dir,
        symbol=symbol,
        expiry=expiry_d,
        trade_date_start=td_start,
        trade_date_end=td_end,
        cfg=cfg,
    )


@doctor_app.command("data")
def doctor_data_cmd(
    symbols: Annotated[list[str], typer.Option(help="Symbols to validate (e.g. NIFTY BANKNIFTY)")] = [
        "NIFTY",
        "BANKNIFTY",
    ],
    lookback_days: Annotated[int, typer.Option(help="How many days to search for a bhavcopy file")] = 10,
    sample_rows: Annotated[int, typer.Option(help="How many sample rows to print")] = 5,
    timeout_s: Annotated[float, typer.Option(help="HTTP timeout for bhavcopy download")] = 30.0,
    verbosity: Annotated[int, typer.Option("-v", count=True, help="Increase log verbosity")] = 0,
) -> None:
    _setup_logging(verbosity)
    result = run_data_preflight(
        symbols=symbols,
        lookback_days=lookback_days,
        sample_rows=sample_rows,
        timeout_s=timeout_s,
    )
    for msg in result.messages:
        typer.echo(msg)
    if result.ok:
        typer.echo("PASS: data ingestion preflight complete.")
    else:
        typer.echo("FAIL: data ingestion preflight failed.")
        raise typer.Exit(code=1)
