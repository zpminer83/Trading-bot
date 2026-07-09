from decimal import Decimal

from bot.execution.conservative_paper_broker import ConservativePaperBroker
from bot.execution.execution_manager import ExecutionManager
from bot.execution.order_manager import OrderManager
from bot.market.market_cache import MarketCache
from bot.market.models import OrderBook, OrderBookLevel
from bot.portfolio.portfolio_manager import PortfolioManager
from bot.risk.risk_manager import RiskManager
from bot.strategy.passive_market_maker import PassiveMarketMakerStrategy


SYMBOL = "SOMI:USDso"


def fmt_decimal(value: Decimal, places: str = "0.000000") -> str:
    quantized = value.quantize(Decimal(places))
    text = format(quantized, "f")
    return text.rstrip("0").rstrip(".") or "0"


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


def print_header() -> None:
    print("=" * 70)
    print("CONSERVATIVE PAPER TRADING DEMO")
    print("=" * 70)
    print("This demo uses:")
    print("- conservative fills")
    print("- order replacement")
    print("- max open orders protection")
    print("=" * 70)


def print_decisions(decisions) -> None:
    for decision in decisions:
        intent = decision.intent

        print(
            f"Decision: {intent.side.upper()} "
            f"price={fmt_decimal(intent.price, '0.0000')} "
            f"qty={fmt_decimal(intent.quantity)} "
            f"notional={fmt_decimal(intent.notional, '0.0000')} "
            f"approved={decision.approved} "
            f"reason={decision.reason}"
        )


def print_fills(fills) -> None:
    for fill in fills:
        print(
            f"Fill: {fill.side.upper()} "
            f"{fmt_decimal(fill.quantity)} {fill.symbol} "
            f"@ {fmt_decimal(fill.price, '0.0000')} "
            f"notional={fmt_decimal(fill.notional, '0.0000')}"
        )


def print_open_orders(broker: ConservativePaperBroker) -> None:
    if not broker.open_orders:
        print("Open order list: empty")
        return

    print("Open order list:")

    for order in broker.open_orders:
        intent = order.intent

        print(
            f"  #{order.order_id} "
            f"{intent.side.upper()} "
            f"price={fmt_decimal(intent.price, '0.0000')} "
            f"qty={fmt_decimal(intent.quantity)} "
            f"notional={fmt_decimal(intent.notional, '0.0000')} "
            f"status={order.status}"
        )


def print_portfolio(portfolio: PortfolioManager) -> None:
    print()
    print(f"Cash balance : {fmt_decimal(portfolio.cash_balance, '0.0000')}")
    print(f"Base position: {fmt_decimal(portfolio.base_position)}")
    print(f"Avg entry    : {fmt_decimal(portfolio.average_entry_price, '0.0000')}")
    print(f"Equity       : {fmt_decimal(portfolio.equity, '0.0000')}")
    print(f"Realized PnL : {fmt_decimal(portfolio.realized_pnl, '0.0000')}")
    print(f"Unrealized   : {fmt_decimal(portfolio.unrealized_pnl, '0.0000')}")
    print(f"Drawdown     : {fmt_decimal(portfolio.drawdown, '0.000000')}")
    print(f"Total volume : {fmt_decimal(portfolio.total_volume, '0.0000')}")


def main() -> None:
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

    scenarios = [
        ("1.00", "1.02"),  # Step 1: passive buy should stay open
        ("0.99", "1.00"),  # Step 2: ask drops, previous buy can fill
        ("1.02", "1.04"),  # Step 3: bid rises, passive sell can fill
        ("1.01", "1.03"),  # Step 4: old order gets replaced
    ]

    print_header()

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

        decisions = [
            execution.review_order(intent)
            for intent in intents
        ]

        submitted_orders = order_manager.replace_orders(decisions)

        print()
        print(f"Step {index}")
        print("-" * 70)
        print(f"Market bid/ask : {bid} / {ask}")
        print(f"Mid price      : {fmt_decimal(mid_price, '0.0000') if mid_price is not None else 'n/a'}")
        print(f"New intents    : {len(intents)}")
        print(f"Decisions      : {len(decisions)}")
        print(f"Submitted      : {len(submitted_orders)}")
        print(f"New fills      : {len(fills)}")
        print(f"Open orders    : {len(broker.open_orders)}")

        print_decisions(decisions)
        print_fills(fills)
        print_open_orders(broker)
        print_portfolio(portfolio)

    print()
    print("=" * 70)
    print("FINAL SUMMARY")
    print("=" * 70)
    print(f"Final cash     : {fmt_decimal(portfolio.cash_balance, '0.0000')}")
    print(f"Final position : {fmt_decimal(portfolio.base_position)}")
    print(f"Final equity   : {fmt_decimal(portfolio.equity, '0.0000')}")
    print(f"Realized PnL   : {fmt_decimal(portfolio.realized_pnl, '0.0000')}")
    print(f"Total volume   : {fmt_decimal(portfolio.total_volume, '0.0000')}")
    print(f"Open orders    : {len(broker.open_orders)}")
    print("=" * 70)


if __name__ == "__main__":
    main()