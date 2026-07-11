import os
from datetime import datetime, timezone
from decimal import Decimal

from bot.competition.competition_tracker import CompetitionTracker
from bot.competition.confirmed_fill_ledger import (
    ConfirmedFillLedger,
    ConfirmedFillLedgerLimits,
)
from bot.competition.fair_play_guard import FairPlayGuard, FairPlayLimits
from bot.competition.trade_intent_ledger import TradeIntentLedger
from bot.core.conservative_paper_trading_engine import (
    ConservativePaperTradingEngine,
)
from bot.execution.conservative_paper_broker import ConservativePaperBroker
from bot.execution.execution_manager import ExecutionManager
from bot.execution.order_manager import OrderManager
from bot.market.market_cache import MarketCache
from bot.market.market_data_service import MarketDataService
from bot.market.orderbook_signal import (
    OrderBookSignalEngine,
    OrderBookSignalLimits,
)
from bot.portfolio.portfolio_manager import PortfolioManager
from bot.risk.portfolio_risk_guard import (
    PortfolioRiskGuard,
    PortfolioRiskLimits,
)
from bot.risk.market_freshness import (
    MarketFreshnessGuard,
    MarketFreshnessLimits,
)
from bot.risk.market_safety import MarketSafety, MarketSafetyLimits
from bot.risk.risk_manager import RiskLimits, RiskManager
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


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


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
    portfolio_risk_limits: PortfolioRiskLimits | None = None,
    fair_play_limits: FairPlayLimits | None = None,
    signal_limits: OrderBookSignalLimits | None = None,
) -> ConservativePaperTradingEngine:
    resolved_risk_limits = portfolio_risk_limits or PortfolioRiskLimits()
    portfolio = PortfolioManager(initial_cash=initial_cash)
    risk = RiskManager(
        limits=RiskLimits(max_drawdown=resolved_risk_limits.max_drawdown)
    )
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

    portfolio_risk_guard = PortfolioRiskGuard(
        limits=resolved_risk_limits,
        risk_manager=risk,
    )
    confirmed_fill_ledger = None
    fair_play_guard = None
    trade_intent_ledger = TradeIntentLedger()
    orderbook_signal_engine = (
        OrderBookSignalEngine(limits=signal_limits)
        if signal_limits is not None
        else None
    )
    if fair_play_limits is not None:
        confirmed_fill_ledger = ConfirmedFillLedger(
            limits=ConfirmedFillLedgerLimits(
                short_window_seconds=fair_play_limits.short_window_seconds,
                quantity_tolerance_ratio=fair_play_limits.quantity_tolerance_ratio,
                near_flat_ratio=fair_play_limits.near_flat_ratio,
                minimum_meaningful_exposure_notional=(
                    fair_play_limits.minimum_meaningful_exposure_notional
                ),
            )
        )
        fair_play_guard = FairPlayGuard(limits=fair_play_limits)

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
        portfolio_risk_guard=portfolio_risk_guard,
        confirmed_fill_ledger=confirmed_fill_ledger,
        fair_play_guard=fair_play_guard,
        trade_intent_ledger=trade_intent_ledger,
        orderbook_signal_engine=orderbook_signal_engine,
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


def print_market_safety(result) -> None:
    decision = result.market_safety_decision

    print()
    print("Market safety:")

    if decision is None:
        print("Status : disabled")
        return

    print(f"Safe   : {decision.safe}")
    print(f"Reason : {decision.reason}")

    if decision.spread_percent is not None:
        print(
            "Spread %: "
            f"{fmt_decimal(decision.spread_percent * Decimal('100'), '0.000000')}%"
        )

    if decision.details:
        print("Details:")

        for item in decision.details:
            print(f"- {item}")


def print_market_freshness(result) -> None:
    decision = result.market_freshness_decision

    print()
    print("Market freshness:")

    if decision is None:
        print("Status         : disabled")
        return

    print(f"Fresh          : {decision.fresh}")
    print(f"Reason         : {decision.reason}")
    print(
        "Exchange age   : "
        f"{fmt_seconds(decision.exchange_age_seconds)}"
    )
    print(
        "Unchanged time : "
        f"{fmt_seconds(decision.unchanged_seconds)}"
    )


def print_orderbook_signal(result) -> None:
    signal = getattr(result, "orderbook_signal", None)
    print()
    print("Order-book signal:")
    if signal is None:
        print("Status                 : not evaluated")
        return
    print(f"State                  : {signal.state.value}")
    print(f"Reason                 : {signal.reason}")
    print(f"Samples                : {signal.sample_count}")
    print(f"Spread bps             : {fmt_decimal(signal.spread_bps)}")
    print(f"Bid depth              : {fmt_decimal(signal.bid_depth)}")
    print(f"Ask depth              : {fmt_decimal(signal.ask_depth)}")
    print(f"Depth imbalance        : {fmt_decimal(signal.depth_imbalance)}")
    print(f"Microprice             : {fmt_decimal(signal.microprice)}")
    print(f"Microprice edge bps    : {fmt_decimal(signal.microprice_edge_bps)}")
    print(f"One-step return bps    : {fmt_decimal(signal.one_step_return_bps)}")
    print(f"Rolling momentum bps   : {fmt_decimal(signal.rolling_momentum_bps)}")
    print(f"Confidence             : {fmt_decimal(signal.confidence)}")
    print("Note: confidence is an uncalibrated diagnostic score.")


def print_orderbook_depth_diagnostics(result) -> None:
    diagnostic = getattr(result, "orderbook_depth_diagnostics", None)
    print("Order-book depth diagnostics:")
    if diagnostic is None:
        print("Status                              : not evaluated")
        return
    print(f"Imbalance L1                       : {fmt_decimal(diagnostic.imbalance_l1)}")
    print(f"Imbalance L2                       : {fmt_decimal(diagnostic.imbalance_l2)}")
    print(f"Imbalance L3                       : {fmt_decimal(diagnostic.imbalance_l3)}")
    print(f"Imbalance L5                       : {fmt_decimal(diagnostic.imbalance_l5)}")
    print(f"Imbalance L10                      : {fmt_decimal(diagnostic.imbalance_l10)}")
    print(f"L1 / L5 bid depth                  : {fmt_decimal(diagnostic.bid_depth_l1)} / {fmt_decimal(diagnostic.bid_depth_l5)}")
    print(f"L1 / L5 ask depth                  : {fmt_decimal(diagnostic.ask_depth_l1)} / {fmt_decimal(diagnostic.ask_depth_l5)}")
    print(f"L1-edge sign consistent            : {diagnostic.l1_edge_sign_consistent}")
    print(f"Bid depth concentration L2-L5      : {fmt_decimal(diagnostic.bid_depth_concentration_l2_to_l5)}")
    print(f"Ask depth concentration L2-L5      : {fmt_decimal(diagnostic.ask_depth_concentration_l2_to_l5)}")


def print_portfolio_risk(result) -> None:
    decision = getattr(result, "portfolio_risk_decision", None)

    print()
    print("Portfolio risk:")

    if decision is None:
        print("Status       : not evaluated")
        return

    print(f"Allowed      : {decision.allowed}")
    print(f"Reason       : {decision.reason}")
    print(f"Latched      : {decision.latched}")
    print(
        "Drawdown     : "
        f"{fmt_decimal(decision.drawdown * Decimal('100'))}%"
    )
    print(
        "Max drawdown : "
        f"{fmt_decimal(decision.max_drawdown * Decimal('100'))}%"
    )
    print(f"Equity       : {fmt_decimal(decision.equity)}")
    print(f"Peak equity  : {fmt_decimal(decision.peak_equity)}")

    if decision.latched:
        print("KILL SWITCH  : LATCHED")


def print_confirmed_fill_events(result) -> None:
    events = getattr(result, "confirmed_fill_events", [])
    print()
    print("Confirmed fill audit:")
    if not events:
        print("No confirmed fills.")
        return
    for event in events:
        print(
            f"#{event.sequence_number} {event.timestamp.isoformat()} "
            f"{event.side.upper()} price={fmt_decimal(event.price)} "
            f"qty={fmt_decimal(event.quantity)} notional={fmt_decimal(event.notional)} "
            f"position={fmt_decimal(event.position_before)}->{fmt_decimal(event.position_after)} "
            f"opposite-delay={fmt_seconds(event.seconds_since_opposite_fill)} "
            f"short-round-trip={event.short_window_round_trip} "
            f"near-flat-complete={event.near_flat_cycle_completed}"
        )


def print_fair_play(result) -> None:
    allowed = getattr(result, "fair_play_allowed", None)
    print()
    print("Competition fair play:")
    if allowed is None:
        print("Status : disabled")
        return
    print(f"Allowed             : {allowed}")
    print(f"Reason              : {getattr(result, 'fair_play_reason', None)}")
    print(f"Latched             : {getattr(result, 'fair_play_latched', False)}")
    print(
        "Blocked intents     : "
        f"{getattr(result, 'fair_play_blocked_intents_count', 0)}"
    )
    print(
        "Short-window round trips: "
        f"{getattr(result, 'short_window_round_trip_count', 0)}"
    )
    print(
        "Near-flat cycles    : "
        f"{getattr(result, 'near_flat_cycle_count', 0)}"
    )
    if getattr(result, "fair_play_latched", False):
        print("WARNING: FAIR-PLAY GUARD LATCHED")


def print_trade_intent_audit(result) -> None:
    events = getattr(result, "trade_intent_events", [])
    print()
    print("Trade intent audit:")
    if not events:
        print("No generated intents.")
        return
    for event in events:
        print(
            f"#{event.sequence_number} {event.side.upper()} "
            f"price={fmt_decimal(event.price)} qty={fmt_decimal(event.quantity)} "
            f"purpose={event.purpose} strategy={event.strategy_name} "
            f"fair-play={event.fair_play_allowed}:{event.fair_play_reason} "
            f"execution={event.execution_approved}:{event.execution_reason} "
            f"submitted={event.submitted} order_id={event.resulting_order_id}"
        )


def print_purpose_summary(result) -> None:
    purpose_counts = getattr(result, "purpose_counts", {})
    print()
    print("Intent purpose summary:")
    if not purpose_counts:
        print("No generated purposes.")
        return
    for purpose, count in sorted(purpose_counts.items()):
        print(f"{purpose}: {count}")
    confirmed_counts: dict[str, int] = {}
    for event in getattr(result, "confirmed_fill_events", []):
        purpose = str(getattr(event, "purpose", "unknown") or "unknown")
        confirmed_counts[purpose] = confirmed_counts.get(purpose, 0) + 1
    if confirmed_counts:
        print("Confirmed fills:")
        for purpose, count in sorted(confirmed_counts.items()):
            print(f"  {purpose}: {count}")


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

    portfolio_risk_limits = PortfolioRiskLimits(
        max_drawdown=env_decimal("PAPER_MAX_DRAWDOWN_RATIO", "0.10")
    )
    fair_play_limits = None
    if env_bool("PAPER_FAIR_PLAY_ENABLED", True):
        fair_play_limits = FairPlayLimits(
            short_window_seconds=env_decimal(
                "PAPER_FAIR_PLAY_SHORT_WINDOW_SECONDS",
                "30",
            ),
            opposite_side_cooldown_seconds=env_decimal(
                "PAPER_FAIR_PLAY_OPPOSITE_COOLDOWN_SECONDS",
                "60",
            ),
            quantity_tolerance_ratio=env_decimal(
                "PAPER_FAIR_PLAY_QUANTITY_TOLERANCE_RATIO",
                "0.10",
            ),
            near_flat_ratio=env_decimal(
                "PAPER_FAIR_PLAY_NEAR_FLAT_RATIO",
                "0.10",
            ),
            minimum_meaningful_exposure_notional=env_decimal(
                "PAPER_FAIR_PLAY_MIN_EXPOSURE_NOTIONAL",
                "5",
            ),
            max_completed_near_flat_cycles=env_int(
                "PAPER_FAIR_PLAY_MAX_NEAR_FLAT_CYCLES",
                2,
            ),
        )
    signal_limits = None
    if env_bool("PAPER_SIGNAL_ENABLED", True):
        signal_limits = OrderBookSignalLimits(
            top_levels=env_int("PAPER_SIGNAL_TOP_LEVELS", 5),
            rolling_window=env_int("PAPER_SIGNAL_ROLLING_WINDOW", 12),
            minimum_samples=env_int("PAPER_SIGNAL_MINIMUM_SAMPLES", 4),
            imbalance_threshold=env_decimal(
                "PAPER_SIGNAL_IMBALANCE_THRESHOLD",
                "0.20",
            ),
            microprice_edge_threshold_bps=env_decimal(
                "PAPER_SIGNAL_MICROPRICE_EDGE_BPS",
                "1",
            ),
            momentum_threshold_bps=env_decimal("PAPER_SIGNAL_MOMENTUM_BPS", "2"),
            maximum_signal_spread_bps=env_decimal("PAPER_SIGNAL_MAX_SPREAD_BPS", "30"),
        )

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
        f"{fmt_decimal(market_freshness_limits.max_exchange_age_seconds)}s"
    )
    print(
        "Max unchanged    : "
        f"{fmt_decimal(market_freshness_limits.max_unchanged_seconds)}s"
    )
    print(
        "Max future skew  : "
        f"{fmt_decimal(market_freshness_limits.max_future_skew_seconds)}s"
    )
    print()
    print("Portfolio risk limit:")
    print(
        "Max drawdown : "
        f"{fmt_decimal(portfolio_risk_limits.max_drawdown * Decimal('100'))}%"
    )
    print()
    print("Competition fair-play limits:")
    if fair_play_limits is None:
        print("Status             : disabled")
    else:
        print(f"Short window       : {fmt_seconds(fair_play_limits.short_window_seconds)}")
        print(
            "Opposite cooldown  : "
            f"{fmt_seconds(fair_play_limits.opposite_side_cooldown_seconds)}"
        )
        print(
            "Quantity tolerance : "
            f"{fmt_decimal(fair_play_limits.quantity_tolerance_ratio * Decimal('100'))}%"
        )
        print(
            "Near-flat ratio    : "
            f"{fmt_decimal(fair_play_limits.near_flat_ratio * Decimal('100'))}%"
        )
        print(
            "Min exposure       : "
            f"{fmt_decimal(fair_play_limits.minimum_meaningful_exposure_notional)}"
        )
        print(
            "Max near-flat cycles: "
            f"{fair_play_limits.max_completed_near_flat_cycles}"
        )
    print()
    print("Order-book signal limits:")
    if signal_limits is None:
        print("Status                 : disabled")
    else:
        print(f"Top levels             : {signal_limits.top_levels}")
        print(f"Rolling window         : {signal_limits.rolling_window}")
        print(f"Minimum samples        : {signal_limits.minimum_samples}")
        print(f"Imbalance threshold    : {fmt_decimal(signal_limits.imbalance_threshold)}")
        print(
            "Microprice edge bps    : "
            f"{fmt_decimal(signal_limits.microprice_edge_threshold_bps)}"
        )
        print(
            "Momentum threshold bps : "
            f"{fmt_decimal(signal_limits.momentum_threshold_bps)}"
        )
        print(
            "Maximum spread bps     : "
            f"{fmt_decimal(signal_limits.maximum_signal_spread_bps)}"
        )
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
        max_spread_percent=max_spread_percent,
        min_best_bid_quantity=min_best_bid_quantity,
        min_best_ask_quantity=min_best_ask_quantity,
        market_freshness_limits=market_freshness_limits,
        portfolio_risk_limits=portfolio_risk_limits,
        fair_play_limits=fair_play_limits,
        signal_limits=signal_limits,
    )

    result = engine.step(
        timestamp=datetime.now(timezone.utc),
    )

    print_market_snapshot(snapshot)
    print_market_freshness(result)
    print_market_safety(result)
    print_orderbook_signal(result)
    print_orderbook_depth_diagnostics(result)
    print_portfolio_risk(result)
    print_confirmed_fill_events(result)
    print_fair_play(result)
    print_trade_intent_audit(result)
    print_purpose_summary(result)
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
