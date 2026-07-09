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
    timestamp: int = 1,
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
        )
    )


def make_engine(
    order_size_usd: Decimal = Decimal("5"),
    with_competition: bool = True,
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
        order_size_usd=order_size_usd,
    )

    competition = None

    if with_competition:
        competition = CompetitionTracker(now=utc_dt(2026, 7, 13))
        competition.set_pair_boost(
            symbol=SYMBOL,
            boost=Decimal("1.2"),
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
    )


def test_engine_submits_passive_buy_without_immediate_fill():
    engine = make_engine()

    set_market(
        market=engine.market,
        bid="1.00",
        ask="1.02",
    )

    result = engine.step(timestamp=utc_dt(2026, 7, 13, 12))

    assert result.mid_price == Decimal("1.01")
    assert len(result.fills) == 0
    assert len(result.intents) == 1
    assert len(result.decisions) == 1
    assert len(result.submitted_orders) == 1

    assert engine.portfolio.cash_balance == Decimal("150")
    assert engine.portfolio.base_position == Decimal("0")
    assert engine.order_manager.open_order_count == 1

    assert result.competition_snapshot is not None
    assert result.competition_snapshot.weekly_volume == Decimal("0")
    assert result.competition_snapshot.estimated_score == Decimal("0.0")


def test_engine_fills_previous_buy_when_ask_drops():
    engine = make_engine()

    set_market(
        market=engine.market,
        bid="1.00",
        ask="1.02",
        timestamp=1,
    )
    engine.step(timestamp=utc_dt(2026, 7, 13, 12))

    set_market(
        market=engine.market,
        bid="0.99",
        ask="1.00",
        timestamp=2,
    )
    result = engine.step(timestamp=utc_dt(2026, 7, 13, 12, 1))

    assert len(result.fills) == 1

    fill = result.fills[0]

    assert fill.side == "buy"
    assert fill.price == Decimal("1.00")
    assert fill.quantity == Decimal("5")
    assert fill.notional == Decimal("5.00")

    assert engine.portfolio.cash_balance == Decimal("145.00")
    assert engine.portfolio.base_position == Decimal("5")
    assert engine.order_manager.open_order_count == 2

    assert result.competition_snapshot is not None
    assert result.competition_snapshot.weekly_volume == Decimal("5.00")
    assert result.competition_snapshot.estimated_score == Decimal("6.000")


def test_engine_fills_sell_and_updates_competition_score():
    engine = make_engine()

    set_market(
        market=engine.market,
        bid="1.00",
        ask="1.02",
        timestamp=1,
    )
    engine.step(timestamp=utc_dt(2026, 7, 13, 12))

    set_market(
        market=engine.market,
        bid="0.99",
        ask="1.00",
        timestamp=2,
    )
    engine.step(timestamp=utc_dt(2026, 7, 13, 12, 1))

    set_market(
        market=engine.market,
        bid="1.02",
        ask="1.04",
        timestamp=3,
    )
    result = engine.step(timestamp=utc_dt(2026, 7, 13, 12, 2))

    assert len(result.fills) == 1

    fill = result.fills[0]

    assert fill.side == "sell"
    assert fill.price == Decimal("1.02")
    assert fill.quantity == Decimal("5")
    assert fill.notional == Decimal("5.10")

    assert engine.portfolio.cash_balance == Decimal("150.10")
    assert engine.portfolio.base_position == Decimal("0")
    assert engine.portfolio.realized_pnl == Decimal("0.10")
    assert engine.order_manager.open_order_count == 1

    assert result.competition_snapshot is not None
    assert result.competition_snapshot.weekly_volume == Decimal("10.10")
    assert result.competition_snapshot.estimated_score == Decimal("12.120")


def test_engine_handles_missing_market_data():
    engine = make_engine()

    result = engine.step(timestamp=utc_dt(2026, 7, 13, 12))

    assert result.mid_price is None
    assert result.intents == []
    assert result.decisions == []
    assert result.fills == []
    assert result.submitted_orders == []

    assert engine.portfolio.cash_balance == Decimal("150")
    assert engine.portfolio.base_position == Decimal("0")
    assert engine.order_manager.open_order_count == 0


def test_engine_can_run_without_competition_tracker():
    engine = make_engine(with_competition=False)

    set_market(
        market=engine.market,
        bid="1.00",
        ask="1.02",
    )

    result = engine.step(timestamp=utc_dt(2026, 7, 13, 12))

    assert len(result.submitted_orders) == 1
    assert result.competition_snapshot is None