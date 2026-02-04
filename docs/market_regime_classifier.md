# Market regime classifier for NIFTY

This note provides a Hidden Markov / clustering-based regime classifier using the following inputs:

- India VIX
- Realized volatility
- ATR
- Gap frequency
- Rolling returns
- Global index movement (optional)

## Workflow overview

1. Build features with `compute_regime_features`.
2. Fit a `MarketRegimeClassifier` (HMM or GMM).
3. Label hidden states into regimes (`LOW VOL`, `HIGH VOL`, `TREND`, `MEAN REVERT`, `PANIC`).
4. Validate regimes with statistical checks (persistence, separation, stability).

## Statistical validation checklist

- **Out-of-sample log-likelihood / AIC/BIC**: split time series and compare likelihood on the holdout window.
- **Transition matrix persistence**: HMM regimes should show diagonal dominance for stable regimes.
- **Separation**: compare regime means (volatility, returns, gap frequency) and confirm meaningful dispersion.
- **Stability**: bootstrap refits and measure Adjusted Rand Index (ARI) to ensure state assignments are robust.

## Example usage

```python
import pandas as pd
from kaira.research import (
    MarketRegimeClassifier,
    RegimeModelConfig,
    compute_regime_features,
    regime_stability_bootstrap,
)

raw = pd.read_parquet("nifty_ohlc_with_vix.parquet")
features = compute_regime_features(
    raw,
    vix_col="india_vix",
    close_col="close",
    open_col="open",
    high_col="high",
    low_col="low",
    global_return_col="spx_return",
)

config = RegimeModelConfig(model_type="hmm", n_states=5, n_iter=300)
clf = MarketRegimeClassifier(config).fit(features)
regimes = clf.predict(features)

stability = regime_stability_bootstrap(features, config)
print(stability)
```
