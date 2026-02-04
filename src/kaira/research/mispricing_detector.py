from __future__ import annotations

from dataclasses import dataclass, field
from math import erf, sqrt
from typing import Iterable, Sequence

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class MispricingDetectorConfig:
    mid_col: str = "mid"
    fair_col: str = "fair_price"
    bid_col: str = "bid"
    ask_col: str = "ask"
    bid_qty_col: str = "bid_qty"
    ask_qty_col: str = "ask_qty"
    volume_col: str = "volume"
    oi_col: str = "oi"
    gamma_col: str = "gamma"
    ts_col: str = "ts"
    rolling_window: int = 60
    min_periods: int | None = None
    threshold_sigma: float = 2.0
    min_rolling_std: float = 1e-6
    min_abs_mispricing: float = 0.0
    min_liquidity: float = 1.0
    min_volume: float = 0.0
    min_oi: float = 0.0
    max_spread: float | None = None
    max_spread_bps: float | None = 25.0
    max_abs_gamma: float | None = None
    macro_event_windows: Sequence[tuple[pd.Timestamp, pd.Timestamp]] = field(
        default_factory=tuple
    )
    event_padding_minutes: int = 0


def detect_mispricing_candidates(
    data: pd.DataFrame,
    *,
    config: MispricingDetectorConfig,
) -> pd.DataFrame:
    """Return candidate trades with probability-weighted edge.

    Mispricing = mid - fair. Signals trigger when abs(mispricing) exceeds a
    dynamic threshold based on rolling standard deviation, plus filters to
    reduce false positives.
    """
    df = data.copy()
    _ensure_mid_price(df, config)
    _ensure_timestamp(df, config)

    df["mispricing"] = df[config.mid_col] - df[config.fair_col]
    rolling_std = (
        df["mispricing"]
        .rolling(config.rolling_window, min_periods=config.min_periods)
        .std()
        .fillna(0.0)
    )
    df["rolling_std"] = rolling_std.clip(lower=config.min_rolling_std)
    df["threshold"] = df["rolling_std"] * config.threshold_sigma

    df["zscore"] = df["mispricing"] / df["rolling_std"]
    df["edge_prob"] = _edge_probability(df["zscore"].abs())
    df["signal"] = np.where(df["mispricing"] < 0, "long", "short")
    df["edge"] = df["edge_prob"] * df["mispricing"].abs()
    df["signed_edge"] = np.where(df["signal"] == "long", df["edge"], -df["edge"])

    liquidity_ok = _liquidity_filter(df, config)
    spread_ok = _spread_filter(df, config)
    event_ok = _event_window_filter(df, config)
    gamma_ok = _gamma_filter(df, config)
    threshold_ok = df["mispricing"].abs().ge(df["threshold"]) & df[
        "mispricing"
    ].abs().ge(config.min_abs_mispricing)

    df["passes_filters"] = (
        liquidity_ok & spread_ok & event_ok & gamma_ok & threshold_ok
    )

    return df.loc[df["passes_filters"]].copy()


def _ensure_mid_price(df: pd.DataFrame, config: MispricingDetectorConfig) -> None:
    if config.mid_col in df.columns:
        return
    if config.bid_col not in df.columns or config.ask_col not in df.columns:
        raise ValueError(
            f"Missing {config.mid_col} and bid/ask columns for mid-price."
        )
    df[config.mid_col] = (df[config.bid_col] + df[config.ask_col]) / 2


def _ensure_timestamp(df: pd.DataFrame, config: MispricingDetectorConfig) -> None:
    if config.ts_col not in df.columns:
        raise ValueError(f"Missing timestamp column: {config.ts_col}.")
    if not np.issubdtype(df[config.ts_col].dtype, np.datetime64):
        df[config.ts_col] = pd.to_datetime(df[config.ts_col], utc=True)


def _liquidity_filter(df: pd.DataFrame, config: MispricingDetectorConfig) -> pd.Series:
    bid_ok = (
        df[config.bid_qty_col] >= config.min_liquidity
        if config.bid_qty_col in df.columns
        else pd.Series(True, index=df.index)
    )
    ask_ok = (
        df[config.ask_qty_col] >= config.min_liquidity
        if config.ask_qty_col in df.columns
        else pd.Series(True, index=df.index)
    )
    volume_ok = (
        df[config.volume_col] >= config.min_volume
        if config.volume_col in df.columns
        else pd.Series(True, index=df.index)
    )
    oi_ok = (
        df[config.oi_col] >= config.min_oi
        if config.oi_col in df.columns
        else pd.Series(True, index=df.index)
    )
    return bid_ok & ask_ok & volume_ok & oi_ok


def _spread_filter(df: pd.DataFrame, config: MispricingDetectorConfig) -> pd.Series:
    if config.bid_col not in df.columns or config.ask_col not in df.columns:
        return pd.Series(True, index=df.index)
    spread = df[config.ask_col] - df[config.bid_col]
    spread_ok = spread.ge(0)
    if config.max_spread is not None:
        spread_ok &= spread.le(config.max_spread)
    if config.max_spread_bps is not None:
        with np.errstate(divide="ignore", invalid="ignore"):
            spread_bps = spread / df[config.mid_col] * 1e4
        spread_ok &= spread_bps.le(config.max_spread_bps)
    return spread_ok


def _event_window_filter(df: pd.DataFrame, config: MispricingDetectorConfig) -> pd.Series:
    if not config.macro_event_windows:
        return pd.Series(True, index=df.index)
    padding = pd.Timedelta(minutes=config.event_padding_minutes)
    timestamps = df[config.ts_col]
    allowed = pd.Series(True, index=df.index)
    for start, end in _normalize_event_windows(config.macro_event_windows):
        window_start = start - padding
        window_end = end + padding
        allowed &= ~timestamps.between(window_start, window_end)
    return allowed


def _normalize_event_windows(
    windows: Iterable[tuple[pd.Timestamp, pd.Timestamp]],
) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    normalized = []
    for start, end in windows:
        start_ts = pd.Timestamp(start, tz="UTC")
        end_ts = pd.Timestamp(end, tz="UTC")
        if end_ts < start_ts:
            raise ValueError("Macro event window end must be >= start.")
        normalized.append((start_ts, end_ts))
    return normalized


def _gamma_filter(df: pd.DataFrame, config: MispricingDetectorConfig) -> pd.Series:
    if config.max_abs_gamma is None or config.gamma_col not in df.columns:
        return pd.Series(True, index=df.index)
    return df[config.gamma_col].abs().le(config.max_abs_gamma)


def _edge_probability(zscore_abs: pd.Series) -> pd.Series:
    return 2 * _normal_cdf(zscore_abs) - 1


def _normal_cdf(values: pd.Series) -> pd.Series:
    scaled = values / sqrt(2.0)
    return 0.5 * (1.0 + scaled.apply(erf))
