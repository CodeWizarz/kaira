from __future__ import annotations

from datetime import date, datetime, timezone

import pyarrow as pa

from kaira.schemas import OPTION_QUOTES_SCHEMA
from kaira.validation.option_quotes import OptionQuoteValidationPolicy, validate_option_quotes


def _row(
    *,
    ts: datetime,
    ts_date: date,
    symbol: str = "NIFTY",
    expiry: date = date(2026, 2, 5),
    strike: int = 18000,
    right: str = "C",
    instrument_id: int = 1,
    option_id: str = "NIFTY|2026-02-05|18000|C",
    underlying: float | None = 22000.0,
    last: float | None = 10.0,
    bid: float | None = 9.5,
    ask: float | None = 10.5,
    bid_qty: int | None = 100,
    ask_qty: int | None = 120,
    iv: float | None = 0.2,
    oi: int | None = 10,
    volume: int | None = 5,
    source: str = "test",
    ingest_ts: datetime | None = None,
):
    ingest_ts = ingest_ts or ts
    return {
        "ts": ts,
        "ts_date": ts_date,
        "symbol": symbol,
        "expiry": expiry,
        "strike": strike,
        "right": right,
        "instrument_id": instrument_id,
        "option_id": option_id,
        "underlying": underlying,
        "last": last,
        "bid": bid,
        "ask": ask,
        "bid_qty": bid_qty,
        "ask_qty": ask_qty,
        "iv": iv,
        "oi": oi,
        "volume": volume,
        "source": source,
        "ingest_ts": ingest_ts,
    }


def test_validate_option_quotes_splits_and_flags() -> None:
    ts = datetime(2026, 2, 4, 10, 0, 0, tzinfo=timezone.utc)
    ts_date = date(2026, 2, 4)

    rows = [
        _row(ts=ts, ts_date=ts_date),
        _row(ts=ts, ts_date=ts_date, bid=11.0, ask=10.0),  # crossed market
        _row(ts=ts, ts_date=ts_date, iv=20.0),  # oob
        _row(ts=ts, ts_date=ts_date, right="X"),  # invalid right
        _row(ts=ts, ts_date=ts_date, volume=None),  # missing required
    ]

    table = pa.Table.from_pylist(rows).cast(OPTION_QUOTES_SCHEMA, safe=False)
    valid, invalid = validate_option_quotes(table, policy=OptionQuoteValidationPolicy(require_bid_ask=True, require_iv=True))

    assert valid.num_rows == 1
    assert invalid.num_rows == 4

    flags = set(invalid["dq_flags"].to_pylist())
    assert 4 in flags  # crossed
    assert 32 in flags  # iv oob
    assert 2 in flags  # invalid right
    assert 1 in flags  # missing required

