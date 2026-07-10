from datetime import datetime, timedelta, timezone
from decimal import Decimal

from bot.competition.competition_tracker import CompetitionTracker
from bot.core.conservative_paper_trading_engine import (
    ConservativePaperTradingEngine,
)
from bot.execution.conservative_paper_broker import (
    ConservativePaperBroker,
)
from bot.execution.execution_manager import ExecutionManager
from bot.execution.order_manager import OrderManager
from bot.market.market_cache import MarketCache
from bot.market.models import OrderBook, OrderBookLevel
from bot.portfolio.portfolio_manager import PortfolioManager
from bot.risk.market_freshness import (
    MarketFreshnessGuard,
    MarketFreshnessLimits,
)
from bot.risk.market_safety import (
    MarketSafety,
    MarketSafetyLimits,
)
from bot.risk.risk_manager import RiskManager
from bot.strategy.passive_market_maker import (
    PassiveMarketMakerStrategy,
)


SYMBOL = "SOMI:USDso"

BASE_TIME = datetime(
    2026,
    7,
    13,
    12,
    0,
    tzinfo=timezone.utc,
)


def unix_seconds(value: datetime) -> int:
    return int(value.timestamp())


def set_market(
    market: MarketCache,
    *,
    timestamp: int,
    nonce: str = "1",
    bid: str = "1.00",
    ask: str = "1.01",
) -> None:
    market.update_orderbook(
        OrderBook(
            symbol=SYMBOL,
            bids=[
                OrderBookLevel(
                    price=Decimal(bid),
                    quantity=Decimal("100"),
                )
            ],
            asks=[
                OrderBookLevel(
                    price=Decimal(ask),
                    quantity=Decimal("100"),
                )
            ],
            timestamp=timestamp,
            nonce=nonce,
        )
    )


def make_engine(
    max_exchange_age_seconds: Decimal = Decimal("30"),
    max_unchanged_seconds: Decimal = Decimal("5"),
) -> ConservativePaperTradingEngine:
    market = MarketCache()
    portfolio = PortfolioManager(
        initial_cash=Decimal("150"),
    )

    risk = RiskManager()

    execution = ExecutionManager(
        portfolio=portfolio,
        risk_manager=risk,
    )

    broker = ConservativePaperBroker(
        portfolio=portfolio,
    )

    order_manager = OrderManager(
        broker=broker,
        max_open_orders=2,
    )

    strategy = PassiveMarketMakerStrategy(
        symbol=SYMBOL,
        order_size_usd=Decimal("5"),
    )

    competition = CompetitionTracker(
        now=BASE_TIME,
    )

    market_safety = MarketSafety(
        limits=MarketSafetyLimits(
            max_spread_percent=Decimal("0.02"),
            min_best_bid_quantity=Decimal("1"),
            min_best_ask_quantity=Decimal("1"),
        )
    )

    market_freshness = MarketFreshnessGuard(
        limits=MarketFreshnessLimits(
            max_exchange_age_seconds=max_exchange_age_seconds,
            max_unchanged_seconds=max_unchanged_seconds,
            max_future_skew_seconds=Decimal("5"),
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
        market_freshness=market_freshness,
    )


def test_engine_generates_order_when_market_data_is_fresh():
    engine = make_engine()

    set_market(
        engine.market,
        timestamp=unix_seconds(BASE_TIME),
        nonce="1",
    )

    result = engine.step(
        timestamp=BASE_TIME + timedelta(seconds=2),
    )

    assert result.market_freshness_decision is not None
    assert result.market_freshness_decision.fresh is True
    assert result.market_freshness_decision.reason == "ok"

    assert result.market_safety_decision is not None
    assert result.market_safety_decision.safe is True

    assert len(result.intents) == 1
    assert len(result.submitted_orders) == 1
    assert engine.order_manager.open_order_count == 1


def test_engine_blocks_orders_when_timestamp_is_stale():
    engine = make_engine(
        max_exchange_age_seconds=Decimal("5"),
    )

    set_market(
        engine.market,
        timestamp=unix_seconds(BASE_TIME),
        nonce="1",
    )

    result = engine.step(
        timestamp=BASE_TIME + timedelta(seconds=10),
    )

    assert result.market_freshness_decision is not None
    assert result.market_freshness_decision.fresh is False
    assert result.market_freshness_decision.reason == "stale_timestamp"
    assert result.portfolio_risk_decision is None

    assert result.intents == []
    assert result.decisions == []
    assert result.fills == []
    assert result.submitted_orders == []
    assert engine.order_manager.open_order_count == 0


def test_engine_cancels_order_when_snapshot_stops_updating():
    engine = make_engine(
        max_exchange_age_seconds=Decimal("30"),
        max_unchanged_seconds=Decimal("5"),
    )

    set_market(
        engine.market,
        timestamp=unix_seconds(BASE_TIME),
        nonce="1",
    )

    first_result = engine.step(
        timestamp=BASE_TIME,
    )

    assert len(first_result.submitted_orders) == 1

    old_order = first_result.submitted_orders[0]

    assert old_order.status == "open"
    assert engine.order_manager.open_order_count == 1

    second_result = engine.step(
        timestamp=BASE_TIME + timedelta(seconds=6),
    )

    assert second_result.market_freshness_decision is not None
    assert second_result.market_freshness_decision.fresh is False
    assert (
        second_result.market_freshness_decision.reason
        == "repeated_snapshot"
    )

    assert old_order.status == "cancelled"
    assert engine.order_manager.open_order_count == 0

    assert second_result.intents == []
    assert second_result.decisions == []
    assert second_result.fills == []
    assert second_result.submitted_orders == []


def test_engine_accepts_updated_snapshot_and_replaces_order():
    engine = make_engine()

    set_market(
        engine.market,
        timestamp=unix_seconds(BASE_TIME),
        nonce="1",
        bid="1.00",
        ask="1.01",
    )

    first_result = engine.step(
        timestamp=BASE_TIME,
    )

    old_order = first_result.submitted_orders[0]

    set_market(
        engine.market,
        timestamp=unix_seconds(
            BASE_TIME + timedelta(seconds=1)
        ),
        nonce="2",
        bid="1.01",
        ask="1.02",
    )

    second_result = engine.step(
        timestamp=BASE_TIME + timedelta(seconds=1),
    )

    assert second_result.market_freshness_decision is not None
    assert second_result.market_freshness_decision.fresh is True
    assert second_result.market_freshness_decision.reason == "ok"
    assert second_result.market_freshness_decision.nonce_changed is True

    assert old_order.status == "cancelled"
    assert len(second_result.submitted_orders) == 1
    assert engine.order_manager.open_order_count == 1

    new_order = engine.broker.open_orders[0]

    assert new_order.intent.price == Decimal("1.01")
    assert new_order.status == "open"


def test_engine_blocks_regressing_market_timestamp():
    engine = make_engine()

    set_market(
        engine.market,
        timestamp=unix_seconds(BASE_TIME),
        nonce="10",
    )

    first_result = engine.step(
        timestamp=BASE_TIME,
    )

    old_order = first_result.submitted_orders[0]

    set_market(
        engine.market,
        timestamp=unix_seconds(
            BASE_TIME - timedelta(seconds=1)
        ),
        nonce="9",
    )

    second_result = engine.step(
        timestamp=BASE_TIME + timedelta(seconds=1),
    )

    assert second_result.market_freshness_decision is not None
    assert second_result.market_freshness_decision.fresh is False
    assert (
        second_result.market_freshness_decision.reason
        == "timestamp_regressed"
    )

    assert old_order.status == "cancelled"
    assert engine.order_manager.open_order_count == 0
    assert second_result.submitted_orders == []
