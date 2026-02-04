# Event-Driven Options Backtesting Engine (Institutional-Style)

## Goals & Non-Goals

**Goals**
- Realistic, event-driven options backtesting with institutional-style market mechanics.
- Support for limit orders, partial fills, slippage, bid/ask simulation, latency, and intraday timestamps.
- Portfolio + risk tracking with Greeks exposure across underlyings and option chains.
- Modular architecture so strategy logic can be plugged in later.

**Non-Goals**
- Ultrafast simulation or vectorized "toy" backtests.
- Perfect market microstructure fidelity (we emulate practical realism, not full LOB reconstruction).

---

## High-Level Architecture (Event-Driven)

```
Market Data -> Event Bus -> Strategy -> Orders -> Execution Simulator -> Fills
                                     |                           |
                                     v                           v
                                Portfolio/Risk <------------ Accounting
                                     |
                                     v
                                Observability
```

### Core Components

1. **Event Bus**
   - Central dispatcher for time-ordered events: market ticks, signals, order acknowledgements, fills, cancel/replace, portfolio updates, end-of-day checks.
   - Strict event sequencing + deterministic replay.

2. **Market Data Replay**
   - Plays historical intraday quotes/trades/greeks snapshots as events.
   - Capable of multi-stream replay (options chain + underlying).
   - Adds synthetic derived events (e.g., mid-price updates, implied vol surface).

3. **Strategy Layer (Plug-in)**
   - Receives market events, emits orders.
   - Does NOT modify portfolio directly; all positions evolve via fills.
   - Can subscribe to risk exposures and account states.

4. **Order Manager**
   - Accepts new orders, cancels, replaces.
   - Maintains order states (NEW, ACK, PARTIAL, FILLED, CANCELED).
   - Enforces risk checks and order constraints (e.g., tick size, minimum lots).

5. **Execution Simulator**
   - Models latency, queue priority, partial fills, slippage, and spread.
   - Uses bid/ask simulation with realistic fills:
     - Market orders cross spread.
     - Limit orders fill when price crosses, with probabilistic partials.
   - Supports execution policies (aggressive, passive).

6. **Portfolio & Risk**
   - Tracks positions, cash, realized/unrealized PnL.
   - Maintains Greeks exposure (Delta, Gamma, Theta, Vega).
   - Evaluates margin usage and stress scenarios.

7. **Accounting/Reporting**
   - Logs execution events, performance metrics, and risk snapshots.
   - Produces trade blotter, latency statistics, and slippage attribution.

---

## Event Model (Intraday Timestamps)

Each event is strictly ordered by `(timestamp, sequence_id)` to avoid ambiguity.

```
Event {
  timestamp: datetime (timezone-aware, intraday)
  type: MarketTick | OrderRequest | OrderAck | Fill | Cancel | PortfolioUpdate | RiskUpdate
  payload: structured dict
}
```

Key point: **no "bar" shortcuts**; all intraday updates are event-driven, with timestamped ticks/quotes and optional trade prints.

---

## Market Data & Bid/Ask Spread Simulation

### Input Streams
- **Underlying**: spot/forward prices with quotes/trades.
- **Options Chain**: bid/ask quotes + trades (if available).
- **Greeks/IV**: calculated or pre-supplied.

### Spread Modeling
If only mid or trade data exists:
- Model dynamic spread: `spread = f(volatility, time_to_expiry, liquidity_proxy)`
- Simulate bid/ask as `mid +/- spread/2`
- Update spread at each tick or in micro-intervals.

---

## Limit Orders & Partial Fills

### Mechanics
Orders are inserted into a simulated queue:
1. Order placed at time `t0`.
2. Latency `L` before it reaches market `t0 + L`.
3. Execution occurs when:
   - Limit buy: best ask <= limit price.
   - Limit sell: best bid >= limit price.
4. If market trades through limit, fill can be partial based on:
   - Liquidity at that level (from data or modeled).
   - Order size vs simulated queue volume.

### Partial Fill Model
- For each eligible event:
  - Determine available volume at price.
  - Execute `min(order_qty_remaining, available_volume * fill_ratio)`
  - Remaining quantity stays in the book.

---

## Slippage Modeling

Slippage is modeled as a function of:
- Spread size
- Trade size vs liquidity
- Volatility regime
- Execution aggressiveness

Example:
```
effective_fill_price = quote_price +/- (spread/2) + k * (order_size / liquidity) * sigma
```
`k` calibrated to match historical execution stats.

---

## Latency & Queue Priority

Latency modeled in three stages:
1. **Strategy latency**: signal generation delay.
2. **Order routing latency**: time to reach exchange.
3. **Exchange processing latency**: time to be queued.

Queue position tracked per order based on arrival time and existing simulated volume.

---

## Portfolio & Greeks Exposure

Portfolio maintains:
- Cash, positions, PnL.
- Options Greeks per instrument + aggregated at portfolio level.

Greeks updated:
1. On market tick (underlying/IV change).
2. On position change (fills).

Risk engine supports:
- Delta/Gamma/Vega exposures
- Scenario analysis (spot shift, IV shift)
- Intraday risk limits

---

## Data Interfaces & Extensibility

### Market Data Interface
```
MarketDataSource:
  next_event() -> Event
  peek_time() -> datetime
```

### Strategy Interface
```
Strategy:
  on_event(event) -> list[OrderRequest]
  on_risk(risk_snapshot) -> list[OrderRequest]
```

### Execution Interface
```
ExecutionModel:
  on_order(order) -> OrderAck
  on_market(event) -> list[Fill]
```

---

## Simulation Workflow

1. Load intraday market data streams.
2. Replay events in chronological order.
3. Strategy observes events and submits orders.
4. Orders pass through latency and execution.
5. Fills update portfolio and risk.
6. Record all events in logs for analysis.

---

## Realism Over Speed Design Choices

- Event-by-event processing (no bar compression).
- Simulated spread and queue position.
- Probabilistic partial fills.
- Latency pipeline, not instantaneous execution.
- Risk recalculated at each fill + key market events.

---

## Implementation Notes

- Use immutable event objects for deterministic replay.
- Logging should include full order lifecycle with timestamps.
- Introduce configurable execution profiles (retail, institutional, dark pool).
- Support calibration from historical execution data.

---

## Future Extensions

- Multi-venue execution simulation.
- Adaptive limit/market order selection.
- Options microstructure features (auction open/close).
- Integration with live trading harness.
