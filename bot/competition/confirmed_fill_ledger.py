from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Iterable, Mapping

from bot.execution.conservative_paper_broker import PaperOrder
from bot.execution.paper_broker import PaperFill


def _as_decimal(value: Decimal | int | str) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _utc_timestamp(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _elapsed_seconds(current: datetime, previous: datetime | None) -> Decimal | None:
    if previous is None:
        return None
    elapsed = Decimal(str((current - previous).total_seconds()))
    return max(elapsed, Decimal("0"))


@dataclass(frozen=True)
class ConfirmedFillLedgerLimits:
    short_window_seconds: Decimal = Decimal("30")
    quantity_tolerance_ratio: Decimal = Decimal("0.10")
    near_flat_ratio: Decimal = Decimal("0.10")
    minimum_meaningful_exposure_notional: Decimal = Decimal("5")

    def __post_init__(self) -> None:
        short_window = _as_decimal(self.short_window_seconds)
        tolerance = _as_decimal(self.quantity_tolerance_ratio)
        near_flat = _as_decimal(self.near_flat_ratio)
        minimum_exposure = _as_decimal(self.minimum_meaningful_exposure_notional)
        if short_window < 0:
            raise ValueError("short_window_seconds must be >= 0")
        if tolerance < 0 or tolerance > 1:
            raise ValueError("quantity_tolerance_ratio must be between 0 and 1")
        if near_flat < 0 or near_flat > 1:
            raise ValueError("near_flat_ratio must be between 0 and 1")
        if minimum_exposure <= 0:
            raise ValueError("minimum_meaningful_exposure_notional must be > 0")
        object.__setattr__(self, "short_window_seconds", short_window)
        object.__setattr__(self, "quantity_tolerance_ratio", tolerance)
        object.__setattr__(self, "near_flat_ratio", near_flat)
        object.__setattr__(self, "minimum_meaningful_exposure_notional", minimum_exposure)


@dataclass(frozen=True)
class ConfirmedFillEvent:
    sequence_number: int
    timestamp: datetime
    symbol: str
    side: str
    price: Decimal
    quantity: Decimal
    notional: Decimal
    position_before: Decimal
    position_after: Decimal
    seconds_since_previous_fill: Decimal | None
    seconds_since_opposite_fill: Decimal | None
    previous_opposite_side: str | None
    previous_opposite_quantity: Decimal | None
    opposite_quantity_difference_ratio: Decimal | None
    short_window_round_trip: bool
    near_flat_cycle_completed: bool
    near_flat_cycle_count: int
    purpose: str = "unknown"
    strategy_name: str = "unknown"
    rationale: str | None = None
    signal_id: str | None = None
    source_order_id: str | int | None = None


@dataclass
class _SymbolState:
    last_event: ConfirmedFillEvent | None = None
    last_by_side: dict[str, ConfirmedFillEvent] = field(default_factory=dict)
    cycle_active: bool = False
    peak_absolute_position: Decimal = Decimal("0")
    near_flat_cycle_count: int = 0
    reconstructed_position: Decimal = Decimal("0")


class ConfirmedFillLedger:
    """Audit ledger fed exclusively with fills returned by the paper broker."""

    def __init__(self, limits: ConfirmedFillLedgerLimits | None = None) -> None:
        self.limits = limits or ConfirmedFillLedgerLimits()
        self._events: list[ConfirmedFillEvent] = []
        self._states: dict[str, _SymbolState] = {}
        self._next_sequence_number = 1

    @property
    def events(self) -> tuple[ConfirmedFillEvent, ...]:
        return tuple(self._events)

    @property
    def short_window_round_trip_count(self) -> int:
        return sum(event.short_window_round_trip for event in self._events)

    @property
    def near_flat_cycle_count(self) -> int:
        return sum(state.near_flat_cycle_count for state in self._states.values())

    def record_fills(
        self,
        fills: Iterable[PaperFill],
        starting_position: Decimal,
        timestamp: datetime | None = None,
        source_orders_by_fill_id: Mapping[int, PaperOrder] | None = None,
    ) -> list[ConfirmedFillEvent]:
        observed_at = _utc_timestamp(timestamp)
        initial_position = _as_decimal(starting_position)
        if initial_position < 0:
            raise ValueError("starting_position cannot be negative")

        positions: dict[str, Decimal] = {}
        recorded: list[ConfirmedFillEvent] = []
        for fill in fills:
            if not isinstance(fill, PaperFill):
                raise TypeError("ConfirmedFillLedger accepts only PaperFill objects")
            side = fill.side.lower()
            if side not in {"buy", "sell"}:
                raise ValueError(f"unsupported fill side: {fill.side}")
            price = _as_decimal(fill.price)
            quantity = _as_decimal(fill.quantity)
            notional = _as_decimal(fill.notional)
            if price <= 0 or quantity <= 0 or notional <= 0:
                raise ValueError("confirmed fill price, quantity, and notional must be positive")

            state = self._states.setdefault(fill.symbol, _SymbolState())
            position_before = positions.get(fill.symbol, initial_position)
            position_after = (
                position_before + quantity if side == "buy" else position_before - quantity
            )
            if position_after < 0:
                raise ValueError(
                    f"confirmed fill reconstruction produced a negative position for {fill.symbol}"
                )
            positions[fill.symbol] = position_after

            opposite_side = "sell" if side == "buy" else "buy"
            previous_opposite = state.last_by_side.get(opposite_side)
            since_previous = _elapsed_seconds(
                observed_at,
                state.last_event.timestamp if state.last_event else None,
            )
            since_opposite = _elapsed_seconds(
                observed_at,
                previous_opposite.timestamp if previous_opposite else None,
            )
            quantity_difference_ratio: Decimal | None = None
            short_window_round_trip = False
            if previous_opposite is not None:
                denominator = max(quantity, previous_opposite.quantity)
                quantity_difference_ratio = abs(quantity - previous_opposite.quantity) / denominator
                short_window_round_trip = bool(
                    since_opposite is not None
                    and since_opposite <= self.limits.short_window_seconds
                    and quantity_difference_ratio <= self.limits.quantity_tolerance_ratio
                )

            cycle_completed = self._update_cycle(
                state=state,
                position_before=position_before,
                position_after=position_after,
                price=price,
            )
            source_order = (
                source_orders_by_fill_id.get(id(fill))
                if source_orders_by_fill_id is not None
                else None
            )
            source_intent = None
            if isinstance(source_order, PaperOrder) and source_order.status == "filled":
                source_intent = source_order.intent
            event = ConfirmedFillEvent(
                sequence_number=self._next_sequence_number,
                timestamp=observed_at,
                symbol=fill.symbol,
                side=side,
                price=price,
                quantity=quantity,
                notional=notional,
                position_before=position_before,
                position_after=position_after,
                seconds_since_previous_fill=since_previous,
                seconds_since_opposite_fill=since_opposite,
                previous_opposite_side=(previous_opposite.side if previous_opposite else None),
                previous_opposite_quantity=(
                    previous_opposite.quantity if previous_opposite else None
                ),
                opposite_quantity_difference_ratio=quantity_difference_ratio,
                short_window_round_trip=short_window_round_trip,
                near_flat_cycle_completed=cycle_completed,
                near_flat_cycle_count=state.near_flat_cycle_count,
                purpose=(
                    source_intent.purpose.value if source_intent is not None else "unknown"
                ),
                strategy_name=(
                    source_intent.strategy_name if source_intent is not None else "unknown"
                ),
                rationale=(source_intent.rationale if source_intent is not None else None),
                signal_id=(source_intent.signal_id if source_intent is not None else None),
                source_order_id=(
                    source_order.order_id if source_intent is not None else None
                ),
            )
            self._next_sequence_number += 1
            self._events.append(event)
            recorded.append(event)
            state.last_event = event
            state.last_by_side[side] = event
            state.reconstructed_position = position_after
        return recorded

    def reset(self, symbol: str | None = None) -> None:
        if symbol is None:
            self._events.clear()
            self._states.clear()
            self._next_sequence_number = 1
            return
        self._states.pop(symbol, None)
        self._events = [event for event in self._events if event.symbol != symbol]

    def _update_cycle(
        self,
        *,
        state: _SymbolState,
        position_before: Decimal,
        position_after: Decimal,
        price: Decimal,
    ) -> bool:
        absolute_before = abs(position_before)
        absolute_after = abs(position_after)
        started_now = False
        if not state.cycle_active:
            before_notional = absolute_before * price
            after_notional = absolute_after * price
            if (
                before_notional < self.limits.minimum_meaningful_exposure_notional
                and after_notional >= self.limits.minimum_meaningful_exposure_notional
            ):
                state.cycle_active = True
                state.peak_absolute_position = absolute_after
                started_now = True

        if not state.cycle_active:
            return False

        state.peak_absolute_position = max(state.peak_absolute_position, absolute_after)
        if (
            not started_now
            and absolute_after
            <= state.peak_absolute_position * self.limits.near_flat_ratio
        ):
            state.near_flat_cycle_count += 1
            state.cycle_active = False
            state.peak_absolute_position = Decimal("0")
            return True
        return False
