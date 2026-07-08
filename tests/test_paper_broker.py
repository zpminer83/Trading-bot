from decimal import Decimal

from bot.execution.execution_manager import ExecutionManager
from bot.execution.order import OrderIntent
from bot.execution.paper_broker import PaperBroker
from bot.portfolio.portfolio_manager import PortfolioManager
from bot.risk.risk_manager import RiskManager


def test_paper_broker_executes_approved_buy_order():
    portfolio = PortfolioManager(initial_cash=Decimal("150"))
    risk = RiskManager()
    execution = ExecutionManager(portfolio=portfolio, risk_manager=risk)
    broker = PaperBroker(portfolio=portfolio)

    intent = OrderIntent(
        symbol="SOMI:USDso",
        side="buy",
        order_type="limit",
        price=Decimal("1"),
        quantity=Decimal("5"),
    )

    decision = execution.review_order(intent)
    fill = broker.execute(decision)

    assert fill is not None
    assert fill.side == "buy"
    assert fill.notional == Decimal("5")

    assert portfolio.cash_balance == Decimal("145")
    assert portfolio.base_position == Decimal("5")
    assert portfolio.average_entry_price == Decimal("1")


def test_paper_broker_does_not_execute_rejected_order():
    portfolio = PortfolioManager(initial_cash=Decimal("150"))
    risk = RiskManager()
    execution = ExecutionManager(portfolio=portfolio, risk_manager=risk)
    broker = PaperBroker(portfolio=portfolio)

    intent = OrderIntent(
        symbol="SOMI:USDso",
        side="buy",
        order_type="limit",
        price=Decimal("1"),
        quantity=Decimal("10"),
    )

    decision = execution.review_order(intent)
    fill = broker.execute(decision)

    assert decision.approved is False
    assert fill is None

    assert portfolio.cash_balance == Decimal("150")
    assert portfolio.base_position == Decimal("0")
    assert broker.fills == []


def test_paper_broker_executes_approved_sell_order():
    portfolio = PortfolioManager(initial_cash=Decimal("150"))

    portfolio.buy(price=Decimal("1"), quantity=Decimal("10"))

    risk = RiskManager()
    execution = ExecutionManager(portfolio=portfolio, risk_manager=risk)
    broker = PaperBroker(portfolio=portfolio)

    intent = OrderIntent(
        symbol="SOMI:USDso",
        side="sell",
        order_type="limit",
        price=Decimal("1.10"),
        quantity=Decimal("5"),
    )

    decision = execution.review_order(intent)
    fill = broker.execute(decision)

    assert fill is not None
    assert fill.side == "sell"
    assert fill.notional == Decimal("5.50")

    assert portfolio.base_position == Decimal("5")
    assert portfolio.cash_balance == Decimal("145.50")
    assert portfolio.realized_pnl == Decimal("0.50")