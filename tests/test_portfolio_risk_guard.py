from decimal import Decimal

import pytest

from bot.portfolio.portfolio_manager import PortfolioManager
from bot.risk.portfolio_risk_guard import (
    PortfolioRiskGuard,
    PortfolioRiskLimits,
)


def make_portfolio_at_price(price: Decimal) -> PortfolioManager:
    portfolio = PortfolioManager(initial_cash=Decimal("150"))
    portfolio.buy(price=Decimal("10"), quantity=Decimal("10"))
    portfolio.update_market_price(price)
    return portfolio


def test_drawdown_below_hard_but_above_preemptive_threshold_halts_entries():
    guard = PortfolioRiskGuard()
    portfolio = make_portfolio_at_price(Decimal("8.60"))

    decision = guard.evaluate(portfolio)

    assert portfolio.drawdown < Decimal("0.10")
    assert decision.allowed is False
    assert decision.reason == "preemptive_drawdown_halt"
    assert decision.latched is False
    assert decision.entry_halt_latched is True


def test_drawdown_below_preemptive_threshold_is_allowed():
    guard = PortfolioRiskGuard()
    portfolio = make_portfolio_at_price(Decimal("8.90"))

    decision = guard.evaluate(portfolio)

    assert portfolio.drawdown < Decimal("0.08")
    assert decision.allowed is True
    assert decision.reason == "ok"


def test_drawdown_exactly_at_threshold_triggers_stop():
    guard = PortfolioRiskGuard()
    portfolio = make_portfolio_at_price(Decimal("8.50"))

    decision = guard.evaluate(portfolio)

    assert portfolio.drawdown == Decimal("0.10")
    assert decision.allowed is False
    assert decision.reason == "max_drawdown_reached"
    assert decision.latched is True
    assert decision.max_drawdown == Decimal("0.10")


def test_drawdown_above_threshold_triggers_stop():
    guard = PortfolioRiskGuard()
    portfolio = make_portfolio_at_price(Decimal("8.40"))

    decision = guard.evaluate(portfolio)

    assert portfolio.drawdown > Decimal("0.10")
    assert decision.allowed is False
    assert decision.latched is True


def test_kill_switch_remains_latched_after_recovery():
    guard = PortfolioRiskGuard()
    portfolio = make_portfolio_at_price(Decimal("8.50"))
    guard.evaluate(portfolio)

    portfolio.update_market_price(Decimal("10"))
    decision = guard.evaluate(portfolio)

    assert portfolio.drawdown == Decimal("0")
    assert decision.allowed is False
    assert decision.reason == "max_drawdown_latched"
    assert decision.latched is True


def test_explicit_reset_clears_latch():
    guard = PortfolioRiskGuard()
    portfolio = make_portfolio_at_price(Decimal("8.50"))
    guard.evaluate(portfolio)
    portfolio.update_market_price(Decimal("10"))

    guard.reset()
    decision = guard.evaluate(portfolio)

    assert decision.allowed is True
    assert decision.reason == "ok"
    assert decision.latched is False


def test_projected_risk_budget_requires_nonzero_assumptions_and_headroom():
    guard = PortfolioRiskGuard()
    portfolio = make_portfolio_at_price(Decimal("10"))

    allowed, reason = guard.projected_order_allowed(
        portfolio,
        side="buy",
        notional=Decimal("1"),
        adverse_move_ratio=None,
    )
    assert allowed is False
    assert reason == "risk_budget_assumptions_required"

    allowed, reason = guard.projected_order_allowed(
        portfolio,
        side="buy",
        notional=Decimal("1"),
        adverse_move_ratio=Decimal("0.02"),
        slippage_ratio=Decimal("0.01"),
        fee_ratio=Decimal("0.001"),
    )
    assert allowed is True
    assert reason == "risk_budget_approved"


@pytest.mark.parametrize("max_drawdown", [Decimal("-0.01"), Decimal("1.01")])
def test_portfolio_risk_limits_reject_invalid_drawdown(max_drawdown):
    with pytest.raises(ValueError, match="between 0 and 1"):
        PortfolioRiskLimits(max_drawdown=max_drawdown)
