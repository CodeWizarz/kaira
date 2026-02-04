from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

import httpx
import pyarrow as pa

from kaira.schemas import OPTION_QUOTES_SCHEMA
from kaira.utils.ids import instrument_id, option_id
from kaira.utils.time import ist_trade_date, utc_now

log = logging.getLogger(__name__)


_NSE_TS_RE = re.compile(r"^\\d{2}-[A-Za-z]{3}-\\d{4}\\s+\\d{2}:\\d{2}:\\d{2}$")

IST = timezone(timedelta(hours=5, minutes=30))


def _log_event(level: int, event: str, **fields: Any) -> None:
    details = " ".join(f"{k}={v}" for k, v in fields.items() if v is not None)
    message = f"{event} {details}".strip()
    log.log(level, message, extra={"event": event, **fields})

def _parse_ist_timestamp_to_utc(ts: str) -> datetime | None:
    # IST has fixed offset +05:30.
    if not ts or not _NSE_TS_RE.match(ts):
        return None
    try:
        naive = datetime.strptime(ts, "%d-%b-%Y %H:%M:%S")
    except ValueError:
        return None
    dt_ist = naive.replace(tzinfo=IST)
    return dt_ist.astimezone(timezone.utc)


def _parse_expiry(expiry: str) -> date:
    # NSE expiry strings are "06-Feb-2026"
    return datetime.strptime(expiry, "%d-%b-%Y").date()


def option_quotes_from_nse_option_chain(
    payload: dict[str, Any],
    *,
    symbol: str,
    ingest_ts: datetime | None = None,
    source: str = "nse_option_chain",
) -> pa.Table:
    ingest_ts = ingest_ts or utc_now()

    records = payload.get("records") or {}
    ts_str = records.get("timestamp") or payload.get("filtered", {}).get("timestamp")
    ts = _parse_ist_timestamp_to_utc(ts_str) if isinstance(ts_str, str) else None
    ts = ts or ingest_ts
    ts_date = ist_trade_date(ts)

    data = records.get("data") or []
    if not isinstance(data, list):
        _log_event(logging.WARNING, "schema mismatch", source=source, symbol=symbol, field="records.data")
        raise ValueError("NSE payload missing records.data list")

    underlying_value = records.get("underlyingValue")
    try:
        underlying_f = float(underlying_value) if underlying_value is not None else None
    except Exception:
        underlying_f = None

    rows: dict[str, list[Any]] = {name: [] for name in OPTION_QUOTES_SCHEMA.names}

    def _emit(*, expiry: date, strike: int, right: str, leg: dict[str, Any]) -> None:
        iv_raw = leg.get("impliedVolatility")
        iv = None
        try:
            if iv_raw is not None:
                # NSE returns IV in percentage units (e.g. 12.34). Canonical is decimal.
                iv = float(iv_raw) / 100.0
        except Exception:
            iv = None

        bid = leg.get("bidprice") if "bidprice" in leg else leg.get("bidPrice")
        ask = leg.get("askPrice") if "askPrice" in leg else leg.get("askprice")
        last = leg.get("lastPrice")
        oi = leg.get("openInterest")
        volume = leg.get("totalTradedVolume")
        bid_qty = leg.get("bidQty")
        ask_qty = leg.get("askQty")

        oid = option_id(symbol=symbol, expiry=expiry, strike=strike, right=right)
        iid = instrument_id(symbol=symbol, expiry=expiry, strike=strike, right=right)

        rows["ts"].append(ts)
        rows["ts_date"].append(ts_date)
        rows["symbol"].append(symbol)
        rows["expiry"].append(expiry)
        rows["strike"].append(strike)
        rows["right"].append(right)
        rows["instrument_id"].append(iid)
        rows["option_id"].append(oid)
        rows["underlying"].append(underlying_f)
        rows["last"].append(float(last) if last is not None else None)
        rows["bid"].append(float(bid) if bid is not None else None)
        rows["ask"].append(float(ask) if ask is not None else None)
        rows["bid_qty"].append(int(bid_qty) if bid_qty is not None else None)
        rows["ask_qty"].append(int(ask_qty) if ask_qty is not None else None)
        rows["iv"].append(iv)
        rows["oi"].append(int(oi) if oi is not None else None)
        rows["volume"].append(int(volume) if volume is not None else None)
        rows["source"].append(source)
        rows["ingest_ts"].append(ingest_ts)

    for rec in data:
        if not isinstance(rec, dict):
            continue
        expiry_s = rec.get("expiryDate")
        strike_raw = rec.get("strikePrice")
        if not expiry_s or strike_raw is None:
            continue
        try:
            expiry = _parse_expiry(str(expiry_s))
        except Exception:
            continue
        try:
            strike = int(round(float(strike_raw)))
        except Exception:
            continue

        ce = rec.get("CE")
        pe = rec.get("PE")
        if isinstance(ce, dict):
            _emit(expiry=expiry, strike=strike, right="C", leg=ce)
        if isinstance(pe, dict):
            _emit(expiry=expiry, strike=strike, right="P", leg=pe)

    arrays = []
    for field in OPTION_QUOTES_SCHEMA:
        arrays.append(pa.array(rows[field.name], type=field.type))
    return pa.Table.from_arrays(arrays, schema=OPTION_QUOTES_SCHEMA)


@dataclass
class NSEOptionChainClient:
    """
    Lightweight NSE option-chain client.

    Notes:
      - NSE blocks naive scraping; this tries to behave like a browser by
        bootstrapping cookies from the homepage and using realistic headers.
      - Treat this as a *collector* for building your own history going forward.
        For full historical tick/minute option chain (incl. bid/ask & IV),
        you typically need a commercial market-data vendor.
    """

    timeout_s: float = 10.0

    def __post_init__(self) -> None:
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "NSEOptionChainClient":
        self._client = httpx.AsyncClient(timeout=self.timeout_s, follow_redirects=True)
        await self._bootstrap()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:  # noqa: ANN001
        if self._client:
            await self._client.aclose()
        self._client = None

    async def fetch(self, symbol: str) -> dict[str, Any]:
        if not self._client:
            raise RuntimeError("Client not initialized; use 'async with NSEOptionChainClient()'")

        url = "https://www.nseindia.com/api/option-chain-indices"
        headers = {
            "accept": "application/json,text/plain,*/*",
            "accept-language": "en-US,en;q=0.9",
            "referer": "https://www.nseindia.com/option-chain",
            "user-agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/121.0.0.0 Safari/537.36"
            ),
        }

        for attempt in range(3):
            resp = await self._client.get(url, params={"symbol": symbol}, headers=headers)
            if resp.status_code in (401, 403):
                log.warning("NSE returned %s; re-bootstrap cookies (attempt %d)", resp.status_code, attempt + 1)
                await self._bootstrap()
                continue
            resp.raise_for_status()
            return resp.json()

        raise RuntimeError("Failed to fetch option-chain after retries (auth/blocked?)")

    async def _bootstrap(self) -> None:
        if not self._client:
            return
        home = "https://www.nseindia.com/"
        headers = {
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "accept-language": "en-US,en;q=0.9",
            "user-agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/121.0.0.0 Safari/537.36"
            ),
        }
        try:
            resp = await self._client.get(home, headers=headers)
            resp.raise_for_status()
        except Exception:
            log.exception("Bootstrap failed; NSE may be blocking requests")
            raise
