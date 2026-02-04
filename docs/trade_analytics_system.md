# Trade analytics system design

## Objectives

- Capture rich trade-level telemetry at entry, during holding, and at exit so we can analyze strategy performance drivers and failure modes.
- Normalize logs into an analysis-friendly schema that supports fast slicing by regime, volatility context, and pricing edge.
- Cluster trades after sufficient sample size to locate conditions where the strategy outperforms and where risk controls need tightening.
- Maintain a continuous improvement loop that feeds learnings back into signal generation, sizing, and execution.

## Trade lifecycle logging

Capture logs at three stages: `entry`, `monitor`, and `exit`. Each trade record should include unique identifiers (`trade_id`, `strategy_id`, `instrument_id`) and timestamps (`entry_ts`, `exit_ts`).

### Entry log fields

- **Regime at entry**: label from the market regime classifier (e.g., `trend`, `range`, `high_volatility`), plus confidence score.
- **IV percentile**: percentile of implied volatility relative to a rolling window for the option or underlying.
- **Mispricing magnitude**: edge estimate vs theoretical price (absolute + signed in premium points and percentage).
- **Greek exposure**: net delta, gamma, vega, theta at entry (per unit and portfolio-scaled).
- **Slippage estimate (expected)**: model-based slippage expectation based on spread, liquidity, and latency.

### Monitoring log fields (optional intratrade snapshots)

- **MFE/MAE**: track maximum favorable/adverse excursion updates over time.
- **Greek drift**: updated exposures as the underlying moves and time decays.
- **IV movement**: realized IV percentile changes since entry.

### Exit log fields

- **Holding time**: total duration from entry to exit.
- **MFE/MAE (final)**: final maximum excursion values and timestamps.
- **Slippage (realized)**: executed price vs mid/expected price.
- **Greek exposure at exit**: net exposures at liquidation.
- **Exit reason**: strategy exit signal, stop, target, time, or risk override.

## Data model

Store analytics in a “gold” layer dataset to keep it distinct from raw market data. Suggested tables:

### `trade_events`

One row per discrete event (`entry`, `monitor`, `exit`).

| Column | Type | Notes |
| --- | --- | --- |
| trade_id | string | Unique trade identifier |
| strategy_id | string | Strategy/version identifier |
| event_type | string | entry/monitor/exit |
| event_ts | timestamp | Event timestamp |
| symbol | string | Underlying symbol |
| instrument_id | int64 | Option/contract identifier |
| regime_label | string | Market regime at event |
| regime_conf | float | Confidence score |
| iv_percentile | float | IV percentile |
| mispricing_abs | float | Absolute edge in premium points |
| mispricing_pct | float | Edge vs theo in % |
| delta | float | Net delta exposure |
| gamma | float | Net gamma exposure |
| vega | float | Net vega exposure |
| theta | float | Net theta exposure |
| slippage_bps | float | Slippage in basis points |
| mfe | float | Max favorable excursion |
| mae | float | Max adverse excursion |
| notes | string | Optional tags |

### `trade_summary`

One row per trade with final outcomes for aggregation and clustering.

| Column | Type | Notes |
| --- | --- | --- |
| trade_id | string | Unique trade identifier |
| strategy_id | string | Strategy/version identifier |
| entry_ts | timestamp | Entry time |
| exit_ts | timestamp | Exit time |
| holding_time_s | int | Holding time in seconds |
| pnl | float | Realized PnL |
| pnl_pct | float | % PnL vs premium or margin |
| mfe | float | Max favorable excursion |
| mae | float | Max adverse excursion |
| slippage_bps | float | Realized slippage |
| regime_label | string | Entry regime |
| iv_percentile | float | Entry IV percentile |
| mispricing_abs | float | Entry edge |
| delta | float | Entry delta |
| gamma | float | Entry gamma |
| vega | float | Entry vega |
| theta | float | Entry theta |
| exit_reason | string | Exit category |

## Feature engineering for clustering

Normalize continuous metrics and create derived features to improve clustering stability:

- **Scaled exposures**: divide delta/gamma/vega by notional or premium.
- **Edge quality**: `mispricing_abs / bid_ask_spread` to capture signal-to-noise.
- **Regime encoding**: one-hot or embedding for `regime_label`.
- **Volatility context**: combine `iv_percentile` with realized vol percentile to capture skew between implied and realized.
- **Risk adjusted outcome**: `pnl / mae` to understand reward per adverse excursion.

## Clustering workflow

1. **Minimum sample requirement**: only cluster after hitting a statistically meaningful sample size (e.g., 300–500 trades or per-strategy thresholds).
2. **Prepare dataset**: join `trade_summary` with relevant regime and volatility features.
3. **Dimension reduction**: optionally use PCA or UMAP for visualization.
4. **Clustering algorithm**: start with k-means or HDBSCAN; compare silhouette score and stability.
5. **Cluster profiling**: compute per-cluster stats (win rate, average PnL, MAE, slippage) and identify best/worst clusters.
6. **Actionability**: translate clusters into rule adjustments (e.g., avoid high IV percentile + low mispricing).

## Continuous improvement loop

- **Weekly/biweekly review**: re-run clustering and generate a summary report.
- **Hypothesis testing**: confirm if high-performing clusters remain stable over time.
- **Strategy updates**: adjust entry filters, position sizing, and exit rules based on cluster insights.
- **Telemetry quality checks**: validate missing fields, regime classification drift, and IV percentile window size.
- **Model governance**: track strategy versioning so changes can be attributed to performance shifts.

## Suggested implementation plan

1. **Schema design**: implement `trade_events` and `trade_summary` as Parquet datasets in a gold layer.
2. **Logging interface**: extend strategy code to emit entry/monitor/exit logs.
3. **Analytics pipeline**: build a batch job that aggregates trades into `trade_summary` and computes features.
4. **Clustering notebook or job**: run clustering and output a report/dashboard.
5. **Feedback integration**: feed actionable cluster rules into strategy configuration.

## Success criteria

- Improved win rate or risk-adjusted returns in targeted clusters.
- Reduced exposure to regimes with persistent underperformance.
- Lower slippage and MAE on trades after updates.
