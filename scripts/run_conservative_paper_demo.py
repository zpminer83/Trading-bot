from decimal import Decimal

from bot.execution.conservative_paper_broker import ConservativePaperBroker
from bot.execution.execution_manager import ExecutionManager
from bot.market.market_cache import MarketCache
from bot.market.models import OrderBook, OrderBookLevel
from bot.portfolio.portfolio_manager import PortfolioManager
from bot.risk.risk_manager import RiskManager
from bot.strategy.passive_market_maker import PassiveMarketMakerStrategy


SYMBOL = "SOMI:USDso"


def set_market(
    market: MarketCache,
    bid: str,
    ask: str,
    timestamp: int,
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


def main() -> None:
    market = MarketCache()

    portfolio = PortfolioManager(initial_cash=Decimal("150"))
    risk = RiskManager()
    execution = ExecutionManager(portfolio=portfolio, risk_manager=risk)
    broker = ConservativePaperBroker(portfolio=portfolio)

    strategy = PassiveMarketMakerStrategy(
        symbol=SYMBOL,
        order_size_usd=Decimal("5"),
    )

    scenarios = [
        ("1.00", "1.02"),  # normal market, passive buy should stay open
        ("0.99", "1.00"),  # ask drops, previous buy can fill
        ("1.02", "1.04"),  # bid rises, passive sell can fill
        ("1.01", "1.03"),  # stable market
    ]

    print("=" * 70)
    print("CONSERVATIVE PAPER TRADING DEMO")
    print("=" * 70)

    for index, (bid, ask) in enumerate(scenarios, start=1):
        set_market(
            market=market,
            bid=bid,
            ask=ask,
            timestamp=index,
        )

        mid_price = market.mid_price(SYMBOL)

        if mid_price is not None:
            portfolio.update_market_price(mid_price)

        fills = broker.process_market(market)

        intents = strategy.generate_orders(
            market=market,
            portfolio=portfolio,
        )

        decisions = []

        for intent in intents:
            decision = execution.review_order(intent)
            decisions.append(decision)

            if decision.approved:
                broker.submit(decision)

        print()
        print(f"Step {index}")
        print("-" * 70)
        print(f"Market bid/ask : {bid} / {ask}")
        print(f"Mid price      : {mid_price}")
        print(f"New intents    : {len(intents)}")
        print(f"Decisions      : {len(decisions)}")
        print(f"New fills      : {len(fills)}")
        print(f"Open orders    : {len(broker.open_orders)}")

        for decision in decisions:
            intent = decision.intent
            print(
                f"Decision: {intent.side.upper()} "
                f"price={intent.price} "
                f"qty={intent.quantity} "
                f"notional={intent.notional} "
                f"approved={decision.approved} "
                f"reason={decision.reason}"
            )

        for fill in fills:
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
        print(f"Unrealized   : {portfolio.unrealized_pnl}")
        print(f"Drawdown     : {portfolio.drawdown}")
        print(f"Total volume : {portfolio.total_volume}")


if __name__ == "__main__":
    main()