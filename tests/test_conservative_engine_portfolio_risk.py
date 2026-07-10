from datetime import datetime, timezone
from decimal import Decimal

import pytest

from bot.core.conservative_paper_trading_engine import (
    ConservativePaperTradingEngine,
)
from bot.execution.conservative_paper_broker import ConservativePaperBroker
from bot.execution.execution_manager import ExecutionManager
from bot.execution.order_manager import OrderManager
from bot.market.market_cache import MarketCache
from bot.market.models import OrderBook, OrderBookLevel
from bot.portfolio.portfolio_manager import PortfolioManager
from bot.risk.portfolio_risk_guard import PortfolioRiskGuard
from bot.risk.risk_manager import RiskManager
from bot.strategy.passive_market_maker import PassiveMarketMakerStrategy


SYMBOL = "SOMI:USDso"
NOW = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)


def set_market(market: MarketCache, bid: str, ask: str) -> None:
    market.update_orderbook(
        OrderBook(
            symbol=SYMBOL,
            bids=[OrderBookLevel(price=Decimal(bid), quantity=Decimal("100"))],
            asks=[OrderBookLevel(price=Decimal(ask), quantity=Decimal("100"))],
            timestamp=1,
        )
    )


def make_engine() -> ConservativePaperTradingEngine:
    market = MarketCache()
    portfolio = PortfolioManager(initial_cash=Decimal("150"))
    portfolio.buy(price=Decimal("10"), quantity=Decimal("10"))
    risk = RiskManager()
    execution = ExecutionManager(portfolio=portfolio, risk_manager=risk)
    broker = ConservativePaperBroker(portfolio=portfolio)
    order_manager = OrderManager(broker=broker, max_open_orders=2)
    strategy = PassiveMarketMakerStrategy(symbol=SYMBOL, order_size_usd=Decimal("5"))

    return ConservativePaperTradingEngine(
        symbol=SYMBOL,
        market=market,
        portfolio=portfolio,
        strategy=strategy,
        execution=execution,
        broker=broker,
        order_manager=order_manager,
        portfolio_risk_guard=PortfolioRiskGuard(),
    )


def test_engine_kill_switch_cancels_orders_before_fills_or_new_intents(
    monkeypatch,
):
    engine = make_engine()
    set_market(engine.market, bid="9.99", ask="10.01")

    first_result = engine.step(timestamp=NOW)

    assert first_result.portfolio_risk_decision is not None
    assert first_result.portfolio_risk_decision.allowed is True
    assert engine.order_manager.open_order_count > 0
    old_orders = list(engine.broker.open_orders)

    def fail_if_called(*args, **kwargs):
        pytest.fail("fill or strategy processing must not run after risk stop")

    monkeypatch.setattr(engine.broker, "process_market", fail_if_called)
    monkeypatch.setattr(engine.strategy, "generate_orders", fail_if_called)
    set_market(engine.market, bid="8.49", ask="8.51")

    result = engine.step(timestamp=NOW)

    assert result.portfolio_risk_decision is not None
    assert result.portfolio_risk_decision.allowed is False
    assert result.portfolio_risk_decision.latched is True
    assert result.portfolio_risk_decision.drawdown == Decimal("0.10")
    assert result.fills == []
    assert result.intents == []
    assert result.decisions == []
    assert result.submitted_orders == []
    assert engine.order_manager.open_order_count == 0
    assert all(order.status == "cancelled" for order in old_orders)
