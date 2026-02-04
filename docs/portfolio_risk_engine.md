# Portfolio Risk Engine for Options Trading

## Objectives (Longevity-First)
- Preserve capital through strict loss containment, diversification, and exposure control.
- Maintain portfolio stability across regimes by scaling risk to volatility and correlation.
- Enforce hard stops for drawdown to prevent compounding losses.

## Core Inputs
- **Equity (`E`)**: current account equity (capital + realized PnL + unrealized PnL).
- **Risk budget per trade (`R_max`)**: 1% of equity.
- **Volatility measures**:
  - **Underlying realized vol (`σ_u`)** over a rolling window (e.g., 20–60 trading days).
  - **Implied vol (`σ_iv`)** for contract or chain (if used for shock sizing).
- **Greeks**: portfolio net Δ, Γ, Θ, Vega, Vomma, Vanna (as needed).
- **Correlation**: rolling correlation matrix across underlyings/strategies.
- **Liquidity**: bid-ask spread, OI, volume, depth.

## Hard Risk Rules
1. **Max 1% capital per trade**  
   Per-trade risk (worst-case defined loss) must be ≤ `R_max = 0.01 * E`.
2. **Volatility-adjusted position sizing**  
   Scale sizes down when volatility increases; favor smaller size in stressed regimes.
3. **Portfolio-level Greek caps**  
   Enforce absolute caps on net and gross greeks (Δ, Γ, Vega, Θ).
4. **Max drawdown auto-stop**  
   Freeze new trades when drawdown breaches a hard limit.
5. **Exposure limits per expiry**  
   Cap risk and notional concentration by expiry bucket.
6. **Prevent correlated trades**  
   Reject or downsize new trades that increase correlated exposure beyond limits.

## Position Sizing (Formula-Driven)

### 1) Risk Budget per Trade
```
R_max = 0.01 * E
```

### 2) Volatility Adjustment Multiplier
Use a scaling factor inversely related to volatility:
```
V_adj = min(1.0, σ_target / σ_u)
```
- `σ_target`: long-term median or policy volatility (e.g., 15–20% annualized).
- If `σ_u` spikes above target, size shrinks.

### 3) Options Loss per Contract (Shock-Based)
Define a conservative adverse move `ΔS` (e.g., 1–2 standard deviations):
```
ΔS = k * S * σ_u * sqrt(Δt)
```
Approximate contract loss using Greeks:
```
Loss_per_contract ≈ |Δ| * |ΔS| + 0.5 * |Γ| * (ΔS)^2 + |Vega| * Δσ
```
Where `Δσ` is an implied volatility shock (e.g., 1–2 vol points).

### 4) Max Contracts by Risk
```
N_risk = floor((R_max * V_adj) / Loss_per_contract)
```

### 5) Max Contracts by Liquidity (Optional Cap)
```
N_liq = floor(α * OI)  or  floor(β * ADV)
```
Use `α`, `β` as small fractions (e.g., 1–5%).

### 6) Final Size
```
N_final = min(N_risk, N_liq, N_greeks, N_expiry, N_corr)
```

## Portfolio-Level Greek Caps
Define policy caps relative to equity or underlying notional:
- **Net Delta**: `|Δ_net| ≤ δ_cap`
- **Net Gamma**: `|Γ_net| ≤ γ_cap`
- **Net Vega**: `|Vega_net| ≤ v_cap`
- **Net Theta**: `|Θ_net| ≥ -θ_cap` (limit negative decay)

**Sizing constraint** for a candidate trade with greeks `(Δ_i, Γ_i, Vega_i, Θ_i)`:
```
N_greeks = min over g ∈ {Δ, Γ, Vega, Θ} of
           floor((Cap_g - |G_net|) / |g_i|)
```
Reject trade if any constraint yields `N_greeks ≤ 0`.

## Max Drawdown Auto-Stop
Track rolling peak equity `E_peak`, drawdown `DD`:
```
DD = (E_peak - E) / E_peak
```
Policy:
- If `DD ≥ DD_stop` (e.g., 8–12%), **halt new trades**.
- Resume only after `DD` recovers below a lower threshold (hysteresis).

## Exposure Limits per Expiry
Define bucket limits:
```
Notional_expiry ≤ N_expiry_cap
Risk_expiry ≤ R_expiry_cap
Vega_expiry ≤ V_expiry_cap
```

## Prevent Correlated Trades
Measure correlation of PnL drivers (underlying returns or strategy PnL):
```
ρ_ij = corr(R_i, R_j)
```
Define a correlation threshold `ρ_max` (e.g., 0.6–0.7). For any new trade `j`,
compute incremental correlated risk:
```
Corr_risk_j = Σ_i (w_i * ρ_ij)
```
where `w_i` is exposure weight (e.g., delta-adjusted notional or risk).

Policy:
- **Reject** the trade if `max(ρ_ij)` exceeds `ρ_max`.
- **Downsize** if `Corr_risk_j` breaches a portfolio cap.

## Longevity-Favoring Policy Defaults
- Tight drawdown stop (`DD_stop`), strict re-entry rules.
- Prefer defined-risk structures (verticals, calendars) over naked short options.
- Limit short gamma in high-vol regimes (use `V_adj` to reduce size further).
- Increase margin buffer requirements (e.g., keep ≥ 30–40% cash).

## Risk Engine Workflow (High Level)
1. **Pre-trade validation**: liquidity, data quality, model integrity checks.
2. **Compute risk budget**: `R_max` and `V_adj`.
3. **Estimate loss per contract**: shock-based greek approximation.
4. **Apply portfolio constraints**: greeks, expiry caps, correlation limits.
5. **Size trade**: `N_final` from the minimum constraint.
6. **Post-trade monitoring**: real-time greeks, drawdown, exposure drift.
