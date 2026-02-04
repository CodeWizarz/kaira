from datetime import datetime, timedelta, timezone

from kaira.backtest.execution import (
    ExecutionConfig,
    ExecutionSimulator,
    MarketSnapshot,
    Order,
    OrderSide,
    OrderType,
)


def _snapshot(ts: datetime, bid: float, ask: float, bid_size: float, ask_size: float) -> MarketSnapshot:
    return MarketSnapshot(
        timestamp=ts,
        bid=bid,
        ask=ask,
        bid_size=bid_size,
        ask_size=ask_size,
        last_trade_price=(bid + ask) / 2.0,
    )


def test_aggressive_order_penalty_and_impact() -> None:
    config = ExecutionConfig(
        spread_crossing_penalty_bps=10.0,
        impact_coefficient=0.2,
        rng_seed=1,
    )
    simulator = ExecutionSimulator(config)
    ts = datetime(2024, 1, 1, tzinfo=timezone.utc)
    snapshot = _snapshot(ts, bid=9.0, ask=10.0, bid_size=50.0, ask_size=50.0)
    order = Order(
        order_id="o-1",
        side=OrderSide.BUY,
        quantity=10.0,
        order_type=OrderType.MARKET,
        limit_price=None,
        timestamp=ts,
    )
    simulator.place_order(order, snapshot)
    fills = simulator.on_market(snapshot)
    assert len(fills) == 1
    fill = fills[0]
    expected_penalty = 10.0 * (config.spread_crossing_penalty_bps / 10000.0)
    expected_impact = config.impact_coefficient * (10.0 / 50.0) * (snapshot.ask - snapshot.bid)
    assert fill.price == 10.0 + expected_penalty + expected_impact


def test_passive_queue_and_partial_fill() -> None:
    config = ExecutionConfig(
        fill_probability_at_touch=1.0,
        queue_position_assumption=1.0,
        volume_fill_ratio=0.5,
        rng_seed=2,
    )
    simulator = ExecutionSimulator(config)
    ts = datetime(2024, 1, 1, tzinfo=timezone.utc)
    snapshot = _snapshot(ts, bid=9.0, ask=10.0, bid_size=10.0, ask_size=10.0)
    order = Order(
        order_id="o-2",
        side=OrderSide.BUY,
        quantity=10.0,
        order_type=OrderType.LIMIT,
        limit_price=9.0,
        timestamp=ts,
    )
    simulator.place_order(order, snapshot)
    fills = simulator.on_market(snapshot)
    assert fills == []
    snapshot2 = _snapshot(ts + timedelta(seconds=1), bid=9.0, ask=10.0, bid_size=10.0, ask_size=10.0)
    fills = simulator.on_market(snapshot2)
    assert len(fills) == 1
    assert fills[0].quantity == 5.0
    assert order.remaining_qty == 5.0


def test_cancel_after_time() -> None:
    config = ExecutionConfig(rng_seed=3)
    simulator = ExecutionSimulator(config)
    ts = datetime(2024, 1, 1, tzinfo=timezone.utc)
    snapshot = _snapshot(ts, bid=9.0, ask=10.0, bid_size=10.0, ask_size=10.0)
    order = Order(
        order_id="o-3",
        side=OrderSide.SELL,
        quantity=5.0,
        order_type=OrderType.LIMIT,
        limit_price=10.0,
        timestamp=ts,
        cancel_after=timedelta(seconds=2),
    )
    simulator.place_order(order, snapshot)
    later = _snapshot(ts + timedelta(seconds=3), bid=9.0, ask=10.0, bid_size=10.0, ask_size=10.0)
    fills = simulator.on_market(later)
    assert fills == []
    assert list(simulator.open_orders()) == []
