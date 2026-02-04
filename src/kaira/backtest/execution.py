from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Iterable

import random


class OrderSide(str, Enum):
    BUY = "buy"
    SELL = "sell"


class OrderType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"


class OrderStatus(str, Enum):
    NEW = "new"
    ACK = "ack"
    PARTIAL = "partial"
    FILLED = "filled"
    CANCELED = "canceled"


@dataclass(frozen=True)
class MarketSnapshot:
    timestamp: datetime
    bid: float
    ask: float
    bid_size: float
    ask_size: float
    last_trade_price: float | None = None
    last_trade_size: float | None = None
    volatility: float | None = None

    @property
    def spread(self) -> float:
        return max(self.ask - self.bid, 0.0)


@dataclass
class Order:
    order_id: str
    side: OrderSide
    quantity: float
    order_type: OrderType
    limit_price: float | None
    timestamp: datetime
    time_in_force: timedelta | None = None
    cancel_after: timedelta | None = None
    status: OrderStatus = OrderStatus.NEW
    remaining_qty: float = field(init=False)
    queue_ahead: float = 0.0
    cancel_requested_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.quantity <= 0:
            raise ValueError("quantity must be positive")
        if self.order_type == OrderType.LIMIT and self.limit_price is None:
            raise ValueError("limit orders require limit_price")
        self.remaining_qty = float(self.quantity)


@dataclass(frozen=True)
class Fill:
    order_id: str
    timestamp: datetime
    quantity: float
    price: float
    liquidity_used: float
    reason: str


@dataclass(frozen=True)
class ExecutionConfig:
    fill_probability_at_touch: float = 0.35
    fill_probability_through: float = 0.75
    queue_position_assumption: float = 0.6
    volume_fill_ratio: float = 0.4
    spread_crossing_penalty_bps: float = 5.0
    impact_coefficient: float = 0.15
    cancel_latency: timedelta = timedelta(milliseconds=200)
    rng_seed: int = 7


class ExecutionSimulator:
    """Simulates fills for option orders with conservative execution mechanics."""

    def __init__(self, config: ExecutionConfig | None = None) -> None:
        self.config = config or ExecutionConfig()
        self._orders: dict[str, Order] = {}
        self._rng = random.Random(self.config.rng_seed)

    def open_orders(self) -> Iterable[Order]:
        return tuple(self._orders.values())

    def place_order(self, order: Order, snapshot: MarketSnapshot) -> None:
        if order.order_id in self._orders:
            raise ValueError(f"duplicate order_id {order.order_id}")
        order.status = OrderStatus.ACK
        order.queue_ahead = self._initial_queue_position(order, snapshot)
        self._orders[order.order_id] = order

    def request_cancel(self, order_id: str, timestamp: datetime) -> None:
        order = self._orders.get(order_id)
        if order is None or order.status in {OrderStatus.CANCELED, OrderStatus.FILLED}:
            return
        order.cancel_requested_at = timestamp

    def on_market(self, snapshot: MarketSnapshot) -> list[Fill]:
        fills: list[Fill] = []
        for order in list(self._orders.values()):
            if self._should_cancel(order, snapshot.timestamp):
                order.status = OrderStatus.CANCELED
                self._orders.pop(order.order_id, None)
                continue

            if order.remaining_qty <= 0:
                order.status = OrderStatus.FILLED
                self._orders.pop(order.order_id, None)
                continue

            if self._is_crossing(order, snapshot):
                fills.extend(self._fill_aggressive(order, snapshot))
                continue

            fills.extend(self._fill_passive(order, snapshot))

        return fills

    def _should_cancel(self, order: Order, now: datetime) -> bool:
        if order.cancel_requested_at is not None:
            if now >= order.cancel_requested_at + self.config.cancel_latency:
                return True
        if order.cancel_after is not None:
            if now >= order.timestamp + order.cancel_after:
                return True
        if order.time_in_force is not None:
            if now >= order.timestamp + order.time_in_force:
                return True
        return False

    def _initial_queue_position(self, order: Order, snapshot: MarketSnapshot) -> float:
        if order.order_type == OrderType.MARKET:
            return 0.0
        if order.side == OrderSide.BUY:
            visible = snapshot.bid_size
        else:
            visible = snapshot.ask_size
        return max(visible, 0.0) * self.config.queue_position_assumption

    def _is_crossing(self, order: Order, snapshot: MarketSnapshot) -> bool:
        if order.order_type == OrderType.MARKET:
            return True
        if order.side == OrderSide.BUY:
            return order.limit_price is not None and order.limit_price >= snapshot.ask
        return order.limit_price is not None and order.limit_price <= snapshot.bid

    def _fill_aggressive(self, order: Order, snapshot: MarketSnapshot) -> list[Fill]:
        liquidity = snapshot.ask_size if order.side == OrderSide.BUY else snapshot.bid_size
        fill_qty = min(order.remaining_qty, max(liquidity, order.remaining_qty))
        base_price = snapshot.ask if order.side == OrderSide.BUY else snapshot.bid
        penalty = base_price * (self.config.spread_crossing_penalty_bps / 10000.0)
        price = base_price + penalty if order.side == OrderSide.BUY else base_price - penalty
        price = self._apply_impact(price, order, fill_qty, liquidity, snapshot)
        order.remaining_qty -= fill_qty
        order.status = OrderStatus.FILLED if order.remaining_qty <= 0 else OrderStatus.PARTIAL
        if order.remaining_qty <= 0:
            self._orders.pop(order.order_id, None)
        return [
            Fill(
                order_id=order.order_id,
                timestamp=snapshot.timestamp,
                quantity=fill_qty,
                price=price,
                liquidity_used=liquidity,
                reason="crossed",
            )
        ]

    def _fill_passive(self, order: Order, snapshot: MarketSnapshot) -> list[Fill]:
        if order.order_type != OrderType.LIMIT:
            return []
        if order.side == OrderSide.BUY:
            touch = snapshot.bid
            visible = snapshot.bid_size
            within_limit = order.limit_price is not None and order.limit_price >= touch
        else:
            touch = snapshot.ask
            visible = snapshot.ask_size
            within_limit = order.limit_price is not None and order.limit_price <= touch

        if not within_limit:
            return []

        executed_volume = max(visible, 0.0) * self.config.volume_fill_ratio
        if order.queue_ahead > 0:
            order.queue_ahead = max(order.queue_ahead - executed_volume, 0.0)
            if order.queue_ahead > 0:
                return []

        fill_probability = self.config.fill_probability_at_touch
        if self._traded_through(order, snapshot):
            fill_probability = max(fill_probability, self.config.fill_probability_through)
        if self._rng.random() > fill_probability:
            return []

        fill_qty = min(order.remaining_qty, executed_volume)
        if fill_qty <= 0:
            return []

        price = float(order.limit_price)
        price = self._apply_impact(price, order, fill_qty, visible, snapshot)
        order.remaining_qty -= fill_qty
        order.status = OrderStatus.FILLED if order.remaining_qty <= 0 else OrderStatus.PARTIAL
        if order.remaining_qty <= 0:
            self._orders.pop(order.order_id, None)
        return [
            Fill(
                order_id=order.order_id,
                timestamp=snapshot.timestamp,
                quantity=fill_qty,
                price=price,
                liquidity_used=visible,
                reason="passive",
            )
        ]

    def _traded_through(self, order: Order, snapshot: MarketSnapshot) -> bool:
        if snapshot.last_trade_price is None or order.limit_price is None:
            return False
        if order.side == OrderSide.BUY:
            return snapshot.last_trade_price <= order.limit_price
        return snapshot.last_trade_price >= order.limit_price

    def _apply_impact(
        self,
        price: float,
        order: Order,
        fill_qty: float,
        liquidity: float,
        snapshot: MarketSnapshot,
    ) -> float:
        if self.config.impact_coefficient <= 0:
            return price
        liquidity = max(liquidity, 1.0)
        scale = snapshot.volatility if snapshot.volatility is not None else snapshot.spread
        impact = self.config.impact_coefficient * (fill_qty / liquidity) * max(scale, 1e-8)
        return price + impact if order.side == OrderSide.BUY else price - impact
