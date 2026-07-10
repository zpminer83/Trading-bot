from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Iterable

from bot.execution.conservative_paper_broker import PaperOrder
from bot.market.models import OrderBook, OrderBookLevel


@dataclass(frozen=True)
class PassiveFillEvidence:
    order_id: int
    symbol: str
    side: str
    order_price: Decimal
    observed_at: datetime
    age_seconds: Decimal
    at_touch: bool
    crossed: bool
    same_side_level_present: bool
    previous_level_quantity: Decimal | None
    current_level_quantity: Decimal | None
    level_quantity_decreased: bool
    level_disappeared: bool


@dataclass
class _OrderObservationState:
    first_observed_at: datetime
    level_quantity: Decimal | None


class PassiveFillEvidenceTracker:
    """Tracks objective order-book evidence without simulating fills."""

    def __init__(self):
        self._states: dict[int, _OrderObservationState] = {}

    def observe(
        self,
        orders: Iterable[PaperOrder],
        orderbook: OrderBook,
        observed_at: datetime | None = None,
    ) -> list[PassiveFillEvidence]:
        now = self._to_utc(observed_at or datetime.now(timezone.utc))
        open_orders = [order for order in orders if order.status == "open"]
        self._remove_closed_states(open_orders)

        evidence: list[PassiveFillEvidence] = []

        for order in open_orders:
            current_quantity = self._level_quantity(order, orderbook)
            state = self._states.get(order.order_id)

            if state is None:
                first_observed_at = now
                previous_quantity = None
            else:
                first_observed_at = state.first_observed_at
                previous_quantity = state.level_quantity

            age_seconds = Decimal(
                str(max(0.0, (now - first_observed_at).total_seconds()))
            )
            at_touch, crossed = self._touch_and_cross(order, orderbook)

            evidence.append(
                PassiveFillEvidence(
                    order_id=order.order_id,
                    symbol=order.intent.symbol,
                    side=order.intent.side,
                    order_price=order.intent.price,
                    observed_at=now,
                    age_seconds=age_seconds,
                    at_touch=at_touch,
                    crossed=crossed,
                    same_side_level_present=current_quantity is not None,
                    previous_level_quantity=previous_quantity,
                    current_level_quantity=current_quantity,
                    level_quantity_decreased=(
                        previous_quantity is not None
                        and current_quantity is not None
                        and current_quantity < previous_quantity
                    ),
                    level_disappeared=(
                        previous_quantity is not None
                        and current_quantity is None
                    ),
                )
            )

            self._states[order.order_id] = _OrderObservationState(
                first_observed_at=first_observed_at,
                level_quantity=current_quantity,
            )

        return evidence

    def synchronize(
        self,
        orders: Iterable[PaperOrder],
        orderbook: OrderBook,
        observed_at: datetime | None = None,
    ) -> None:
        now = self._to_utc(observed_at or datetime.now(timezone.utc))
        open_orders = [order for order in orders if order.status == "open"]
        self._remove_closed_states(open_orders)

        for order in open_orders:
            if order.order_id in self._states:
                continue

            self._states[order.order_id] = _OrderObservationState(
                first_observed_at=now,
                level_quantity=self._level_quantity(order, orderbook),
            )

    def reset(self) -> None:
        self._states.clear()

    @property
    def tracked_order_ids(self) -> frozenset[int]:
        return frozenset(self._states)

    def _remove_closed_states(self, open_orders: list[PaperOrder]) -> None:
        open_order_ids = {order.order_id for order in open_orders}

        for order_id in tuple(self._states):
            if order_id not in open_order_ids:
                self._states.pop(order_id, None)

    @staticmethod
    def _level_quantity(
        order: PaperOrder,
        orderbook: OrderBook,
    ) -> Decimal | None:
        levels = (
            orderbook.bids
            if order.intent.side == "buy"
            else orderbook.asks
        )
        matching_levels = [
            level.quantity
            for level in levels
            if level.price == order.intent.price
        ]

        if not matching_levels:
            return None

        return sum(matching_levels, Decimal("0"))

    @staticmethod
    def _touch_and_cross(
        order: PaperOrder,
        orderbook: OrderBook,
    ) -> tuple[bool, bool]:
        best_bid = PassiveFillEvidenceTracker._best_bid(orderbook.bids)
        best_ask = PassiveFillEvidenceTracker._best_ask(orderbook.asks)
        order_price = order.intent.price

        if order.intent.side == "buy":
            return (
                best_bid is not None and order_price == best_bid.price,
                best_ask is not None and best_ask.price <= order_price,
            )

        if order.intent.side == "sell":
            return (
                best_ask is not None and order_price == best_ask.price,
                best_bid is not None and best_bid.price >= order_price,
            )

        raise ValueError(f"unsupported paper order side: {order.intent.side}")

    @staticmethod
    def _best_bid(levels: list[OrderBookLevel]) -> OrderBookLevel | None:
        return max(levels, key=lambda level: level.price, default=None)

    @staticmethod
    def _best_ask(levels: list[OrderBookLevel]) -> OrderBookLevel | None:
        return min(levels, key=lambda level: level.price, default=None)

    @staticmethod
    def _to_utc(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)

        return value.astimezone(timezone.utc)
