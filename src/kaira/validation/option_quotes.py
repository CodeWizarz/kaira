from __future__ import annotations

from dataclasses import dataclass

import pyarrow as pa
import pyarrow.compute as pc


@dataclass(frozen=True)
class OptionQuoteValidationPolicy:
    require_bid_ask: bool = False
    require_iv: bool = False
    iv_min: float = 0.0
    iv_max: float = 10.0  # 1000% vol upper bound; reject obvious corruption


def _is_in_right_domain(col: pa.ChunkedArray) -> pa.Array:
    # Accept "C"/"P", "CE"/"PE" (some sources use CE/PE)
    upper = pc.utf8_upper(col)
    is_c = pc.or_(pc.equal(upper, "C"), pc.equal(upper, "CE"))
    is_p = pc.or_(pc.equal(upper, "P"), pc.equal(upper, "PE"))
    return pc.or_(is_c, is_p)


def validate_option_quotes(
    table: pa.Table,
    *,
    policy: OptionQuoteValidationPolicy | None = None,
) -> tuple[pa.Table, pa.Table]:
    """
    Split into (valid, invalid) using fast vectorized checks.

    The invalid table includes an extra `dq_flags` column (bitmask):
      1: missing required
      2: invalid right
      4: crossed market (ask < bid)
      8: negative oi/volume
     16: NaN in numeric
     32: iv out of bounds
    """
    if table.num_rows == 0:
        return table, table

    policy = policy or OptionQuoteValidationPolicy()
    names = set(table.schema.names)

    required = {
        "ts",
        "ts_date",
        "symbol",
        "expiry",
        "strike",
        "right",
        "instrument_id",
        "option_id",
        "oi",
        "volume",
        "source",
        "ingest_ts",
    }
    if policy.require_bid_ask:
        required |= {"bid", "ask"}
    if policy.require_iv:
        required |= {"iv"}
    missing_cols = sorted(required - names)
    if missing_cols:
        raise ValueError(f"Missing required columns: {missing_cols}")

    n = table.num_rows

    false = pa.array([False] * n)

    # missing required values
    missing_value = false
    for col in required:
        missing_value = pc.or_(missing_value, pc.is_null(table[col]))

    # right domain
    right_ok = pc.fill_null(_is_in_right_domain(table["right"]), False)

    # crossed markets
    bid = table["bid"] if "bid" in names else pa.array([None] * n, type=pa.float32())
    ask = table["ask"] if "ask" in names else pa.array([None] * n, type=pa.float32())
    crossed = pc.and_(
        pc.and_(pc.is_valid(bid), pc.is_valid(ask)),
        pc.less(ask, bid),
    )
    crossed = pc.fill_null(crossed, False)

    # oi/volume non-negative where present
    negative = false
    for col in ("oi", "volume"):
        if col in names:
            cmp = pc.fill_null(pc.less(table[col], 0), False)
            negative = pc.or_(negative, cmp)

    # NaN checks (floats only)
    nan_any = false
    for col in ("bid", "ask", "last", "underlying", "iv"):
        if col in names and pa.types.is_floating(table[col].type):
            nan_any = pc.or_(nan_any, pc.fill_null(pc.is_nan(table[col]), False))

    # iv range if present
    iv_oob = false
    if "iv" in names:
        iv = table["iv"]
        iv_oob = pc.and_(
            pc.is_valid(iv),
            pc.or_(pc.less(iv, policy.iv_min), pc.greater(iv, policy.iv_max)),
        )
        iv_oob = pc.fill_null(iv_oob, False)

    dq_flags = pc.add(
        pc.add(
            pc.add(
                pc.add(
                    pc.add(
                        pc.if_else(missing_value, 1, 0),
                        pc.if_else(pc.invert(right_ok), 2, 0),
                    ),
                    pc.if_else(crossed, 4, 0),
                ),
                pc.if_else(negative, 8, 0),
            ),
            pc.if_else(nan_any, 16, 0),
        ),
        pc.if_else(iv_oob, 32, 0),
    ).cast(pa.int32())
    dq_flags = pc.fill_null(dq_flags, 0).cast(pa.int32())

    is_valid_row = pc.equal(dq_flags, 0)

    valid = table.filter(is_valid_row)
    invalid_mask = pc.invert(is_valid_row)
    invalid_flags = pc.filter(dq_flags, invalid_mask)
    invalid = table.filter(invalid_mask).append_column("dq_flags", invalid_flags)
    return valid, invalid
