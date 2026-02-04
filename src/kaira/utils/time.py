from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


def utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


def ist_trade_date(ts_utc: datetime):
    if ts_utc.tzinfo is None:
        raise ValueError("ts_utc must be timezone-aware (UTC)")
    return ts_utc.astimezone(IST).date()

