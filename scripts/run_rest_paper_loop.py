import os
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from bot.analytics.paper_run_recorder import PaperRunRecord, PaperRunRecorder
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
from bot.risk.market_freshness import (
    MarketFreshnessGuard,
    MarketFreshnessLimits,
)
from bot.risk.market_safety import MarketSafety, MarketSafetyLimits
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


def fmt_seconds(value: Decimal | None) -> str:
    if value is None:
        return "n/a"

    return f"{fmt_decimal(value, '0.000000')}s"


def build_output_path(started_at: datetime) -> Path:
    custom_path = os.getenv("PAPER_RUN_OUTPUT")

    if custom_path:
        return Path(custom_path)

    filename = f"paper_run_{started_at.strftime('%Y%m%d_%H%M%S')}.jsonl"

    return Path("data") / "paper_runs" / filename


def build_engine(
    symbol: str,
    market_cache: MarketCache,
    initial_cash: Decimal,
    order_size_usd: Decimal,
    max_open_orders: int,
    pair_boost: Decimal,
    max_spread_percent: Decimal,
    min_best_bid_quantity: Decimal,
    min_best_ask_quantity: Decimal,
    market_freshness_limits: MarketFreshnessLimits | None = None,
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

    market_safety = MarketSafety(
        limits=MarketSafetyLimits(
            max_spread_percent=max_spread_percent,
            min_best_bid_quantity=min_best_bid_quantity,
            min_best_ask_quantity=min_best_ask_quantity,
        )
    )

    market_freshness = MarketFreshnessGuard(
        limits=market_freshness_limits,
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
        market_safety=market_safety,
        market_freshness=market_freshness,
    )


def print_header(
    base_url: str,
    symbol: str,
    depth: int,
    iterations: int,
    interval_seconds: int,
    initial_cash: Decimal,
    order_size_usd: Decimal,
    pair_boost: Decimal,
    max_spread_percent: Decimal,
    min_best_bid_quantity: Decimal,
    min_best_ask_quantity: Decimal,
    output_path: Path,
    market_freshness_limits: MarketFreshnessLimits | None = None,
) -> None:
    freshness_limits = market_freshness_limits or MarketFreshnessLimits()

    print("=" * 80)
    print("DREAMDEX REST PAPER LOOP")
    print("=" * 80)
    print("Mode    : READ-ONLY + PAPER LOOP")
    print("Warning : no real orders are sent")
    print(f"Base URL: {base_url}")
    print(f"Symbol  : {symbol}")
    print(f"Depth   : {depth}")
    print(f"Loops   : {iterations}")
    print(f"Interval: {interval_seconds}s")
    print(f"Cash    : {fmt_decimal(initial_cash, '0.000000')}")
    print(f"Order $ : {fmt_decimal(order_size_usd, '0.000000')}")
    print(f"Boost   : {fmt_decimal(pair_boost, '0.000000')}")
    print(f"Recorder: {output_path}")
    print()
    print("Market safety limits:")
    print(
        "Max spread % : "
        f"{fmt_decimal(max_spread_percent * Decimal('100'), '0.000000')}%"
    )
    print(f"Min bid qty  : {fmt_decimal(min_best_bid_quantity, '0.000000')}")
    print(f"Min ask qty  : {fmt_decimal(min_best_ask_quantity, '0.000000')}")
    print()
    print("Market freshness limits:")
    print(
        "Max exchange age : "
        f"{fmt_decimal(freshness_limits.max_exchange_age_seconds)}s"
    )
    print(
        "Max unchanged    : "
        f"{fmt_decimal(freshness_limits.max_unchanged_seconds)}s"
    )
    print(
        "Max future skew  : "
        f"{fmt_decimal(freshness_limits.max_future_skew_seconds)}s"
    )
    print("=" * 80)


def print_market_snapshot(snapshot) -> None:
    print("Market:")

    if snapshot.best_bid is not None:
        print(
            "  Best bid : "
            f"{fmt_decimal(snapshot.best_bid.price, '0.000000')} "
            f"x {fmt_decimal(snapshot.best_bid.quantity, '0.000000')}"
        )
    else:
        print("  Best bid : n/a")

    if snapshot.best_ask is not None:
        print(
            "  Best ask : "
            f"{fmt_decimal(snapshot.best_ask.price, '0.000000')} "
            f"x {fmt_decimal(snapshot.best_ask.quantity, '0.000000')}"
        )
    else:
        print("  Best ask : n/a")

    print(f"  Mid      : {fmt_decimal(snapshot.mid_price, '0.000000')}")
    print(f"  Spread   : {fmt_decimal(snapshot.spread, '0.000000')}")


def print_market_safety(result) -> None:
    decision = result.market_safety_decision

    print("Market safety:")

    if decision is None:
        print("  Status   : disabled")
        return

    print(f"  Safe     : {decision.safe}")
    print(f"  Reason   : {decision.reason}")

    if decision.spread_percent is not None:
        print(
            "  Spread % : "
            f"{fmt_decimal(decision.spread_percent * Decimal('100'), '0.000000')}%"
        )


def print_market_freshness(result) -> None:
    decision = result.market_freshness_decision

    print("Market freshness:")

    if decision is None:
        print("  Status         : disabled")
        return

    print(f"  Fresh          : {decision.fresh}")
    print(f"  Reason         : {decision.reason}")
    print(
        "  Exchange age   : "
        f"{fmt_seconds(decision.exchange_age_seconds)}"
    )
    print(
        "  Unchanged time : "
        f"{fmt_seconds(decision.unchanged_seconds)}"
    )


def print_fills(result) -> None:
    print("Fills:")

    if not result.fills:
        print("  No fills.")
        return

    for fill in result.fills:
        print(
            f"  {fill.side.upper()} "
            f"{fill.symbol} "
            f"price={fmt_decimal(fill.price, '0.000000')} "
            f"qty={fmt_decimal(fill.quantity, '0.000000')} "
            f"notional={fmt_decimal(fill.notional, '0.000000')}"
        )


def print_decisions(result) -> None:
    print("Paper decisions:")

    if not result.decisions:
        print("  No decisions.")
        return

    for decision in result.decisions:
        intent = decision.intent

        print(
            f"  {intent.side.upper()} "
            f"{intent.symbol} "
            f"price={fmt_decimal(intent.price, '0.000000')} "
            f"qty={fmt_decimal(intent.quantity, '0.000000')} "
            f"notional={fmt_decimal(intent.notional, '0.000000')} "
            f"approved={decision.approved} "
            f"reason={decision.reason}"
        )


def print_open_orders(engine: ConservativePaperTradingEngine) -> None:
    print("Open paper orders:")

    if not engine.broker.open_orders:
        print("  No open paper orders.")
        return

    for order in engine.broker.open_orders:
        intent = order.intent

        print(
            f"  #{order.order_id} "
            f"{intent.side.upper()} "
            f"{intent.symbol} "
            f"price={fmt_decimal(intent.price, '0.000000')} "
            f"qty={fmt_decimal(intent.quantity, '0.000000')} "
            f"notional={fmt_decimal(intent.notional, '0.000000')} "
            f"status={order.status}"
        )


def print_portfolio(engine: ConservativePaperTradingEngine) -> None:
    portfolio = engine.portfolio

    print("Paper portfolio:")
    print(f"  Cash       : {fmt_decimal(portfolio.cash_balance, '0.000000')}")
    print(f"  Position   : {fmt_decimal(portfolio.base_position, '0.000000')}")
    print(f"  Avg entry  : {fmt_decimal(portfolio.average_entry_price, '0.000000')}")
    print(f"  Equity     : {fmt_decimal(portfolio.equity, '0.000000')}")
    print(f"  Realized   : {fmt_decimal(portfolio.realized_pnl, '0.000000')}")
    print(f"  Unrealized : {fmt_decimal(portfolio.unrealized_pnl, '0.000000')}")
    print(f"  Drawdown   : {fmt_decimal(portfolio.drawdown, '0.000000')}")
    print(f"  Volume     : {fmt_decimal(portfolio.total_volume, '0.000000')}")


def print_competition(engine: ConservativePaperTradingEngine) -> None:
    competition = engine.competition

    if competition is None:
        return

    print("Competition estimate:")
    print(f"  Weekly volume : {fmt_decimal(competition.weekly_volume, '0.000000')}")
    print(f"  Est. score    : {fmt_decimal(competition.estimated_score, '0.000000')}")
    print(f"  Raffle tickets: {competition.raffle_tickets}")


def print_final_summary(
    engine: ConservativePaperTradingEngine,
    recorder: PaperRunRecorder,
    output_path: Path,
) -> None:
    portfolio = engine.portfolio
    competition = engine.competition

    print()
    print("=" * 80)
    print("FINAL PAPER SUMMARY")
    print("=" * 80)
    print(f"Cash          : {fmt_decimal(portfolio.cash_balance, '0.000000')}")
    print(f"Position      : {fmt_decimal(portfolio.base_position, '0.000000')}")
    print(f"Equity        : {fmt_decimal(portfolio.equity, '0.000000')}")
    print(f"Realized PnL  : {fmt_decimal(portfolio.realized_pnl, '0.000000')}")
    print(f"Unrealized PnL: {fmt_decimal(portfolio.unrealized_pnl, '0.000000')}")
    print(f"Total volume  : {fmt_decimal(portfolio.total_volume, '0.000000')}")

    if competition is not None:
        print(f"Weekly volume : {fmt_decimal(competition.weekly_volume, '0.000000')}")
        print(f"Est. score    : {fmt_decimal(competition.estimated_score, '0.000000')}")
        print(f"Raffle tickets: {competition.raffle_tickets}")

    print(f"Open orders   : {len(engine.broker.open_orders)}")
    print(f"Records saved : {recorder.count}")
    print(f"Output file   : {output_path}")
    print("=" * 80)


def build_record(
    timestamp: datetime,
    symbol: str,
    snapshot,
    result,
    engine: ConservativePaperTradingEngine,
) -> PaperRunRecord:
    portfolio = engine.portfolio
    competition = engine.competition
    market_safety = result.market_safety_decision
    market_freshness = result.market_freshness_decision

    return PaperRunRecord(
        timestamp=timestamp,
        symbol=symbol,
        best_bid=snapshot.best_bid.price if snapshot.best_bid is not None else None,
        best_ask=snapshot.best_ask.price if snapshot.best_ask is not None else None,
        mid_price=snapshot.mid_price,
        spread=snapshot.spread,
        market_safe=market_safety.safe if market_safety is not None else None,
        market_safety_reason=(
            market_safety.reason if market_safety is not None else None
        ),
        market_fresh=(
            market_freshness.fresh if market_freshness is not None else None
        ),
        market_freshness_reason=(
            market_freshness.reason if market_freshness is not None else None
        ),
        exchange_age_seconds=(
            market_freshness.exchange_age_seconds
            if market_freshness is not None
            else None
        ),
        unchanged_seconds=(
            market_freshness.unchanged_seconds
            if market_freshness is not None
            else None
        ),
        intents_count=len(result.intents),
        decisions_count=len(result.decisions),
        fills_count=len(result.fills),
        submitted_orders_count=len(result.submitted_orders),
        open_orders_count=len(engine.broker.open_orders),
        cash_balance=portfolio.cash_balance,
        base_position=portfolio.base_position,
        equity=portfolio.equity,
        realized_pnl=portfolio.realized_pnl,
        unrealized_pnl=portfolio.unrealized_pnl,
        drawdown=portfolio.drawdown,
        total_volume=portfolio.total_volume,
        weekly_volume=(
            competition.weekly_volume if competition is not None else Decimal("0")
        ),
        estimated_score=(
            competition.estimated_score if competition is not None else Decimal("0")
        ),
        raffle_tickets=competition.raffle_tickets if competition is not None else 0,
    )


def run_iteration(
    index: int,
    url: str,
    symbol: str,
    market_data: MarketDataService,
    engine: ConservativePaperTradingEngine,
    recorder: PaperRunRecorder,
) -> None:
    now = datetime.now(timezone.utc)

    print()
    print("=" * 80)
    print(f"LOOP {index} | {now.isoformat()}")
    print("=" * 80)

    response: dict[str, Any] = fetch_json(url)

    payload = extract_orderbook_payload(
        response=response,
        symbol=symbol,
    )

    snapshot = market_data.handle_orderbook_payload(
        payload=payload,
        default_symbol=symbol,
    )

    result = engine.step(timestamp=now)

    record = build_record(
        timestamp=now,
        symbol=symbol,
        snapshot=snapshot,
        result=result,
        engine=engine,
    )

    recorder.append(record)

    print_market_snapshot(snapshot)
    print_market_freshness(result)
    print_market_safety(result)
    print_fills(result)
    print_decisions(result)
    print_open_orders(engine)
    print_portfolio(engine)
    print_competition(engine)


def main() -> None:
    started_at = datetime.now(timezone.utc)
    output_path = build_output_path(started_at)
    recorder = PaperRunRecorder()

    base_url = os.getenv("DREAMDEX_API_BASE_URL", DEFAULT_BASE_URL)
    symbol = os.getenv("DREAMDEX_SYMBOL", DEFAULT_SYMBOL)
    depth = env_int("DREAMDEX_DEPTH", DEFAULT_DEPTH)

    iterations = env_int("PAPER_LOOP_ITERATIONS", 5)
    interval_seconds = env_int("PAPER_LOOP_INTERVAL_SECONDS", 10)

    initial_cash = env_decimal("PAPER_INITIAL_CASH", "150")
    order_size_usd = env_decimal("PAPER_ORDER_SIZE_USD", "5")
    pair_boost = env_decimal("PAPER_PAIR_BOOST", "1")
    max_open_orders = env_int("PAPER_MAX_OPEN_ORDERS", 2)

    max_spread_percent = env_decimal("MARKET_MAX_SPREAD_PERCENT", "0.02")
    min_best_bid_quantity = env_decimal("MARKET_MIN_BEST_BID_QTY", "1")
    min_best_ask_quantity = env_decimal("MARKET_MIN_BEST_ASK_QTY", "1")

    market_freshness_limits = MarketFreshnessLimits(
        max_exchange_age_seconds=env_decimal(
            "MARKET_MAX_EXCHANGE_AGE_SECONDS",
            "30",
        ),
        max_unchanged_seconds=env_decimal(
            "MARKET_MAX_UNCHANGED_SECONDS",
            "30",
        ),
        max_future_skew_seconds=env_decimal(
            "MARKET_MAX_FUTURE_SKEW_SECONDS",
            "5",
        ),
    )

    url = build_orderbook_url(
        base_url=base_url,
        symbol=symbol,
        depth=depth,
    )

    market_cache = MarketCache()
    market_data = MarketDataService(market_cache=market_cache)

    engine = build_engine(
        symbol=symbol,
        market_cache=market_cache,
        initial_cash=initial_cash,
        order_size_usd=order_size_usd,
        max_open_orders=max_open_orders,
        pair_boost=pair_boost,
        max_spread_percent=max_spread_percent,
        min_best_bid_quantity=min_best_bid_quantity,
        min_best_ask_quantity=min_best_ask_quantity,
        market_freshness_limits=market_freshness_limits,
    )

    print_header(
        base_url=base_url,
        symbol=symbol,
        depth=depth,
        iterations=iterations,
        interval_seconds=interval_seconds,
        initial_cash=initial_cash,
        order_size_usd=order_size_usd,
        pair_boost=pair_boost,
        max_spread_percent=max_spread_percent,
        min_best_bid_quantity=min_best_bid_quantity,
        min_best_ask_quantity=min_best_ask_quantity,
        output_path=output_path,
        market_freshness_limits=market_freshness_limits,
    )

    for index in range(1, iterations + 1):
        try:
            run_iteration(
                index=index,
                url=url,
                symbol=symbol,
                market_data=market_data,
                engine=engine,
                recorder=recorder,
            )
        except KeyboardInterrupt:
            print()
            print("Interrupted by user.")
            break
        except Exception as exc:
            print()
            print("=" * 80)
            print(f"LOOP {index} FAILED")
            print("=" * 80)
            print(f"Error: {exc}")
            print("=" * 80)

        if index < iterations:
            time.sleep(interval_seconds)

    if recorder.count > 0:
        recorder.write_jsonl(output_path)

    print_final_summary(
        engine=engine,
        recorder=recorder,
        output_path=output_path,
    )


if __name__ == "__main__":
    main()
