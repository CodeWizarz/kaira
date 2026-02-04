from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Optional

import numpy as np
import pandas as pd
from hmmlearn.hmm import GaussianHMM
from sklearn.mixture import GaussianMixture
from sklearn.preprocessing import StandardScaler
from sklearn.metrics import adjusted_rand_score


class RegimeLabel(StrEnum):
    LOW_VOL = "LOW VOL"
    HIGH_VOL = "HIGH VOL"
    TREND = "TREND"
    MEAN_REVERT = "MEAN REVERT"
    PANIC = "PANIC"


@dataclass(frozen=True)
class RegimeModelConfig:
    model_type: str = "hmm"  # "hmm" or "gmm"
    n_states: int = 5
    covariance_type: str = "full"
    n_iter: int = 200
    random_state: int = 42
    scale_features: bool = True


def compute_regime_features(
    data: pd.DataFrame,
    *,
    vix_col: str = "vix",
    close_col: str = "close",
    open_col: str = "open",
    high_col: str = "high",
    low_col: str = "low",
    global_return_col: Optional[str] = None,
    realized_vol_window: int = 20,
    atr_window: int = 14,
    gap_window: int = 20,
    gap_threshold: float = 0.01,
    rolling_return_window: int = 20,
) -> pd.DataFrame:
    """Compute regime features from OHLCV + VIX inputs.

    Expected columns: open, high, low, close, vix, and optionally global returns.
    """
    df = data.copy()
    df["vix"] = df[vix_col]
    df["log_return"] = np.log(df[close_col]).diff()
    df["realized_vol"] = (
        df["log_return"]
        .rolling(realized_vol_window)
        .std()
        .mul(np.sqrt(252))
    )

    high_low = df[high_col] - df[low_col]
    high_close = (df[high_col] - df[close_col].shift()).abs()
    low_close = (df[low_col] - df[close_col].shift()).abs()
    true_range = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    df["atr"] = true_range.rolling(atr_window).mean()

    gap = (df[open_col] - df[close_col].shift()).abs() / df[close_col].shift()
    df["gap_freq"] = gap.gt(gap_threshold).rolling(gap_window).mean()

    df["rolling_return"] = df["log_return"].rolling(rolling_return_window).sum()

    if global_return_col is not None:
        df["global_return"] = df[global_return_col]

    features = [
        "vix",
        "realized_vol",
        "atr",
        "gap_freq",
        "rolling_return",
    ]
    if global_return_col is not None:
        features.append("global_return")

    return df[features].dropna()


class MarketRegimeClassifier:
    """Hidden Markov / GMM regime classifier with rule-based state labeling."""

    def __init__(self, config: RegimeModelConfig) -> None:
        self.config = config
        self.scaler = StandardScaler() if config.scale_features else None
        self.model = self._build_model()
        self.state_labels: dict[int, RegimeLabel] = {}

    def _build_model(self) -> GaussianHMM | GaussianMixture:
        if self.config.model_type.lower() == "hmm":
            return GaussianHMM(
                n_components=self.config.n_states,
                covariance_type=self.config.covariance_type,
                n_iter=self.config.n_iter,
                random_state=self.config.random_state,
            )
        if self.config.model_type.lower() == "gmm":
            return GaussianMixture(
                n_components=self.config.n_states,
                covariance_type=self.config.covariance_type,
                max_iter=self.config.n_iter,
                random_state=self.config.random_state,
            )
        raise ValueError("model_type must be 'hmm' or 'gmm'.")

    def _prepare(self, features: pd.DataFrame) -> np.ndarray:
        values = features.to_numpy()
        if self.scaler is None:
            return values
        return self.scaler.fit_transform(values)

    def fit(self, features: pd.DataFrame) -> "MarketRegimeClassifier":
        X = self._prepare(features)
        self.model.fit(X)
        self._label_states(features, self.predict_states(features))
        return self

    def predict_states(self, features: pd.DataFrame) -> np.ndarray:
        X = features.to_numpy()
        if self.scaler is not None:
            X = self.scaler.transform(X)
        if isinstance(self.model, GaussianHMM):
            return self.model.predict(X)
        return self.model.predict(X)

    def predict(self, features: pd.DataFrame) -> pd.Series:
        states = self.predict_states(features)
        labels = pd.Series(states, index=features.index).map(self.state_labels)
        return labels

    def fit_predict(self, features: pd.DataFrame) -> pd.Series:
        self.fit(features)
        return self.predict(features)

    def _label_states(self, features: pd.DataFrame, states: np.ndarray) -> None:
        summary = summarize_regimes(features, states)
        labels = _assign_regime_labels(summary)
        self.state_labels = labels


def summarize_regimes(features: pd.DataFrame, states: np.ndarray) -> pd.DataFrame:
    df = features.copy()
    df["state"] = states
    summary = df.groupby("state").agg(
        vix=("vix", "mean"),
        realized_vol=("realized_vol", "mean"),
        atr=("atr", "mean"),
        gap_freq=("gap_freq", "mean"),
        rolling_return=("rolling_return", "mean"),
        return_std=("rolling_return", "std"),
    )
    summary["return_autocorr"] = (
        df.groupby("state")["rolling_return"].apply(lambda s: s.autocorr())
    )
    return summary


def _assign_regime_labels(summary: pd.DataFrame) -> dict[int, RegimeLabel]:
    labels: dict[int, RegimeLabel] = {}
    vol_rank = summary["realized_vol"].rank()
    return_rank = summary["rolling_return"].rank()
    autocorr = summary["return_autocorr"]

    panic_state = summary["realized_vol"].idxmax()
    labels[panic_state] = RegimeLabel.PANIC

    low_vol_state = summary["realized_vol"].idxmin()
    if low_vol_state != panic_state:
        labels[low_vol_state] = RegimeLabel.LOW_VOL

    remaining = [s for s in summary.index if s not in labels]
    for state in remaining:
        if vol_rank[state] >= vol_rank.median() and summary.loc[state, "rolling_return"] < 0:
            labels[state] = RegimeLabel.HIGH_VOL
        elif autocorr[state] > 0.2 or return_rank[state] >= return_rank.median():
            labels[state] = RegimeLabel.TREND
        else:
            labels[state] = RegimeLabel.MEAN_REVERT

    return labels


def regime_stability_bootstrap(
    features: pd.DataFrame,
    config: RegimeModelConfig,
    *,
    n_bootstrap: int = 20,
    sample_frac: float = 0.8,
) -> float:
    """Refit model on bootstrap samples and return mean Adjusted Rand Index."""
    if n_bootstrap < 2:
        raise ValueError("n_bootstrap must be >= 2.")
    baseline = MarketRegimeClassifier(config).fit_predict(features)
    scores = []
    for _ in range(n_bootstrap):
        sample = features.sample(frac=sample_frac, replace=True, random_state=None)
        preds = MarketRegimeClassifier(config).fit_predict(sample)
        joined = baseline.to_frame("baseline").join(
            preds.rename("bootstrap"), how="inner"
        )
        scores.append(
            adjusted_rand_score(joined["baseline"], joined["bootstrap"])
        )
    return float(np.mean(scores))
