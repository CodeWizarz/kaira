from __future__ import annotations

import hashlib
from datetime import date


def option_id(symbol: str, expiry: date, strike: int, right: str) -> str:
    r = right.upper()
    if r not in {"C", "P"}:
        raise ValueError(f"right must be 'C' or 'P', got {right!r}")
    return f"{symbol.upper()}|{expiry.isoformat()}|{strike}|{r}"


def instrument_id(symbol: str, expiry: date, strike: int, right: str) -> int:
    """
    Stable 64-bit id for joins / grouping.
    Uses BLAKE2b (8-byte digest) to avoid Python's randomized hash().
    """
    oid = option_id(symbol=symbol, expiry=expiry, strike=strike, right=right)
    digest = hashlib.blake2b(oid.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, byteorder="little", signed=False)

