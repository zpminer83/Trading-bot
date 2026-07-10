from datetime import datetime, timezone
from decimal import Decimal

from bot.competition.competition_tracker import CompetitionTracker
from bot.core.conservative_paper_trading_engine import (
    ConservativePaperTradingEngine,
)
from bot.execution.conservative_paper_broker import ConservativePaperBroker
from bot.execution.execution_manager import ExecutionManager
from bot.execution.order_manager import OrderManager
from bot.market.market_cache import MarketCache
from bot.market.models import OrderBook, OrderBookLevel
from bot.portfolio.portfolio_manager import PortfolioManager
from bot.risk.market_safety import MarketSafety, MarketSafetyLimits
from bot.risk.risk_manager import RiskManager
from bot.strategy.passive_market_maker import PassiveMarketMakerStrategy


SYMBOL = "SOMI:USDso"


def utc_dt(
    year: int,
    month: int,
    day: int,
    hour: int = 0,
    minute: int = 0,
) -> datetime:
    return datetime(
        year,
        month,
        day,
        hour,
        minute,
        tzinfo=timezone.utc,
    )


def set_market(
    market: MarketCache,
    bid: str,
    ask: str,
    bid_qty: str = "100",
    ask_qty: str = "100",
    timestamp: int = 1,
) -> None:
    market.update_orderbook(
        OrderBook(
            symbol=SYMBOL,
            bids=[
                OrderBookLevel(
                    price=Decimal(bid),
                    quantity=Decimal(bid_qty),
                )
            ],
            asks=[
                OrderBookLevel(
                    price=Decimal(ask),
                    quantity=Decimal(ask_qty),
                )
            ],
            timestamp=timestamp,
        )
    )


def make_engine(
    max_spread_percent: Decimal = Decimal("0.02"),
    min_best_bid_quantity: Decimal = Decimal("1"),
    min_best_ask_quantity: Decimal = Decimal("1"),
) -> ConservativePaperTradingEngine:
    market = MarketCache()
    portfolio = PortfolioManager(initial_cash=Decimal("150"))
    risk = RiskManager()
    execution = ExecutionManager(portfolio=portfolio, risk_manager=risk)
    broker = ConservativePaperBroker(portfolio=portfolio)

    order_manager = OrderManager(
        broker=broker,
        max_open_orders=2,
    )

    strategy = PassiveMarketMakerStrategy(
        symbol=SYMBOL,
        order_size_usd=Decimal("5"),
    )

    competition = CompetitionTracker(now=utc_dt(2026, 7, 13))

    market_safety = MarketSafety(
        limits=MarketSafetyLimits(
            max_spread_percent=max_spread_percent,
            min_best_bid_quantity=min_best_bid_quantity,
            min_best_ask_quantity=min_best_ask_quantity,
        )
    )

    return ConservativePaperTradingEngine(
        symbol=SYMBOL,
        market=market,
        portfolio=portfolio,
        strategy=strategy,
        execution=execution,
        broker=broker,
        order_manager=order_manager,
        competition=competition,
        market_safety=market_safety,
    )


def test_engine_generates_orders_when_market_is_safe():
    engine = make_engine(
        max_spread_percent=Decimal("0.02"),
    )

    set_market(
        market=engine.market,
        bid="1.00",
        ask="1.01",
    )

    result = engine.step(timestamp=utc_dt(2026, 7, 13, 12))

    assert result.market_safety_decision is not None
    assert result.market_safety_decision.safe is True
    assert result.market_safety_decision.reason == "ok"

    assert len(result.intents) == 1
    assert len(result.decisions) == 1
    assert len(result.submitted_orders) == 1
    assert engine.order_manager.open_order_count == 1


def test_engine_blocks_new_orders_when_spread_is_too_wide():
    engine = make_engine(
        max_spread_percent=Decimal("0.02"),
    )

    set_market(
        market=engine.market,
        bid="1.00",
        ask="1.10",
    )

    result = engine.step(timestamp=utc_dt(2026, 7, 13, 12))

    assert result.market_safety_decision is not None
    assert result.market_safety_decision.safe is False
    assert result.market_safety_decision.reason == "spread_too_wide"
    assert result.portfolio_risk_decision is None

    assert result.intents == []
    assert result.decisions == []
    assert result.fills == []
    assert result.submitted_orders == []
    assert engine.order_manager.open_order_count == 0


def test_engine_cancels_existing_orders_when_market_becomes_unsafe():
    engine = make_engine(
        max_spread_percent=Decimal("0.02"),
    )

    set_market(
        market=engine.market,
        bid="1.00",
        ask="1.01",
        timestamp=1,
    )

    first_result = engine.step(timestamp=utc_dt(2026, 7, 13, 12))

    assert len(first_result.submitted_orders) == 1

    old_order = first_result.submitted_orders[0]

    assert old_order.status == "open"
    assert engine.order_manager.open_order_count == 1

    set_market(
        market=engine.market,
        bid="1.00",
        ask="1.20",
        timestamp=2,
    )

    second_result = engine.step(timestamp=utc_dt(2026, 7, 13, 12, 1))

    assert second_result.market_safety_decision is not None
    assert second_result.market_safety_decision.safe is False
    assert second_result.market_safety_decision.reason == "spread_too_wide"

    assert old_order.status == "cancelled"
    assert engine.order_manager.open_order_count == 0
    assert second_result.intents == []
    assert second_result.submitted_orders == []


def test_engine_blocks_orders_when_top_liquidity_is_too_low():
    engine = make_engine(
        min_best_bid_quantity=Decimal("10"),
        min_best_ask_quantity=Decimal("10"),
    )

    set_market(
        market=engine.market,
        bid="1.00",
        ask="1.01",
        bid_qty="100",
        ask_qty="0.5",
    )

    result = engine.step(timestamp=utc_dt(2026, 7, 13, 12))

    assert result.market_safety_decision is not None
    assert result.market_safety_decision.safe is False
    assert result.market_safety_decision.reason == "insufficient_ask_liquidity"

    assert result.intents == []
    assert result.decisions == []
    assert result.submitted_orders == []
    assert engine.order_manager.open_order_count == 0
