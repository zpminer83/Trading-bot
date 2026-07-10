from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from bot.competition.confirmed_fill_ledger import ConfirmedFillEvent
from bot.competition.fair_play_guard import FairPlayGuard, FairPlayLimits
from bot.execution.order import OrderIntent


START = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)
SYMBOL = "SOMI:USDso"


def event(
    *,
    side: str = "buy",
    short: bool = False,
    cycles: int = 0,
) -> ConfirmedFillEvent:
    return ConfirmedFillEvent(
        sequence_number=1,
        timestamp=START,
        symbol=SYMBOL,
        side=side,
        price=Decimal("1"),
        quantity=Decimal("5"),
        notional=Decimal("5"),
        position_before=Decimal("0"),
        position_after=Decimal("5"),
        seconds_since_previous_fill=None,
        seconds_since_opposite_fill=None,
        previous_opposite_side=None,
        previous_opposite_quantity=None,
        opposite_quantity_difference_ratio=None,
        short_window_round_trip=short,
        near_flat_cycle_completed=cycles > 0,
        near_flat_cycle_count=cycles,
    )


def intent(side: str, quantity: str = "5") -> OrderIntent:
    return OrderIntent(
        symbol=SYMBOL,
        side=side,
        order_type="limit",
        price=Decimal("1"),
        quantity=Decimal(quantity),
    )


def test_limits_validate_and_short_window_event_latches():
    with pytest.raises(ValueError, match="cooldown"):
        FairPlayLimits(short_window_seconds=Decimal("61"))
    with pytest.raises(ValueError, match="between 0 and 1"):
        FairPlayLimits(near_flat_ratio=Decimal("2"))

    guard = FairPlayGuard()
    decision = guard.consume([event(short=True)])
    assert decision.allowed is False
    assert decision.latched is True
    assert decision.reason == "short_window_round_trip"
    assert guard.status().latched is True
    guard.reset()
    assert guard.status().allowed is True


def test_near_flat_limit_and_opposite_cooldown():
    guard = FairPlayGuard()
    guard.consume([event(cycles=1)])
    assert guard.status().latched is False
    assert guard.consume([event(cycles=2)]).latched is True

    guard.reset()
    guard.consume([event(side="buy")])
    blocked = guard.review_intent(
        intent("sell", "5.4"),
        timestamp=START + timedelta(seconds=60),
        current_position=Decimal("5"),
    )
    assert blocked.allowed is False
    assert blocked.reason == "opposite_side_cooldown"
    assert guard.review_intent(
        intent("buy"),
        timestamp=START + timedelta(seconds=1),
        current_position=Decimal("5"),
    ).allowed is True
    assert guard.review_intent(
        intent("sell", "8"),
        timestamp=START + timedelta(seconds=1),
        current_position=Decimal("5"),
    ).allowed is True
