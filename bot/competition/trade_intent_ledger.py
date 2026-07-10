from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from bot.competition.fair_play_guard import FairPlayDecision
from bot.execution.conservative_paper_broker import PaperOrder
from bot.execution.order import OrderDecision, OrderIntent


def _utc_timestamp(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class TradeIntentEvent:
    sequence_number: int
    timestamp: datetime
    symbol: str
    side: str
    price: Decimal
    quantity: Decimal
    notional: Decimal
    purpose: str
    strategy_name: str
    rationale: str | None
    signal_id: str | None
    fair_play_allowed: bool | None
    fair_play_reason: str | None
    execution_approved: bool | None
    execution_reason: str | None
    submitted: bool
    resulting_order_id: str | int | None


class TradeIntentLedger:
    """Append-only audit telemetry for generated strategy intents only."""

    def __init__(self) -> None:
        self._events: list[TradeIntentEvent] = []
        self._next_sequence_number = 1

    @property
    def events(self) -> tuple[TradeIntentEvent, ...]:
        return tuple(self._events)

    def record_intent(
        self,
        intent: OrderIntent,
        *,
        timestamp: datetime | None = None,
        fair_play_decision: FairPlayDecision | None = None,
        execution_decision: OrderDecision | None = None,
        submitted_order: PaperOrder | None = None,
    ) -> TradeIntentEvent:
        if not isinstance(intent, OrderIntent):
            raise TypeError("TradeIntentLedger accepts only OrderIntent objects")
        if execution_decision is not None and execution_decision.intent is not intent:
            raise ValueError("execution decision must belong to the recorded intent")
        if submitted_order is not None and submitted_order.intent is not intent:
            raise ValueError("submitted order must belong to the recorded intent")

        event = TradeIntentEvent(
            sequence_number=self._next_sequence_number,
            timestamp=_utc_timestamp(timestamp),
            symbol=intent.symbol,
            side=intent.side,
            price=intent.price,
            quantity=intent.quantity,
            notional=intent.notional,
            purpose=intent.purpose.value,
            strategy_name=intent.strategy_name,
            rationale=intent.rationale,
            signal_id=intent.signal_id,
            fair_play_allowed=(
                fair_play_decision.allowed if fair_play_decision is not None else None
            ),
            fair_play_reason=(
                fair_play_decision.reason if fair_play_decision is not None else None
            ),
            execution_approved=(
                execution_decision.approved if execution_decision is not None else None
            ),
            execution_reason=(
                execution_decision.reason if execution_decision is not None else None
            ),
            submitted=submitted_order is not None,
            resulting_order_id=(
                submitted_order.order_id if submitted_order is not None else None
            ),
        )
        self._next_sequence_number += 1
        self._events.append(event)
        return event

    def reset(self) -> None:
        self._events.clear()
        self._next_sequence_number = 1
