from decimal import Decimal

from bot.execution.execution_manager import ExecutionManager
from bot.execution.order import OrderIntent
from bot.portfolio.portfolio_manager import PortfolioManager
from bot.risk.risk_manager import RiskManager


def test_execution_manager_approves_safe_buy_order():
    portfolio = PortfolioManager(initial_cash=Decimal("150"))
    risk = RiskManager()
    execution = ExecutionManager(portfolio=portfolio, risk_manager=risk)

    intent = OrderIntent(
        symbol="SOMI:USDso",
        side="buy",
        order_type="limit",
        price=Decimal("1"),
        quantity=Decimal("5"),
    )

    decision = execution.review_order(intent)

    assert decision.approved is True
    assert decision.reason == "approved"
    assert decision.intent == intent


def test_execution_manager_rejects_large_buy_order():
    portfolio = PortfolioManager(initial_cash=Decimal("150"))
    risk = RiskManager()
    execution = ExecutionManager(portfolio=portfolio, risk_manager=risk)

    intent = OrderIntent(
        symbol="SOMI:USDso",
        side="buy",
        order_type="limit",
        price=Decimal("1"),
        quantity=Decimal("10"),
    )

    decision = execution.review_order(intent)

    assert decision.approved is False
    assert decision.reason == "order_notional_exceeds_risk_limit"


def test_execution_manager_rejects_sell_without_position():
    portfolio = PortfolioManager(initial_cash=Decimal("150"))
    risk = RiskManager()
    execution = ExecutionManager(portfolio=portfolio, risk_manager=risk)

    intent = OrderIntent(
        symbol="SOMI:USDso",
        side="sell",
        order_type="limit",
        price=Decimal("1"),
        quantity=Decimal("1"),
    )

    decision = execution.review_order(intent)

    assert decision.approved is False
    assert decision.reason == "no_position_to_sell"