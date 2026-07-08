from decimal import Decimal

from bot.core.paper_trading_engine import PaperTradingEngine
from bot.execution.execution_manager import ExecutionManager
from bot.execution.paper_broker import PaperBroker
from bot.market.market_cache import MarketCache
from bot.market.models import OrderBook, OrderBookLevel
from bot.portfolio.portfolio_manager import PortfolioManager
from bot.risk.risk_manager import RiskManager
from bot.strategy.passive_market_maker import PassiveMarketMakerStrategy


SYMBOL = "SOMI:USDso"


def build_market() -> MarketCache:
    market = MarketCache()

    market.update_orderbook(
        OrderBook(
            symbol=SYMBOL,
            bids=[
                OrderBookLevel(price=Decimal("1.00"), quantity=Decimal("100")),
            ],
            asks=[
                OrderBookLevel(price=Decimal("1.02"), quantity=Decimal("100")),
            ],
            timestamp=12345,
        )
    )

    return market


def main() -> None:
    market = build_market()

    portfolio = PortfolioManager(initial_cash=Decimal("150"))
    risk = RiskManager()
    execution = ExecutionManager(portfolio=portfolio, risk_manager=risk)
    broker = PaperBroker(portfolio=portfolio)

    strategy = PassiveMarketMakerStrategy(
        symbol=SYMBOL,
        order_size_usd=Decimal("5"),
    )

    engine = PaperTradingEngine(
        symbol=SYMBOL,
        market=market,
        portfolio=portfolio,
        strategy=strategy,
        execution=execution,
        broker=broker,
    )

    print("=" * 60)
    print("PAPER TRADING DEMO")
    print("=" * 60)

    for step in range(1, 4):
        result = engine.step()

        print()
        print(f"Step {step}")
        print("-" * 60)
        print(f"Intents  : {len(result.intents)}")
        print(f"Decisions: {len(result.decisions)}")
        print(f"Fills    : {len(result.fills)}")

        for fill in result.fills:
            print(
                f"Fill: {fill.side.upper()} "
                f"{fill.quantity} {fill.symbol} "
                f"@ {fill.price} "
                f"notional={fill.notional}"
            )

        print()
        print(f"Cash balance : {portfolio.cash_balance}")
        print(f"Base position: {portfolio.base_position}")
        print(f"Avg entry    : {portfolio.average_entry_price}")
        print(f"Equity       : {portfolio.equity}")
        print(f"Realized PnL : {portfolio.realized_pnl}")
        print(f"Drawdown     : {portfolio.drawdown}")


if __name__ == "__main__":
    main()