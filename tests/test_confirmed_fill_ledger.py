from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from bot.competition.confirmed_fill_ledger import ConfirmedFillLedger
from bot.execution.paper_broker import PaperFill


SYMBOL = "SOMI:USDso"
START = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)


def fill(side: str, quantity: str = "5", symbol: str = SYMBOL) -> PaperFill:
    quantity_decimal = Decimal(quantity)
    return PaperFill(
        symbol=symbol,
        side=side,
        price=Decimal("1"),
        quantity=quantity_decimal,
        notional=quantity_decimal,
    )


def test_first_fill_is_a_confirmed_event_without_false_round_trip():
    ledger = ConfirmedFillLedger()

    events = ledger.record_fills([fill("buy")], Decimal("0"), START)

    assert len(events) == 1
    event = events[0]
    assert event.sequence_number == 1
    assert event.position_before == Decimal("0")
    assert event.position_after == Decimal("5")
    assert event.seconds_since_previous_fill is None
    assert event.seconds_since_opposite_fill is None
    assert event.short_window_round_trip is False
    assert ledger.events == (event,)


def test_sequential_positions_and_short_window_boundaries():
    ledger = ConfirmedFillLedger()
    ledger.record_fills([fill("buy")], Decimal("0"), START)

    event = ledger.record_fills(
        [fill("sell")],
        Decimal("5"),
        START + timedelta(seconds=30),
    )[0]

    assert event.position_before == Decimal("5")
    assert event.position_after == Decimal("0")
    assert event.seconds_since_opposite_fill == Decimal("30")
    assert event.opposite_quantity_difference_ratio == Decimal("0")
    assert event.short_window_round_trip is True

    later = ledger.record_fills(
        [fill("buy")],
        Decimal("0"),
        START + timedelta(seconds=61),
    )[0]
    assert later.short_window_round_trip is False


def test_round_trip_uses_quantity_tolerance_and_clamps_negative_elapsed():
    ledger = ConfirmedFillLedger()
    ledger.record_fills([fill("buy")], Decimal("0"), START)
    outside_tolerance = ledger.record_fills(
        [fill("sell", "4")], Decimal("5"), START + timedelta(seconds=29)
    )[0]
    assert outside_tolerance.short_window_round_trip is False
    assert outside_tolerance.seconds_since_opposite_fill == Decimal("29")

    ledger = ConfirmedFillLedger()
    ledger.record_fills([fill("buy")], Decimal("0"), START)
    earlier = ledger.record_fills(
        [fill("sell")], Decimal("5"), START - timedelta(seconds=1)
    )[0]
    assert earlier.seconds_since_opposite_fill == Decimal("0")
    assert earlier.short_window_round_trip is True


def test_negative_position_is_rejected_and_states_are_independent():
    ledger = ConfirmedFillLedger()

    with pytest.raises(ValueError, match="negative position"):
        ledger.record_fills([fill("sell")], Decimal("0"), START)

    ledger.record_fills([fill("buy", symbol="OTHER")], Decimal("0"), START)
    event = ledger.record_fills([fill("buy")], Decimal("0"), START)[0]
    assert event.seconds_since_previous_fill is None


def test_near_flat_cycles_and_resets():
    ledger = ConfirmedFillLedger()
    ledger.record_fills([fill("buy")], Decimal("0"), START)
    completed = ledger.record_fills(
        [fill("sell")], Decimal("5"), START + timedelta(seconds=31)
    )[0]

    assert completed.near_flat_cycle_completed is True
    assert completed.near_flat_cycle_count == 1
    assert ledger.near_flat_cycle_count == 1

    ledger.record_fills(
        [fill("buy", symbol="OTHER")], Decimal("0"), START
    )
    ledger.reset(SYMBOL)
    assert all(event.symbol != SYMBOL for event in ledger.events)
    assert ledger.near_flat_cycle_count == 0

    ledger.reset()
    assert ledger.events == ()
    assert ledger.short_window_round_trip_count == 0
