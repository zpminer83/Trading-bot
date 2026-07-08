from decimal import Decimal

from bot.core.paper_trading_engine import PaperTradingEngine
from bot.execution.execution_manager import ExecutionManager
from bot.execution.paper_broker import PaperBroker
from bot.market.market_cache import MarketCache
from bot.market.models import OrderBook, OrderBookLevel
from bot.portfolio.portfolio_manager import PortfolioManager
from bot.risk.risk_manager import RiskManager
from bot.strategy.passive_market_maker import PassiveMarketMakerStrategy


def make_market() -> MarketCache:
    market = MarketCache()

    market.update_orderbook(
        OrderBook(
            symbol="SOMI:USDso",
            bids=[
                OrderBookLevel(
                    price=Decimal("1.00"),
                    quantity=Decimal("100"),
                )
            ],
            asks=[
                OrderBookLevel(
                    price=Decimal("1.02"),
                    quantity=Decimal("100"),
                )
            ],
            timestamp=12345,
        )
    )

    return market


def make_engine(order_size_usd: Decimal = Decimal("5")) -> PaperTradingEngine:
    symbol = "SOMI:USDso"

    market = make_market()
    portfolio = PortfolioManager(initial_cash=Decimal("150"))
    risk = RiskManager()
    execution = ExecutionManager(portfolio=portfolio, risk_manager=risk)
    broker = PaperBroker(portfolio=portfolio)

    strategy = PassiveMarketMakerStrategy(
        symbol=symbol,
        order_size_usd=order_size_usd,
    )

    return PaperTradingEngine(
        symbol=symbol,
        market=market,
        portfolio=portfolio,
        strategy=strategy,
        execution=execution,
        broker=broker,
    )


def test_paper_trading_engine_executes_safe_buy():
    engine = make_engine()

    result = engine.step()

    assert len(result.intents) == 1
    assert len(result.decisions) == 1
    assert len(result.fills) == 1

    fill = result.fills[0]

    assert fill.side == "buy"
    assert fill.notional == Decimal("5")

    assert engine.portfolio.cash_balance == Decimal("145")
    assert engine.portfolio.base_position == Decimal("5")


def test_paper_trading_engine_rejects_oversized_buy():
    engine = make_engine(order_size_usd=Decimal("10"))

    result = engine.step()

    assert len(result.intents) == 1
    assert len(result.decisions) == 1
    assert len(result.fills) == 0

    assert result.decisions[0].approved is False
    assert result.decisions[0].reason == "order_notional_exceeds_risk_limit"

    assert engine.portfolio.cash_balance == Decimal("150")
    assert engine.portfolio.base_position == Decimal("0")


def test_paper_trading_engine_returns_no_orders_without_market_data():
    symbol = "SOMI:USDso"

    market = MarketCache()
    portfolio = PortfolioManager(initial_cash=Decimal("150"))
    risk = RiskManager()
    execution = ExecutionManager(portfolio=portfolio, risk_manager=risk)
    broker = PaperBroker(portfolio=portfolio)

    strategy = PassiveMarketMakerStrategy(
        symbol=symbol,
        order_size_usd=Decimal("5"),
    )

    engine = PaperTradingEngine(
        symbol=symbol,
        market=market,
        portfolio=portfolio,
        strategy=strategy,
        execution=execution,
        broker=broker,
    )

    result = engine.step()

    assert result.intents == []
    assert result.decisions == []
    assert result.fills == []