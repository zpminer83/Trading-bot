from decimal import Decimal

import pytest

from bot.portfolio.portfolio_manager import PortfolioManager


def test_initial_portfolio_state():
    portfolio = PortfolioManager(initial_cash=Decimal("150"))

    assert portfolio.cash_balance == Decimal("150")
    assert portfolio.base_position == Decimal("0")
    assert portfolio.average_entry_price == Decimal("0")
    assert portfolio.equity == Decimal("150")
    assert portfolio.drawdown == Decimal("0")


def test_buy_updates_position_and_cash():
    portfolio = PortfolioManager(initial_cash=Decimal("150"))

    portfolio.buy(price=Decimal("2"), quantity=Decimal("10"))

    assert portfolio.cash_balance == Decimal("130")
    assert portfolio.base_position == Decimal("10")
    assert portfolio.average_entry_price == Decimal("2")
    assert portfolio.total_volume == Decimal("20")
    assert portfolio.equity == Decimal("150")


def test_sell_updates_realized_pnl():
    portfolio = PortfolioManager(initial_cash=Decimal("150"))

    portfolio.buy(price=Decimal("2"), quantity=Decimal("10"))
    portfolio.sell(price=Decimal("2.5"), quantity=Decimal("4"))

    assert portfolio.cash_balance == Decimal("140")
    assert portfolio.base_position == Decimal("6")
    assert portfolio.average_entry_price == Decimal("2")
    assert portfolio.realized_pnl == Decimal("2.0")
    assert portfolio.total_volume == Decimal("30.0")


def test_unrealized_pnl_and_drawdown():
    portfolio = PortfolioManager(initial_cash=Decimal("150"))

    portfolio.buy(price=Decimal("2"), quantity=Decimal("10"))
    portfolio.update_market_price(Decimal("1.8"))

    assert portfolio.equity == Decimal("148.0")
    assert portfolio.unrealized_pnl == Decimal("-2.0")
    assert portfolio.drawdown == Decimal("2") / Decimal("150")


def test_cannot_sell_more_than_position():
    portfolio = PortfolioManager(initial_cash=Decimal("150"))

    portfolio.buy(price=Decimal("2"), quantity=Decimal("10"))

    with pytest.raises(ValueError):
        portfolio.sell(price=Decimal("2"), quantity=Decimal("11"))


def test_cannot_buy_more_than_cash_balance():
    portfolio = PortfolioManager(initial_cash=Decimal("150"))

    with pytest.raises(ValueError):
        portfolio.buy(price=Decimal("2"), quantity=Decimal("100"))