from decimal import Decimal

from bot.portfolio.portfolio_manager import PortfolioManager
from bot.risk.risk_manager import RiskManager, RiskMode


def make_portfolio_with_drawdown(price: Decimal) -> PortfolioManager:
    """
    Creates a portfolio where drawdown is caused by price movement,
    not by excessive position exposure.

    Initial cash: 150
    Buy: 5 units at 10 = 50 USD
    Initial exposure: 50 / 150 = 33.3%, below the 40% limit
    """
    portfolio = PortfolioManager(initial_cash=Decimal("150"))

    portfolio.buy(price=Decimal("10"), quantity=Decimal("5"))
    portfolio.update_market_price(price)

    return portfolio


def test_risk_mode_normal():
    portfolio = PortfolioManager(initial_cash=Decimal("150"))
    risk = RiskManager()

    decision = risk.evaluate(portfolio)

    assert decision.mode == RiskMode.NORMAL
    assert decision.allow_buy is True
    assert decision.allow_sell is True
    assert decision.max_order_notional == Decimal("5")


def test_risk_mode_caution_after_3_percent_drawdown():
    portfolio = make_portfolio_with_drawdown(Decimal("9.10"))
    risk = RiskManager()

    decision = risk.evaluate(portfolio)

    assert decision.mode == RiskMode.CAUTION
    assert decision.allow_buy is True
    assert decision.allow_sell is True
    assert decision.max_order_notional == Decimal("3.50")


def test_risk_mode_defensive_after_5_percent_drawdown():
    portfolio = make_portfolio_with_drawdown(Decimal("8.50"))
    risk = RiskManager()

    decision = risk.evaluate(portfolio)

    assert decision.mode == RiskMode.DEFENSIVE
    assert decision.allow_sell is True
    assert decision.max_order_notional == Decimal("2.00")


def test_risk_mode_survival_after_7_percent_drawdown():
    portfolio = make_portfolio_with_drawdown(Decimal("7.90"))
    risk = RiskManager()

    decision = risk.evaluate(portfolio)

    assert decision.mode == RiskMode.SURVIVAL
    assert decision.allow_buy is False
    assert decision.allow_sell is True
    assert decision.max_order_notional == Decimal("0")


def test_risk_mode_stop_after_10_percent_drawdown():
    portfolio = make_portfolio_with_drawdown(Decimal("7.00"))
    risk = RiskManager()

    decision = risk.evaluate(portfolio)

    assert decision.mode == RiskMode.STOP
    assert decision.allow_buy is False
    assert decision.allow_sell is False


def test_order_rejected_when_notional_too_large():
    portfolio = PortfolioManager(initial_cash=Decimal("150"))
    risk = RiskManager()

    approved, reason = risk.can_submit_order(
        portfolio=portfolio,
        side="buy",
        notional=Decimal("10"),
    )

    assert approved is False
    assert reason == "order_notional_exceeds_risk_limit"


def test_order_rejected_when_projected_position_exceeds_limit():
    portfolio = PortfolioManager(initial_cash=Decimal("150"))

    # Existing position value = 58 USD
    # Equity = 150 USD
    # Exposure = 38.6%, still below the 40% limit
    portfolio.buy(price=Decimal("10"), quantity=Decimal("5.8"))

    risk = RiskManager()

    # Additional 5 USD would push exposure above 40%
    approved, reason = risk.can_submit_order(
        portfolio=portfolio,
        side="buy",
        notional=Decimal("5"),
    )

    assert approved is False
    assert reason == "projected_position_exceeds_limit"