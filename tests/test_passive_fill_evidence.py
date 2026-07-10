from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from bot.execution.conservative_paper_broker import (
    ConservativePaperBroker,
    PaperOrder,
)
from bot.execution.order import OrderIntent
from bot.execution.passive_fill_evidence import PassiveFillEvidenceTracker
from bot.market.models import OrderBook, OrderBookLevel
from bot.portfolio.portfolio_manager import PortfolioManager


SYMBOL = "SOMI:USDso"
BASE_TIME = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)


def make_order(
    order_id: int,
    side: str,
    price: str,
) -> PaperOrder:
    return PaperOrder(
        order_id=order_id,
        intent=OrderIntent(
            symbol=SYMBOL,
            side=side,
            order_type="limit",
            price=Decimal(price),
            quantity=Decimal("1"),
        ),
    )


def make_book(
    *,
    bids: list[tuple[str, str]],
    asks: list[tuple[str, str]],
) -> OrderBook:
    return OrderBook(
        symbol=SYMBOL,
        bids=[
            OrderBookLevel(price=Decimal(price), quantity=Decimal(quantity))
            for price, quantity in bids
        ],
        asks=[
            OrderBookLevel(price=Decimal(price), quantity=Decimal(quantity))
            for price, quantity in asks
        ],
    )


def test_buy_order_at_best_bid():
    tracker = PassiveFillEvidenceTracker()
    order = make_order(1, "buy", "100")
    book = make_book(bids=[("100", "5")], asks=[("101", "5")])

    evidence = tracker.observe([order], book, BASE_TIME)[0]

    assert evidence.at_touch is True
    assert evidence.crossed is False
    assert evidence.same_side_level_present is True
    assert evidence.current_level_quantity == Decimal("5")


def test_sell_order_at_best_ask():
    tracker = PassiveFillEvidenceTracker()
    order = make_order(1, "sell", "101")
    book = make_book(bids=[("100", "5")], asks=[("101", "7")])

    evidence = tracker.observe([order], book, BASE_TIME)[0]

    assert evidence.at_touch is True
    assert evidence.crossed is False
    assert evidence.current_level_quantity == Decimal("7")


def test_buy_order_crossed_by_best_ask():
    tracker = PassiveFillEvidenceTracker()
    order = make_order(1, "buy", "100")
    book = make_book(bids=[("99", "5")], asks=[("100", "5")])

    evidence = tracker.observe([order], book, BASE_TIME)[0]

    assert evidence.crossed is True


def test_sell_order_crossed_by_best_bid():
    tracker = PassiveFillEvidenceTracker()
    order = make_order(1, "sell", "101")
    book = make_book(bids=[("101", "5")], asks=[("102", "5")])

    evidence = tracker.observe([order], book, BASE_TIME)[0]

    assert evidence.crossed is True


def test_same_side_quantity_decrease_is_ambiguous_evidence():
    tracker = PassiveFillEvidenceTracker()
    order = make_order(1, "buy", "100")
    first_book = make_book(bids=[("100", "10")], asks=[("101", "5")])
    second_book = make_book(bids=[("100", "6")], asks=[("101", "5")])
    tracker.observe([order], first_book, BASE_TIME)

    evidence = tracker.observe(
        [order],
        second_book,
        BASE_TIME + timedelta(seconds=2),
    )[0]

    assert evidence.previous_level_quantity == Decimal("10")
    assert evidence.current_level_quantity == Decimal("6")
    assert evidence.level_quantity_decreased is True
    assert evidence.level_disappeared is False


def test_same_side_level_disappearance_is_ambiguous_evidence():
    tracker = PassiveFillEvidenceTracker()
    order = make_order(1, "sell", "101")
    first_book = make_book(bids=[("100", "5")], asks=[("101", "10")])
    second_book = make_book(bids=[("100", "5")], asks=[("102", "5")])
    tracker.observe([order], first_book, BASE_TIME)

    evidence = tracker.observe([order], second_book, BASE_TIME)[0]

    assert evidence.previous_level_quantity == Decimal("10")
    assert evidence.current_level_quantity is None
    assert evidence.same_side_level_present is False
    assert evidence.level_disappeared is True


def test_unchanged_quantity_has_no_ambiguous_signal():
    tracker = PassiveFillEvidenceTracker()
    order = make_order(1, "buy", "100")
    book = make_book(bids=[("100", "10")], asks=[("101", "5")])
    tracker.observe([order], book, BASE_TIME)

    evidence = tracker.observe([order], book, BASE_TIME)[0]

    assert evidence.level_quantity_decreased is False
    assert evidence.level_disappeared is False


def test_first_observation_has_no_false_change_signal():
    tracker = PassiveFillEvidenceTracker()
    order = make_order(1, "buy", "100")
    book = make_book(bids=[("99", "5")], asks=[("101", "5")])

    evidence = tracker.observe([order], book, BASE_TIME)[0]

    assert evidence.previous_level_quantity is None
    assert evidence.current_level_quantity is None
    assert evidence.level_quantity_decreased is False
    assert evidence.level_disappeared is False


def test_multiple_orders_keep_independent_state():
    tracker = PassiveFillEvidenceTracker()
    first = make_order(1, "buy", "100")
    second = make_order(2, "buy", "99")
    initial = make_book(
        bids=[("100", "10"), ("99", "20")],
        asks=[("101", "5")],
    )
    changed = make_book(
        bids=[("100", "6"), ("99", "20")],
        asks=[("101", "5")],
    )
    tracker.observe([first, second], initial, BASE_TIME)

    evidence = tracker.observe([first, second], changed, BASE_TIME)
    by_order_id = {item.order_id: item for item in evidence}

    assert by_order_id[1].level_quantity_decreased is True
    assert by_order_id[2].level_quantity_decreased is False


def test_state_is_removed_after_order_closes():
    tracker = PassiveFillEvidenceTracker()
    order = make_order(1, "buy", "100")
    initial = make_book(bids=[("100", "10")], asks=[("101", "5")])
    changed = make_book(bids=[("100", "5")], asks=[("101", "5")])
    tracker.synchronize([order], initial, BASE_TIME)
    order.status = "cancelled"

    tracker.synchronize([order], changed, BASE_TIME)

    assert tracker.tracked_order_ids == frozenset()

    replacement = make_order(1, "buy", "100")
    evidence = tracker.observe([replacement], changed, BASE_TIME)[0]
    assert evidence.previous_level_quantity is None
    assert evidence.level_quantity_decreased is False


def test_reset_clears_all_state():
    tracker = PassiveFillEvidenceTracker()
    order = make_order(1, "buy", "100")
    book = make_book(bids=[("100", "10")], asks=[("101", "5")])
    tracker.synchronize([order], book, BASE_TIME)

    tracker.reset()

    assert tracker.tracked_order_ids == frozenset()


def test_age_is_utc_aware_and_never_negative():
    tracker = PassiveFillEvidenceTracker()
    order = make_order(1, "buy", "100")
    book = make_book(bids=[("100", "10")], asks=[("101", "5")])
    tracker.synchronize([order], book, BASE_TIME + timedelta(seconds=5))

    evidence = tracker.observe([order], book, BASE_TIME)[0]

    assert evidence.observed_at.tzinfo == timezone.utc
    assert evidence.age_seconds == Decimal("0.0")


def test_evidence_is_immutable_and_never_changes_trading_state():
    portfolio = PortfolioManager(initial_cash=Decimal("150"))
    broker = ConservativePaperBroker(portfolio=portfolio)
    order = make_order(1, "buy", "100")
    broker.open_orders.append(order)
    tracker = PassiveFillEvidenceTracker()
    book = make_book(bids=[("99", "5")], asks=[("100", "5")])
    starting_state = (
        portfolio.cash_balance,
        portfolio.base_position,
        portfolio.total_volume,
    )

    evidence = tracker.observe(broker.open_orders, book, BASE_TIME)[0]

    assert evidence.crossed is True
    assert broker.fills == []
    assert order.status == "open"
    assert (
        portfolio.cash_balance,
        portfolio.base_position,
        portfolio.total_volume,
    ) == starting_state

    with pytest.raises(FrozenInstanceError):
        evidence.crossed = False
