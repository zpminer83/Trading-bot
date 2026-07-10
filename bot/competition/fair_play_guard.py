from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Iterable

from bot.competition.confirmed_fill_ledger import ConfirmedFillEvent
from bot.execution.order import OrderIntent


def _as_decimal(value: Decimal | int | str) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _utc_timestamp(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class FairPlayLimits:
    short_window_seconds: Decimal = Decimal("30")
    opposite_side_cooldown_seconds: Decimal = Decimal("60")
    quantity_tolerance_ratio: Decimal = Decimal("0.10")
    near_flat_ratio: Decimal = Decimal("0.10")
    minimum_meaningful_exposure_notional: Decimal = Decimal("5")
    max_completed_near_flat_cycles: int = 2

    def __post_init__(self) -> None:
        short_window = _as_decimal(self.short_window_seconds)
        cooldown = _as_decimal(self.opposite_side_cooldown_seconds)
        tolerance = _as_decimal(self.quantity_tolerance_ratio)
        near_flat = _as_decimal(self.near_flat_ratio)
        minimum_exposure = _as_decimal(self.minimum_meaningful_exposure_notional)
        if short_window < 0:
            raise ValueError("short_window_seconds must be >= 0")
        if cooldown < 0:
            raise ValueError("opposite_side_cooldown_seconds must be >= 0")
        if cooldown < short_window:
            raise ValueError(
                "opposite_side_cooldown_seconds must be >= short_window_seconds"
            )
        if tolerance < 0 or tolerance > 1:
            raise ValueError("quantity_tolerance_ratio must be between 0 and 1")
        if near_flat < 0 or near_flat > 1:
            raise ValueError("near_flat_ratio must be between 0 and 1")
        if minimum_exposure <= 0:
            raise ValueError("minimum_meaningful_exposure_notional must be > 0")
        if self.max_completed_near_flat_cycles < 1:
            raise ValueError("max_completed_near_flat_cycles must be >= 1")
        object.__setattr__(self, "short_window_seconds", short_window)
        object.__setattr__(self, "opposite_side_cooldown_seconds", cooldown)
        object.__setattr__(self, "quantity_tolerance_ratio", tolerance)
        object.__setattr__(self, "near_flat_ratio", near_flat)
        object.__setattr__(self, "minimum_meaningful_exposure_notional", minimum_exposure)


@dataclass(frozen=True)
class FairPlayDecision:
    allowed: bool
    reason: str
    latched: bool
    seconds_since_opposite_fill: Decimal | None
    short_window_round_trip_count: int
    near_flat_cycle_count: int


class FairPlayGuard:
    """Conservative local compliance control with no implicit risk-exit bypass.

    A future real-trading implementation would need an explicit, auditable
    risk-exit order purpose before emergency exits could be treated specially.
    """

    def __init__(self, limits: FairPlayLimits | None = None) -> None:
        self.limits = limits or FairPlayLimits()
        self._latched = False
        self._latch_reason: str | None = None
        self._short_window_round_trip_count = 0
        self._near_flat_cycles_by_symbol: dict[str, int] = {}
        self._latest_by_symbol_side: dict[tuple[str, str], ConfirmedFillEvent] = {}

    @property
    def latched(self) -> bool:
        return self._latched

    @property
    def short_window_round_trip_count(self) -> int:
        return self._short_window_round_trip_count

    @property
    def near_flat_cycle_count(self) -> int:
        return sum(self._near_flat_cycles_by_symbol.values())

    def consume(self, events: Iterable[ConfirmedFillEvent]) -> FairPlayDecision:
        for event in events:
            if not isinstance(event, ConfirmedFillEvent):
                raise TypeError("FairPlayGuard consumes ConfirmedFillEvent objects")
            self._latest_by_symbol_side[(event.symbol, event.side.lower())] = event
            if event.short_window_round_trip:
                self._short_window_round_trip_count += 1
                self._latch("short_window_round_trip")
            previous_cycles = self._near_flat_cycles_by_symbol.get(event.symbol, 0)
            self._near_flat_cycles_by_symbol[event.symbol] = max(
                previous_cycles,
                event.near_flat_cycle_count,
            )
            if self.near_flat_cycle_count >= self.limits.max_completed_near_flat_cycles:
                self._latch("near_flat_cycle_limit")
        return self.status()

    def review_intent(
        self,
        intent: OrderIntent,
        *,
        timestamp: datetime | None,
        current_position: Decimal,
    ) -> FairPlayDecision:
        _as_decimal(current_position)
        if self._latched:
            return self.status()

        observed_at = _utc_timestamp(timestamp)
        side = intent.side.lower()
        if side not in {"buy", "sell"}:
            return self._decision(False, "unsupported_side", None)
        opposite_side = "sell" if side == "buy" else "buy"
        previous_opposite = self._latest_by_symbol_side.get((intent.symbol, opposite_side))
        if previous_opposite is None:
            return self._decision(True, "ok", None)

        elapsed = Decimal(str((observed_at - previous_opposite.timestamp).total_seconds()))
        elapsed = max(elapsed, Decimal("0"))
        quantity = _as_decimal(intent.quantity)
        denominator = max(quantity, previous_opposite.quantity)
        difference_ratio = abs(quantity - previous_opposite.quantity) / denominator
        if (
            elapsed <= self.limits.opposite_side_cooldown_seconds
            and difference_ratio <= self.limits.quantity_tolerance_ratio
        ):
            return self._decision(False, "opposite_side_cooldown", elapsed)
        return self._decision(True, "ok", elapsed)

    def status(self) -> FairPlayDecision:
        return self._decision(
            allowed=not self._latched,
            reason=self._latch_reason or "ok",
            seconds_since_opposite_fill=None,
        )

    def reset(self) -> None:
        self._latched = False
        self._latch_reason = None
        self._short_window_round_trip_count = 0
        self._near_flat_cycles_by_symbol.clear()
        self._latest_by_symbol_side.clear()

    def _latch(self, reason: str) -> None:
        if not self._latched:
            self._latched = True
            self._latch_reason = reason

    def _decision(
        self,
        allowed: bool,
        reason: str,
        seconds_since_opposite_fill: Decimal | None,
    ) -> FairPlayDecision:
        return FairPlayDecision(
            allowed=allowed,
            reason=reason,
            latched=self._latched,
            seconds_since_opposite_fill=seconds_since_opposite_fill,
            short_window_round_trip_count=self.short_window_round_trip_count,
            near_flat_cycle_count=self.near_flat_cycle_count,
        )
