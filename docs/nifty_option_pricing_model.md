# NIFTY Option Fair-Value Modeling (Tabular ML)

## Objective
Build a robust, tabular ML model to estimate **fair mid-price** for NIFTY options and detect statistically significant mispricing by comparing market mid-price to a model-derived confidence interval.

## Modeling Approach (LightGBM/XGBoost)
**Primary recommendation:** LightGBM with:
- **Point model** (regression) to predict mid-price.
- **Quantile models** (e.g., 5th/50th/95th percentiles) to estimate prediction intervals.

Alternative: XGBoost with quantile regression or conformal prediction on residuals.

## Data & Feature Engineering
**Target:**
- Mid-price = (best_bid + best_ask) / 2

**Core features (as requested):**
- Moneyness (e.g., spot/strike or log(spot/strike))
- Time to expiry (in years or trading days)
- Implied volatility (IV)
- Realized volatility (RV) over a rolling window (e.g., 10–30 days)
- Distance from ATM (absolute or signed distance)
- Open interest (OI)
- Volume
- Bid/ask spread (ask - bid, or relative spread)

**Additional robust, non-leaky tabular features (optional, recommended):**
- Option type (call/put) as a categorical feature
- Risk-free rate proxy (overnight/short-term yield)
- Dividend yield proxy (if applicable for index; can be 0 if negligible)
- Lagged features: IV change, RV change, OI/volume change
- Time-of-day and day-of-week (microstructure effects)

## Robust Target Transformations
Options have heavy tails and heteroskedasticity. Consider:
- **Log transform** of mid-price (with floor epsilon) for stability.
- **Normalized price** by spot or strike (e.g., mid / spot).
- **Scaled spread** = spread / mid-price for robustness.

## Training/Validation Strategy (Avoid Leakage)
- Use **time-based splits** (e.g., rolling/expanding window).
- Avoid random shuffling due to temporal dependence.
- Keep data grouped by **expiry** when appropriate.

Example:
1. Train: months 1–6
2. Validate: month 7
3. Test: month 8
4. Slide the window forward and repeat (walk-forward validation).

## Model 1: Point Prediction (Fair Value)
Train LightGBM regressor to minimize MAE or Huber loss.
- Use early stopping on validation.
- Keep **monotonic constraints** where relevant (e.g., price increases with time to expiry for ATM options, decreases with distance from ATM).

## Model 2: Prediction Intervals
Two robust options:

### A) Quantile Regression (recommended)
Train separate LightGBM models for quantiles:
- Q05, Q50, Q95
This yields an interval [Q05, Q95] for mid-price.

### B) Conformal Prediction (model-agnostic)
1. Train a point model.
2. Compute residuals on a calibration set.
3. Use empirical quantiles of |residual| to build intervals:
   - Interval = y_hat ± q_(1-α)

This provides **finite-sample coverage** guarantees, robust to non-normal errors.

## Mispricing Signal
Define a **z-score style deviation**:
- Deviation = (mid_market - fair_mid_pred)
- If mid_market is outside the prediction interval, flag as **significant mispricing**.

Example signal rules:
- **Overpriced** if mid_market > Q95
- **Underpriced** if mid_market < Q05
- **Neutral** otherwise

## Suggested Evaluation Metrics
- MAE / RMSE for point prediction
- Interval coverage rate (e.g., % of actual mid within Q05–Q95)
- Pinball loss for quantiles
- Stability across regimes and expiries

## Feature Consistency & Data Quality
- Ensure synchronized timestamps for spot, option chain, and IV/RV.
- Handle missing OI/volume carefully (forward-fill or neutral imputation).
- Winsorize or cap extreme outliers (especially spreads and OI).

## Model Monitoring & Robustness
- Monitor residuals by expiry, moneyness buckets, and time-to-expiry.
- Retrain on rolling basis (e.g., weekly/monthly).
- Track drift in IV/RV distributions.

## Minimal Pseudocode (LightGBM)
```python
import lightgbm as lgb

# prepare features and target
X_train, y_train = ...
X_valid, y_valid = ...

# point model
params = {
    "objective": "regression",
    "metric": "mae",
    "learning_rate": 0.05,
    "num_leaves": 63,
    "feature_fraction": 0.9,
    "bagging_fraction": 0.8,
    "bagging_freq": 5,
}
model = lgb.train(params, lgb.Dataset(X_train, label=y_train),
                  valid_sets=[lgb.Dataset(X_valid, label=y_valid)],
                  num_boost_round=2000, early_stopping_rounds=100)

# quantile model example
q_params = dict(params)
q_params["objective"] = "quantile"
q_params["alpha"] = 0.05
q05 = lgb.train(q_params, lgb.Dataset(X_train, label=y_train))

q_params["alpha"] = 0.95
q95 = lgb.train(q_params, lgb.Dataset(X_train, label=y_train))

# mispricing signal
pred = model.predict(X_valid)
low = q05.predict(X_valid)
high = q95.predict(X_valid)

signal = (y_valid > high) - (y_valid < low)
```

## Practical Notes for NIFTY Options
- Prefer **index-wide features** and ensure IV/RV are aligned to the same underlying.
- Use **consistent contract filtering** (e.g., remove illiquid strikes with large spreads).
- Consider **separate models** for calls/puts or incorporate option type explicitly.

---

If you want, I can also provide a template training script with feature engineering and walk-forward validation.
