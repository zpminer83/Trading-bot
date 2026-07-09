import os
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
from bot.market.market_data_service import MarketDataService
from bot.portfolio.portfolio_manager import PortfolioManager
from bot.risk.risk_manager import RiskManager
from bot.strategy.passive_market_maker import PassiveMarketMakerStrategy
from scripts.check_dreamdex_orderbook_rest import (
    DEFAULT_BASE_URL,
    DEFAULT_DEPTH,
    DEFAULT_SYMBOL,
    build_orderbook_url,
    extract_orderbook_payload,
    fetch_json,
)


def env_decimal(name: str, default: str) -> Decimal:
    return Decimal(os.getenv(name, default))


def env_int(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


def fmt_decimal(value: Decimal | None, places: str = "0.000000") -> str:
    if value is None:
        return "n/a"

    quantized = value.quantize(Decimal(places))
    text = format(quantized, "f")

    return text.rstrip("0").rstrip(".") or "0"


def build_engine(
    symbol: str,
    market_cache: MarketCache,
    initial_cash: Decimal,
    order_size_usd: Decimal,
    max_open_orders: int,
    pair_boost: Decimal,
) -> ConservativePaperTradingEngine:
    portfolio = PortfolioManager(initial_cash=initial_cash)
    risk = RiskManager()
    execution = ExecutionManager(portfolio=portfolio, risk_manager=risk)
    broker = ConservativePaperBroker(portfolio=portfolio)

    order_manager = OrderManager(
        broker=broker,
        max_open_orders=max_open_orders,
    )

    competition = CompetitionTracker(
        now=datetime.now(timezone.utc),
    )

    competition.set_pair_boost(
        symbol=symbol,
        boost=pair_boost,
    )

    strategy = PassiveMarketMakerStrategy(
        symbol=symbol,
        order_size_usd=order_size_usd,
    )

    return ConservativePaperTradingEngine(
        symbol=symbol,
        market=market_cache,
        portfolio=portfolio,
        strategy=strategy,
        execution=execution,
        broker=broker,
        order_manager=order_manager,
        competition=competition,
    )


def print_market_snapshot(snapshot) -> None:
    print()
    print("Market snapshot:")
    print(f"Symbol   : {snapshot.symbol}")

    if snapshot.best_bid is not None:
        print(
            "Best bid : "
            f"{fmt_decimal(snapshot.best_bid.price, '0.000000')} "
            f"x {fmt_decimal(snapshot.best_bid.quantity, '0.000000')}"
        )
    else:
        print("Best bid : n/a")

    if snapshot.best_ask is not None:
        print(
            "Best ask : "
            f"{fmt_decimal(snapshot.best_ask.price, '0.000000')} "
            f"x {fmt_decimal(snapshot.best_ask.quantity, '0.000000')}"
        )
    else:
        print("Best ask : n/a")

    print(f"Mid price: {fmt_decimal(snapshot.mid_price, '0.000000')}")
    print(f"Spread   : {fmt_decimal(snapshot.spread, '0.000000')}")


def print_decisions(result) -> None:
    print()
    print("Paper decisions:")

    if not result.decisions:
        print("No decisions.")
        return

    for decision in result.decisions:
        intent = decision.intent

        print(
            f"{intent.side.upper()} "
            f"{intent.symbol} "
            f"price={fmt_decimal(intent.price, '0.000000')} "
            f"qty={fmt_decimal(intent.quantity, '0.000000')} "
            f"notional={fmt_decimal(intent.notional, '0.000000')} "
            f"approved={decision.approved} "
            f"reason={decision.reason}"
        )


def print_submitted_orders(engine: ConservativePaperTradingEngine) -> None:
    print()
    print("Open paper orders:")

    if not engine.broker.open_orders:
        print("No open paper orders.")
        return

    for order in engine.broker.open_orders:
        intent = order.intent

        print(
            f"#{order.order_id} "
            f"{intent.side.upper()} "
            f"{intent.symbol} "
            f"price={fmt_decimal(intent.price, '0.000000')} "
            f"qty={fmt_decimal(intent.quantity, '0.000000')} "
            f"notional={fmt_decimal(intent.notional, '0.000000')} "
            f"status={order.status}"
        )


def print_portfolio(engine: ConservativePaperTradingEngine) -> None:
    portfolio = engine.portfolio

    print()
    print("Paper portfolio:")
    print(f"Cash balance : {fmt_decimal(portfolio.cash_balance, '0.000000')}")
    print(f"Base position: {fmt_decimal(portfolio.base_position, '0.000000')}")
    print(f"Avg entry    : {fmt_decimal(portfolio.average_entry_price, '0.000000')}")
    print(f"Equity       : {fmt_decimal(portfolio.equity, '0.000000')}")
    print(f"Realized PnL : {fmt_decimal(portfolio.realized_pnl, '0.000000')}")
    print(f"Drawdown     : {fmt_decimal(portfolio.drawdown, '0.000000')}")


def print_competition(engine: ConservativePaperTradingEngine) -> None:
    competition = engine.competition

    if competition is None:
        return

    print()
    print("Competition estimate:")
    print(f"Weekly volume  : {fmt_decimal(competition.weekly_volume, '0.000000')}")
    print(f"Est. score     : {fmt_decimal(competition.estimated_score, '0.000000')}")
    print(f"Raffle tickets : {competition.raffle_tickets}")


def main() -> None:
    base_url = os.getenv("DREAMDEX_API_BASE_URL", DEFAULT_BASE_URL)
    symbol = os.getenv("DREAMDEX_SYMBOL", DEFAULT_SYMBOL)
    depth = env_int("DREAMDEX_DEPTH", DEFAULT_DEPTH)

    initial_cash = env_decimal("PAPER_INITIAL_CASH", "150")
    order_size_usd = env_decimal("PAPER_ORDER_SIZE_USD", "5")
    pair_boost = env_decimal("PAPER_PAIR_BOOST", "1")
    max_open_orders = env_int("PAPER_MAX_OPEN_ORDERS", 2)

    url = build_orderbook_url(
        base_url=base_url,
        symbol=symbol,
        depth=depth,
    )

    print("=" * 70)
    print("DREAMDEX REST PAPER ONCE")
    print("=" * 70)
    print("Mode    : READ-ONLY + PAPER")
    print("Warning : no real orders are sent")
    print(f"Base URL: {base_url}")
    print(f"Symbol  : {symbol}")
    print(f"Depth   : {depth}")
    print(f"Cash    : {fmt_decimal(initial_cash, '0.000000')}")
    print(f"Order $ : {fmt_decimal(order_size_usd, '0.000000')}")
    print(f"Boost   : {fmt_decimal(pair_boost, '0.000000')}")
    print("=" * 70)

    response = fetch_json(url)

    payload = extract_orderbook_payload(
        response=response,
        symbol=symbol,
    )

    market_cache = MarketCache()
    market_data = MarketDataService(market_cache=market_cache)

    snapshot = market_data.handle_orderbook_payload(
        payload=payload,
        default_symbol=symbol,
    )

    engine = build_engine(
        symbol=symbol,
        market_cache=market_cache,
        initial_cash=initial_cash,
        order_size_usd=order_size_usd,
        max_open_orders=max_open_orders,
        pair_boost=pair_boost,
    )

    result = engine.step(
        timestamp=datetime.now(timezone.utc),
    )

    print_market_snapshot(snapshot)
    print_decisions(result)
    print_submitted_orders(engine)
    print_portfolio(engine)
    print_competition(engine)

    print()
    print("=" * 70)
    print("DONE")
    print("=" * 70)


if __name__ == "__main__":
    main()