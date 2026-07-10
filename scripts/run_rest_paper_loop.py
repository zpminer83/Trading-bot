import os
import re
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
from bot.execution.passive_fill_evidence import (
    PassiveFillEvidence,
    PassiveFillEvidenceTracker,
)
from bot.market.market_cache import MarketCache
from bot.market.market_data_service import MarketDataService
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


MAX_ERROR_MESSAGE_LENGTH = 500
REQUEST_HEADERS_PATTERN = re.compile(
    r"(?is)\b(?:request[_ -]?headers?|headers?)\b\s*[:=]\s*"
    r"(?:\{.*?\}|[^\r\n]*)"
)
PRIVATE_KEY_BLOCK_PATTERN = re.compile(
    r"(?is)-----BEGIN [^-\r\n]*PRIVATE KEY-----.*?"
    r"-----END [^-\r\n]*PRIVATE KEY-----"
)
AUTHORIZATION_PATTERN = re.compile(
    r"(?i)\bauthorization\b\s*[:=]\s*[^\r\n,;]+"
)
BEARER_PATTERN = re.compile(r"(?i)\bBearer\s+[^\s,;]+")
SENSITIVE_VALUE_PATTERN = re.compile(
    r"(?i)\b(api[-_ ]?key|private[-_ ]?key|jwt|token|secret|cookie|"
    r"password|passphrase)\b\s*[:=]\s*"
    r"(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)


def env_decimal(name: str, default: str) -> Decimal:
    return Decimal(os.getenv(name, default))


def env_int(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


def env_float(name: str, default: float) -> float:
    return float(os.getenv(name, str(default)))


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)

    if value is None:
        return default

    return value.strip().lower() in {"1", "true", "yes", "on"}


def validate_failure_configuration(
    max_consecutive_failures: int,
    base_seconds: float,
    max_seconds: float,
) -> None:
    if max_consecutive_failures < 1:
        raise ValueError("PAPER_MAX_CONSECUTIVE_FAILURES must be >= 1")

    if not base_seconds >= 0:
        raise ValueError("PAPER_ERROR_BACKOFF_BASE_SECONDS must be >= 0")

    if not max_seconds >= base_seconds:
        raise ValueError(
            "PAPER_ERROR_BACKOFF_MAX_SECONDS must be >= "
            "PAPER_ERROR_BACKOFF_BASE_SECONDS"
        )


def calculate_error_backoff(
    consecutive_failures: int,
    base_seconds: float,
    max_seconds: float,
) -> float:
    if consecutive_failures < 1:
        raise ValueError("consecutive_failures must be >= 1")

    if not base_seconds >= 0:
        raise ValueError("base_seconds must be >= 0")

    if not max_seconds >= base_seconds:
        raise ValueError("max_seconds must be >= base_seconds")

    if base_seconds == 0:
        return 0.0

    backoff_seconds = float(base_seconds)

    for _ in range(consecutive_failures - 1):
        backoff_seconds *= 2

        if backoff_seconds >= max_seconds:
            return float(max_seconds)

    return float(min(backoff_seconds, max_seconds))


def cancel_all_paper_orders(
    engine: ConservativePaperTradingEngine,
) -> None:
    order_manager = getattr(engine, "order_manager", None)
    cancel_all = getattr(order_manager, "cancel_all", None)

    if callable(cancel_all):
        cancel_all()


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


def safe_error_message(exc: Exception) -> str:
    message = str(exc)
    message = REQUEST_HEADERS_PATTERN.sub("headers=[REDACTED]", message)
    message = PRIVATE_KEY_BLOCK_PATTERN.sub("[REDACTED PRIVATE KEY]", message)
    message = AUTHORIZATION_PATTERN.sub("authorization=[REDACTED]", message)
    message = BEARER_PATTERN.sub("Bearer [REDACTED]", message)
    message = SENSITIVE_VALUE_PATTERN.sub(
        lambda match: f"{match.group(1)}=[REDACTED]",
        message,
    )

    return message[:MAX_ERROR_MESSAGE_LENGTH]


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
    portfolio_risk_limits: PortfolioRiskLimits | None = None,
) -> None:
    freshness_limits = market_freshness_limits or MarketFreshnessLimits()
    risk_limits = portfolio_risk_limits or PortfolioRiskLimits()

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
    print()
    print("Portfolio risk limit:")
    print(
        "Max drawdown : "
        f"{fmt_decimal(risk_limits.max_drawdown * Decimal('100'))}%"
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


def print_portfolio_risk(result) -> None:
    decision = getattr(result, "portfolio_risk_decision", None)

    print("Portfolio risk:")

    if decision is None:
        print("  Status       : not evaluated")
        return

    print(f"  Allowed      : {decision.allowed}")
    print(f"  Reason       : {decision.reason}")
    print(f"  Latched      : {decision.latched}")
    print(
        "  Drawdown     : "
        f"{fmt_decimal(decision.drawdown * Decimal('100'))}%"
    )
    print(
        "  Max drawdown : "
        f"{fmt_decimal(decision.max_drawdown * Decimal('100'))}%"
    )
    print(f"  Equity       : {fmt_decimal(decision.equity)}")
    print(f"  Peak equity  : {fmt_decimal(decision.peak_equity)}")

    if decision.latched:
        print("  KILL SWITCH  : LATCHED")


def print_passive_fill_evidence(
    evidence: list[PassiveFillEvidence],
) -> None:
    maximum_age = max(
        (item.age_seconds for item in evidence),
        default=None,
    )

    print("Passive fill evidence:")
    print(f"  Evaluated orders                    : {len(evidence)}")
    print(
        "  At touch                            : "
        f"{sum(item.at_touch for item in evidence)}"
    )
    print(
        "  Crossed                             : "
        f"{sum(item.crossed for item in evidence)}"
    )
    print(
        "  Level quantity decreased (ambiguous): "
        f"{sum(item.level_quantity_decreased for item in evidence)}"
    )
    print(
        "  Level disappeared (ambiguous)       : "
        f"{sum(item.level_disappeared for item in evidence)}"
    )
    print(f"  Maximum open-order age              : {fmt_seconds(maximum_age)}")
    print(
        "  Note: quantity changes may be trades or cancellations; "
        "they are not confirmed fills."
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
    iteration_index: int = 0,
    passive_fill_evidence: list[PassiveFillEvidence] | None = None,
) -> PaperRunRecord:
    portfolio = engine.portfolio
    competition = engine.competition
    market_safety = result.market_safety_decision
    market_freshness = result.market_freshness_decision
    portfolio_risk = getattr(result, "portfolio_risk_decision", None)
    evidence = passive_fill_evidence or []

    return PaperRunRecord(
        timestamp=timestamp,
        symbol=symbol,
        iteration_index=iteration_index,
        iteration_ok=True,
        error_type=None,
        error_message=None,
        consecutive_failures=0,
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
        portfolio_risk_allowed=(
            portfolio_risk.allowed if portfolio_risk is not None else None
        ),
        portfolio_risk_reason=(
            portfolio_risk.reason if portfolio_risk is not None else None
        ),
        portfolio_risk_latched=(
            portfolio_risk.latched if portfolio_risk is not None else None
        ),
        risk_drawdown=(
            portfolio_risk.drawdown if portfolio_risk is not None else None
        ),
        risk_max_drawdown=(
            portfolio_risk.max_drawdown if portfolio_risk is not None else None
        ),
        evaluated_open_orders_count=len(evidence),
        orders_at_touch_count=sum(item.at_touch for item in evidence),
        crossed_order_count=sum(item.crossed for item in evidence),
        level_quantity_decreased_count=sum(
            item.level_quantity_decreased for item in evidence
        ),
        level_disappeared_count=sum(
            item.level_disappeared for item in evidence
        ),
        max_open_order_age_seconds=max(
            (item.age_seconds for item in evidence),
            default=None,
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


def build_failed_record(
    timestamp: datetime,
    symbol: str,
    iteration_index: int,
    exc: Exception,
    engine: ConservativePaperTradingEngine,
    consecutive_failures: int = 1,
) -> PaperRunRecord:
    values: dict[str, Any] = {}
    portfolio = getattr(engine, "portfolio", None)
    competition = getattr(engine, "competition", None)
    broker = getattr(engine, "broker", None)

    if portfolio is not None:
        for field_name in (
            "cash_balance",
            "base_position",
            "equity",
            "realized_pnl",
            "unrealized_pnl",
            "drawdown",
            "total_volume",
        ):
            field_value = getattr(portfolio, field_name, None)

            if field_value is not None:
                values[field_name] = field_value

    if competition is not None:
        for field_name in (
            "weekly_volume",
            "estimated_score",
            "raffle_tickets",
        ):
            field_value = getattr(competition, field_name, None)

            if field_value is not None:
                values[field_name] = field_value

    open_orders = getattr(broker, "open_orders", None)

    if open_orders is not None:
        values["open_orders_count"] = len(open_orders)

    return PaperRunRecord(
        timestamp=timestamp,
        symbol=symbol,
        iteration_index=iteration_index,
        iteration_ok=False,
        error_type=type(exc).__name__,
        error_message=safe_error_message(exc),
        consecutive_failures=consecutive_failures,
        **values,
    )


def persist_record(
    recorder: PaperRunRecorder,
    record: PaperRunRecord,
    output_path: Path,
    *,
    sync_to_disk: bool = False,
) -> None:
    recorder.append_jsonl(
        output_path,
        record,
        sync_to_disk=sync_to_disk,
    )


def run_iteration(
    index: int,
    url: str,
    symbol: str,
    market_data: MarketDataService,
    engine: ConservativePaperTradingEngine,
    recorder: PaperRunRecorder,
    output_path: Path,
    *,
    sync_to_disk: bool = False,
    fill_evidence_tracker: PassiveFillEvidenceTracker | None = None,
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

    evidence: list[PassiveFillEvidence] = []

    if fill_evidence_tracker is not None:
        open_orders_before_step = tuple(engine.broker.open_orders)
        evidence = fill_evidence_tracker.observe(
            orders=open_orders_before_step,
            orderbook=snapshot.orderbook,
            observed_at=now,
        )

    result = engine.step(timestamp=now)

    if fill_evidence_tracker is not None:
        fill_evidence_tracker.synchronize(
            orders=tuple(engine.broker.open_orders),
            orderbook=snapshot.orderbook,
            observed_at=now,
        )

    record = build_record(
        timestamp=now,
        symbol=symbol,
        snapshot=snapshot,
        result=result,
        engine=engine,
        iteration_index=index,
        passive_fill_evidence=evidence,
    )

    persist_record(
        recorder=recorder,
        record=record,
        output_path=output_path,
        sync_to_disk=sync_to_disk,
    )

    print_market_snapshot(snapshot)
    print_market_freshness(result)
    print_market_safety(result)
    print_portfolio_risk(result)
    print_passive_fill_evidence(evidence)
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
    sync_to_disk = env_bool("PAPER_RUN_FSYNC")
    max_consecutive_failures = env_int(
        "PAPER_MAX_CONSECUTIVE_FAILURES",
        5,
    )
    error_backoff_base_seconds = env_float(
        "PAPER_ERROR_BACKOFF_BASE_SECONDS",
        2,
    )
    error_backoff_max_seconds = env_float(
        "PAPER_ERROR_BACKOFF_MAX_SECONDS",
        30,
    )

    validate_failure_configuration(
        max_consecutive_failures=max_consecutive_failures,
        base_seconds=error_backoff_base_seconds,
        max_seconds=error_backoff_max_seconds,
    )

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

    url = build_orderbook_url(
        base_url=base_url,
        symbol=symbol,
        depth=depth,
    )

    market_cache = MarketCache()
    market_data = MarketDataService(market_cache=market_cache)
    fill_evidence_tracker = PassiveFillEvidenceTracker()

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
        portfolio_risk_limits=portfolio_risk_limits,
    )

    consecutive_failures = 0

    try:
        for index in range(1, iterations + 1):
            try:
                run_iteration(
                    index=index,
                    url=url,
                    symbol=symbol,
                    market_data=market_data,
                    engine=engine,
                    recorder=recorder,
                    output_path=output_path,
                    sync_to_disk=sync_to_disk,
                    fill_evidence_tracker=fill_evidence_tracker,
                )
            except Exception as exc:
                consecutive_failures += 1

                failed_record = build_failed_record(
                    timestamp=datetime.now(timezone.utc),
                    symbol=symbol,
                    iteration_index=index,
                    exc=exc,
                    engine=engine,
                    consecutive_failures=consecutive_failures,
                )
                persist_record(
                    recorder=recorder,
                    record=failed_record,
                    output_path=output_path,
                    sync_to_disk=sync_to_disk,
                )

                print()
                print("=" * 80)
                print(f"LOOP {index} FAILED")
                print("=" * 80)
                print(f"Error: {failed_record.error_message}")
                print("=" * 80)

                if consecutive_failures >= max_consecutive_failures:
                    cancel_all_paper_orders(engine)
                    print()
                    print("=" * 80)
                    print(
                        "CIRCUIT BREAKER: "
                        f"{consecutive_failures} consecutive failures reached "
                        f"the configured limit of {max_consecutive_failures}."
                    )
                    print("Open paper orders cancelled. Stopping paper loop.")
                    print("=" * 80)
                    break

                if index < iterations:
                    backoff_seconds = calculate_error_backoff(
                        consecutive_failures=consecutive_failures,
                        base_seconds=error_backoff_base_seconds,
                        max_seconds=error_backoff_max_seconds,
                    )
                    print(f"Retry backoff: {backoff_seconds:g}s")
                    time.sleep(backoff_seconds)

                continue

            consecutive_failures = 0

            if index < iterations:
                time.sleep(interval_seconds)
    except KeyboardInterrupt:
        cancel_all_paper_orders(engine)
        print()
        print("Interrupted by user. Open paper orders cancelled.")

    print_final_summary(
        engine=engine,
        recorder=recorder,
        output_path=output_path,
    )


if __name__ == "__main__":
    main()
