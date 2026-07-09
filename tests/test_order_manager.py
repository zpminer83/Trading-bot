from decimal import Decimal

import pytest

from bot.execution.conservative_paper_broker import ConservativePaperBroker
from bot.execution.order import OrderDecision, OrderIntent
from bot.execution.order_manager import OrderManager
from bot.portfolio.portfolio_manager import PortfolioManager


SYMBOL = "SOMI:USDso"


def make_intent(
    side: str = "buy",
    price: str = "1.00",
    quantity: str = "5",
) -> OrderIntent:
    return OrderIntent(
        symbol=SYMBOL,
        side=side,
        order_type="limit",
        price=Decimal(price),
        quantity=Decimal(quantity),
    )


def make_decision(
    approved: bool = True,
    reason: str = "approved",
    side: str = "buy",
    price: str = "1.00",
    quantity: str = "5",
) -> OrderDecision:
    return OrderDecision(
        approved=approved,
        reason=reason,
        intent=make_intent(
            side=side,
            price=price,
            quantity=quantity,
        ),
    )


def make_manager(max_open_orders: int = 2) -> OrderManager:
    portfolio = PortfolioManager(initial_cash=Decimal("150"))
    broker = ConservativePaperBroker(portfolio=portfolio)

    return OrderManager(
        broker=broker,
        max_open_orders=max_open_orders,
    )


def test_order_manager_submits_approved_orders():
    manager = make_manager(max_open_orders=2)

    decisions = [
        make_decision(side="buy", price="1.00"),
        make_decision(side="sell", price="1.02"),
    ]

    submitted = manager.replace_orders(decisions)

    assert len(submitted) == 2
    assert manager.open_order_count == 2


def test_order_manager_skips_rejected_orders():
    manager = make_manager(max_open_orders=2)

    decisions = [
        make_decision(
            approved=False,
            reason="risk_rejected",
            side="buy",
            price="1.00",
        )
    ]

    submitted = manager.replace_orders(decisions)

    assert submitted == []
    assert manager.open_order_count == 0


def test_order_manager_replaces_existing_orders():
    manager = make_manager(max_open_orders=2)

    first_submitted = manager.replace_orders(
        [
            make_decision(side="buy", price="1.00"),
        ]
    )

    old_order = first_submitted[0]

    assert manager.open_order_count == 1
    assert old_order.status == "open"

    second_submitted = manager.replace_orders(
        [
            make_decision(side="buy", price="1.01"),
        ]
    )

    assert old_order.status == "cancelled"
    assert len(second_submitted) == 1
    assert manager.open_order_count == 1
    assert manager.broker.open_orders[0].intent.price == Decimal("1.01")


def test_order_manager_respects_max_open_orders():
    manager = make_manager(max_open_orders=1)

    decisions = [
        make_decision(side="buy", price="1.00"),
        make_decision(side="sell", price="1.02"),
    ]

    submitted = manager.replace_orders(decisions)

    assert len(submitted) == 1
    assert manager.open_order_count == 1
    assert manager.broker.open_orders[0].intent.side == "buy"


def test_order_manager_rejects_invalid_max_open_orders():
    portfolio = PortfolioManager(initial_cash=Decimal("150"))
    broker = ConservativePaperBroker(portfolio=portfolio)

    with pytest.raises(ValueError):
        OrderManager(
            broker=broker,
            max_open_orders=0,
        )