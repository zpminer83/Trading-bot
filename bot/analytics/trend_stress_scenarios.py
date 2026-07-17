"""Deterministic offline stress scenarios for the conservative paper engine.

The module is deliberately a thin scenario harness.  Market snapshots are
generated locally and are fed through the existing engine, broker, portfolio,
risk, and fair-play implementations.  No strategy or execution rules are
duplicated here and no network client is used.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Iterable

from bot.competition.competition_tracker import CompetitionTracker
from bot.competition.confirmed_fill_ledger import ConfirmedFillLedger
from bot.competition.fair_play_guard import FairPlayGuard, FairPlayLimits
from bot.core.conservative_paper_trading_engine import (
    ConservativePaperTradingEngine,
    ConservativePaperTradingStepResult,
)
from bot.execution.conservative_paper_broker import ConservativePaperBroker
from bot.execution.execution_manager import ExecutionManager
from bot.execution.order import OrderPurpose
from bot.execution.order_manager import OrderManager
from bot.market.market_cache import MarketCache
from bot.market.models import OrderBook, OrderBookLevel
from bot.portfolio.portfolio_manager import PortfolioManager
from bot.risk.market_freshness import MarketFreshnessGuard, MarketFreshnessLimits
from bot.risk.market_safety import MarketSafety
from bot.risk.portfolio_risk_guard import PortfolioRiskGuard, PortfolioRiskLimits
from bot.risk.risk_manager import RiskLimits, RiskManager
from bot.strategy.passive_market_maker import PassiveMarketMakerStrategy


SYMBOL = "SOMI:USDso"
SCENARIO_NAMES = (
    "STEADY_UPTREND",
    "STEADY_DOWNTREND",
    "FAST_SELL_OFF",
    "V_SHAPE_RECOVERY",
    "HIGH_VOLATILITY_SIDEWAYS",
)
SCENARIO_START = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)


@dataclass(frozen=True)
class TrendStressScenario:
    """A deterministic sequence of mid prices and elapsed step seconds."""

    name: str
    mid_prices: tuple[Decimal, ...]
    step_seconds: int = 10
    spread: Decimal = Decimal("0.20")

    def __post_init__(self) -> None:
        if self.name not in SCENARIO_NAMES:
            raise ValueError(f"unknown trend stress scenario: {self.name}")
        if len(self.mid_prices) < 2:
            raise ValueError("a scenario requires at least two market snapshots")
        if any(price <= 0 for price in self.mid_prices):
            raise ValueError("scenario prices must be positive")
        if self.step_seconds <= 0:
            raise ValueError("step_seconds must be positive")
        if self.spread <= 0:
            raise ValueError("spread must be positive")

    @property
    def prices(self) -> tuple[Decimal, ...]:
        return self.mid_prices


@dataclass(frozen=True)
class TrendStressResult:
    scenario: str
    initial_mid_price: Decimal
    final_mid_price: Decimal
    market_return: Decimal
    generated_orders: int
    submitted_orders: int
    rejected_orders: int
    confirmed_fills: int
    confirmed_volume: Decimal
    buy_fills: int
    sell_fills: int
    maximum_base_inventory: Decimal
    final_base_inventory: Decimal
    maximum_notional_exposure: Decimal
    initial_equity: Decimal
    final_equity: Decimal
    minimum_equity: Decimal
    maximum_drawdown: Decimal
    configured_drawdown_threshold: Decimal
    drawdown_at_latch: Decimal | None
    maximum_drawdown_after_latch: Decimal
    drawdown_overshoot: Decimal
    portfolio_risk_allowed: bool
    portfolio_risk_latched: bool
    risk_exit_enabled: bool
    risk_exit_intents: int
    risk_exit_fills: int
    fair_play_allowed_count: int
    fair_play_rejected_count: int
    fair_play_latched: bool
    open_orders_after_shutdown: int
    inventory_limit_ok: bool
    invariant_passed: bool
    invariant_failures: tuple[str, ...] = field(default_factory=tuple)
    steps: int = 0
    peak_equity: Decimal = Decimal("0")
    realized_pnl: Decimal = Decimal("0")
    unrealized_pnl: Decimal = Decimal("0")
    fees_paid: Decimal = Decimal("0")
    reserved_order_exposure: Decimal = Decimal("0")
    open_order_exposure: Decimal = Decimal("0")
    hard_limit_gap_breach: bool = False
    normal_intents_after_latch: int = 0
    automatic_retries: int = 0
    replacement_count: int = 0
    risk_compliance_status: str = "compliant"
    configured_preemptive_drawdown: Decimal = Decimal("0.08")
    entry_halt_latched: bool = False
    gap_risk_approved_count: int = 0
    gap_risk_blocked_count: int = 0
    largest_adverse_step_return: Decimal | None = None
    largest_adverse_step_from_price: Decimal | None = None
    largest_adverse_step_to_price: Decimal | None = None
    equity_before_largest_adverse_step: Decimal | None = None
    peak_equity_before_largest_adverse_step: Decimal | None = None
    drawdown_before_largest_adverse_step: Decimal | None = None
    inventory_before_largest_adverse_step: Decimal | None = None
    marked_exposure_before_largest_adverse_step: Decimal | None = None
    reserved_buy_exposure_before_largest_adverse_step: Decimal | None = None
    projected_equity_after_largest_adverse_step: Decimal | None = None
    projected_drawdown_after_largest_adverse_step: Decimal | None = None
    fee_slippage_contribution: Decimal | None = None
    maximum_gap_safe_position_notional: Decimal | None = None

    @property
    def drawdown_guard_triggered(self) -> bool:
        return self.portfolio_risk_latched

    @property
    def initial_price(self) -> Decimal:
        return self.initial_mid_price

    @property
    def final_price(self) -> Decimal:
        return self.final_mid_price

    @property
    def max_base_inventory(self) -> Decimal:
        return self.maximum_base_inventory

    @property
    def max_notional_exposure(self) -> Decimal:
        return self.maximum_notional_exposure

    @property
    def final_inventory(self) -> Decimal:
        return self.final_base_inventory

    @property
    def portfolio_risk_status(self) -> str:
        return "latched" if self.portfolio_risk_latched else "allowed"

    @property
    def risk_guard_latched(self) -> bool:
        return self.portfolio_risk_latched

    @property
    def fair_play_status(self) -> str:
        return "latched" if self.fair_play_latched else "allowed"

    @property
    def risk_exit_intent_count(self) -> int:
        return self.risk_exit_intents

    @property
    def risk_exit_fill_count(self) -> int:
        return self.risk_exit_fills

    @property
    def fair_play_rejected(self) -> int:
        return self.fair_play_rejected_count


def _d(value: str | Decimal | int | float) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _path(values: Iterable[str | int | Decimal]) -> tuple[Decimal, ...]:
    return tuple(_d(value) for value in values)


def build_scenario(name: str) -> TrendStressScenario:
    """Return one of the fixed, reproducible market paths."""
    key = name.upper()
    paths: dict[str, tuple[Decimal, ...]] = {
        "STEADY_UPTREND": _path((
            "100", "101", "100.7", "102", "103", "102.6", "104", "105",
            "104.5", "106", "107",
        )),
        "STEADY_DOWNTREND": _path((
            "100", "99", "99.3", "98", "97", "97.4", "96", "95",
            "95.5", "94", "93",
        )),
        "FAST_SELL_OFF": _path((
            "100", "100.1", "99.9", "100", "98", "94", "88", "80",
            "72", "65", "62", "61",
        )),
        "V_SHAPE_RECOVERY": _path((
            "100", "96", "90", "84", "80", "80.5", "82", "88", "94",
            "98", "100",
        )),
        "HIGH_VOLATILITY_SIDEWAYS": _path((
            "100", "108", "94", "106", "92", "104", "90", "102", "98",
            "100",
        )),
    }
    try:
        return TrendStressScenario(name=key, mid_prices=paths[key])
    except KeyError as exc:
        raise ValueError(f"unknown trend stress scenario: {name}") from exc


def build_all_scenarios() -> tuple[TrendStressScenario, ...]:
    return tuple(build_scenario(name) for name in SCENARIO_NAMES)


def _set_market(
    market: MarketCache,
    *,
    mid_price: Decimal,
    spread: Decimal,
    timestamp: datetime,
    sequence: int,
) -> None:
    half_spread = spread / Decimal("2")
    bid = mid_price - half_spread
    ask = mid_price + half_spread
    # Quantities and nonce vary deterministically, while spread remains valid.
    quantity = Decimal("100") + Decimal(sequence)
    market.update_orderbook(
        OrderBook(
            symbol=SYMBOL,
            bids=[OrderBookLevel(price=bid, quantity=quantity)],
            asks=[OrderBookLevel(price=ask, quantity=quantity)],
            timestamp=int(timestamp.timestamp()),
            nonce=f"trend-stress-{sequence}",
        )
    )


def build_engine(
    *,
    initial_cash: Decimal = Decimal("150"),
    order_size_usd: Decimal = Decimal("20"),
    max_drawdown: Decimal = Decimal("0.10"),
    risk_exit_enabled: bool = False,
    paper_risk_exit_enabled: bool | None = None,
    gap_aware: bool = True,
) -> ConservativePaperTradingEngine:
    """Build the production engine with deterministic, offline dependencies."""
    market = MarketCache()
    portfolio = PortfolioManager(initial_cash=initial_cash)
    risk_manager = RiskManager(
        limits=RiskLimits(
            max_drawdown=max_drawdown,
            base_order_size_usd=order_size_usd,
        )
    )
    execution = ExecutionManager(portfolio=portfolio, risk_manager=risk_manager)
    broker = ConservativePaperBroker(portfolio=portfolio)
    order_manager = OrderManager(broker=broker, max_open_orders=2)
    competition = CompetitionTracker(now=SCENARIO_START)
    competition.set_pair_boost(symbol=SYMBOL, boost=Decimal("1"))
    fair_play = FairPlayGuard(
        FairPlayLimits(
            # Keep the published windows as a detection rule, while the
            # guard remains a conservative local control rather than a
            # statement about eligibility.
            short_window_seconds=Decimal("30"),
            opposite_side_cooldown_seconds=Decimal("60"),
            quantity_tolerance_ratio=Decimal("0.10"),
            near_flat_ratio=Decimal("0.10"),
            minimum_meaningful_exposure_notional=Decimal("5"),
            max_completed_near_flat_cycles=2,
        )
    )

    return ConservativePaperTradingEngine(
        symbol=SYMBOL,
        market=market,
        portfolio=portfolio,
        strategy=PassiveMarketMakerStrategy(
            symbol=SYMBOL,
            order_size_usd=order_size_usd,
        ),
        execution=execution,
        broker=broker,
        order_manager=order_manager,
        competition=competition,
        market_safety=MarketSafety(),
        market_freshness=MarketFreshnessGuard(
            MarketFreshnessLimits(
                max_exchange_age_seconds=Decimal("30"),
                max_unchanged_seconds=Decimal("30"),
                max_future_skew_seconds=Decimal("5"),
            )
        ),
        portfolio_risk_guard=PortfolioRiskGuard(
            limits=PortfolioRiskLimits(
                max_drawdown=max_drawdown,
                require_gap_risk_assumptions=gap_aware,
            ),
            risk_manager=risk_manager,
        ),
        confirmed_fill_ledger=ConfirmedFillLedger(),
        fair_play_guard=fair_play,
        paper_risk_exit_enabled=(
            risk_exit_enabled
            if paper_risk_exit_enabled is None
            else paper_risk_exit_enabled
        ),
    )


def run_scenario(
    scenario: TrendStressScenario | str,
    *,
    engine: ConservativePaperTradingEngine | None = None,
) -> TrendStressResult:
    """Run one scenario and validate safety invariants."""
    if isinstance(scenario, str):
        scenario = build_scenario(scenario)
    active_engine = engine or build_engine()
    portfolio = active_engine.portfolio

    initial_equity = portfolio.equity
    minimum_equity = initial_equity
    maximum_drawdown_observed = Decimal("0")
    maximum_base_inventory = portfolio.base_position
    maximum_notional_exposure = Decimal("0")
    generated_orders = submitted_orders = rejected_orders = 0
    confirmed_fills = buy_fills = sell_fills = 0
    confirmed_volume = Decimal("0")
    fair_play_allowed_count = fair_play_rejected_count = 0
    risk_allowed = True
    risk_latched = False
    fair_play_latched = False
    inventory_limit_ok = True
    risk_exit_intents = risk_exit_fills = 0
    configured_drawdown_threshold = active_engine.portfolio_risk_guard.limits.max_drawdown
    drawdown_at_latch: Decimal | None = None
    maximum_drawdown_after_latch = Decimal("0")
    risk_exit_inventory_increase = False
    invariant_failures: list[str] = []
    entry_submission_after_latch = False
    normal_intents_after_latch = 0
    previous_drawdown = Decimal("0")
    hard_limit_gap_breach = False
    entry_halt_latched = False
    gap_risk_approved_count = 0
    gap_risk_blocked_count = 0
    largest_adverse_step_return: Decimal | None = None
    largest_adverse_step_from_price: Decimal | None = None
    largest_adverse_step_to_price: Decimal | None = None
    equity_before_largest_adverse_step: Decimal | None = None
    peak_equity_before_largest_adverse_step: Decimal | None = None
    drawdown_before_largest_adverse_step: Decimal | None = None
    inventory_before_largest_adverse_step: Decimal | None = None
    marked_exposure_before_largest_adverse_step: Decimal | None = None
    reserved_buy_exposure_before_largest_adverse_step: Decimal | None = None
    projected_equity_after_largest_adverse_step: Decimal | None = None
    projected_drawdown_after_largest_adverse_step: Decimal | None = None
    fee_slippage_contribution: Decimal | None = None
    maximum_gap_safe_position_notional: Decimal | None = None
    competition_volume_before = (
        active_engine.competition.weekly_volume
        if active_engine.competition is not None
        else Decimal("0")
    )

    for index, mid_price in enumerate(scenario.mid_prices):
        timestamp = SCENARIO_START + timedelta(seconds=index * scenario.step_seconds)
        if index > 0:
            previous_price = scenario.mid_prices[index - 1]
            step_return = (mid_price - previous_price) / previous_price
            if step_return < 0 and (
                largest_adverse_step_return is None
                or step_return < largest_adverse_step_return
            ):
                reserved_buys = sum(
                    (
                        order.intent.notional
                        for order in active_engine.broker.open_orders
                        if order.intent.side == "buy"
                    ),
                    Decimal("0"),
                )
                budget_before_step = active_engine.portfolio_risk_guard.calculate_gap_risk_budget(
                    portfolio,
                    reserved_order_exposure=reserved_buys,
                )
                before_equity = portfolio.equity
                before_peak = portfolio.peak_equity
                projected_equity = (
                    portfolio.cash_balance
                    + portfolio.base_position * mid_price
                    - budget_before_step.fee_buffer
                    - budget_before_step.exit_slippage_buffer
                )
                projected_dd = (
                    max(before_peak - projected_equity, Decimal("0")) / before_peak
                    if before_peak > 0
                    else Decimal("1")
                )
                largest_adverse_step_return = step_return
                largest_adverse_step_from_price = previous_price
                largest_adverse_step_to_price = mid_price
                equity_before_largest_adverse_step = before_equity
                peak_equity_before_largest_adverse_step = before_peak
                drawdown_before_largest_adverse_step = portfolio.drawdown
                inventory_before_largest_adverse_step = portfolio.base_position
                marked_exposure_before_largest_adverse_step = abs(portfolio.position_value)
                reserved_buy_exposure_before_largest_adverse_step = reserved_buys
                projected_equity_after_largest_adverse_step = projected_equity
                projected_drawdown_after_largest_adverse_step = projected_dd
                fee_slippage_contribution = (
                    budget_before_step.fee_buffer
                    + budget_before_step.exit_slippage_buffer
                )
                maximum_gap_safe_position_notional = (
                    budget_before_step.maximum_gap_safe_position_notional
                )
        _set_market(
            active_engine.market,
            mid_price=mid_price,
            spread=scenario.spread,
            timestamp=timestamp,
            sequence=index,
        )
        was_fair_play_latched = bool(active_engine.fair_play_guard and active_engine.fair_play_guard.latched)
        was_risk_latched = active_engine.portfolio_risk_guard.latched
        was_entry_halt_latched = active_engine.portfolio_risk_guard.entry_halt_latched
        result: ConservativePaperTradingStepResult = active_engine.step(timestamp=timestamp)

        generated_orders += len(result.intents)
        if was_risk_latched or was_entry_halt_latched:
            normal_intents_after_latch += sum(
                intent.purpose == OrderPurpose.ENTRY for intent in result.intents
            )
        submitted_orders += len(result.submitted_orders)
        rejected_orders += sum(not decision.approved for decision in result.decisions)
        rejected_orders += result.fair_play_blocked_intents_count
        confirmed_fills += len(result.confirmed_fill_events)
        buy_fills += sum(event.side.lower() == "buy" for event in result.confirmed_fill_events)
        sell_fills += sum(event.side.lower() == "sell" for event in result.confirmed_fill_events)
        confirmed_volume += sum(
            (event.notional for event in result.confirmed_fill_events),
            Decimal("0"),
        )
        fair_play_rejected_count += result.fair_play_blocked_intents_count
        fair_play_allowed_count += sum(
            decision.allowed for decision in result.fair_play_decisions
        )
        if result.portfolio_risk_decision is not None:
            risk_allowed = result.portfolio_risk_decision.allowed
            risk_latched = result.portfolio_risk_decision.latched
            if result.portfolio_risk_decision.latched:
                if drawdown_at_latch is None:
                    drawdown_at_latch = result.portfolio_risk_decision.drawdown
                maximum_drawdown_after_latch = max(
                    maximum_drawdown_after_latch,
                    result.portfolio_risk_decision.drawdown,
                )
            entry_halt_latched = entry_halt_latched or result.portfolio_risk_decision.entry_halt_latched
        risk_exit_intents += getattr(result, "risk_exit_intents_count", 0)
        risk_exit_fills += getattr(result, "risk_exit_fills_count", 0)
        gap_risk_approved_count += sum(
            budget.gap_risk_budget_approved for budget in result.gap_risk_budgets
        )
        gap_risk_blocked_count += sum(
            not budget.gap_risk_budget_approved for budget in result.gap_risk_budgets
        )
        if getattr(result, "risk_exit_intents_count", 0) > 0 and any(
            intent.side == "buy" or intent.quantity > portfolio.base_position
            for intent in result.intents
            if intent.purpose == OrderPurpose.RISK_EXIT
        ):
            risk_exit_inventory_increase = True
        fair_play_latched = bool(result.fair_play_latched)

        if (was_fair_play_latched or was_risk_latched) and any(
            order.intent.purpose == OrderPurpose.ENTRY
            for order in result.submitted_orders
        ):
            entry_submission_after_latch = True

        minimum_equity = min(minimum_equity, portfolio.equity)
        maximum_drawdown_observed = max(
            maximum_drawdown_observed,
            portfolio.drawdown,
        )
        current_drawdown = portfolio.drawdown
        if (
            current_drawdown > configured_drawdown_threshold
            and previous_drawdown <= configured_drawdown_threshold
        ):
            hard_limit_gap_breach = True
        previous_drawdown = current_drawdown
        maximum_base_inventory = max(maximum_base_inventory, portfolio.base_position)
        maximum_notional_exposure = max(
            maximum_notional_exposure,
            portfolio.position_value.copy_abs(),
        )
        if portfolio.equity > 0:
            if (
                portfolio.position_value / portfolio.equity
                > active_engine.execution.risk_manager.limits.max_position_percent
            ):
                inventory_limit_ok = False

        # Every fair-play rejection is represented in decisions and cannot
        # reach the broker because the engine reviews it before replacement.
        if result.fair_play_blocked_intents_count > 0:
            blocked_ids = {
                id(intent)
                for intent, decision in zip(
                    result.intents,
                    result.fair_play_decisions,
                )
                if not decision.allowed
            }
            if any(id(order.intent) in blocked_ids for order in result.submitted_orders):
                invariant_failures.append("fair-play blocked intent was submitted")

    active_engine.order_manager.cancel_all()
    open_orders_after_shutdown = active_engine.order_manager.open_order_count

    final_mid_price = scenario.mid_prices[-1]
    market_return = (final_mid_price - scenario.mid_prices[0]) / scenario.mid_prices[0]
    drawdown = (
        (initial_equity - minimum_equity) / initial_equity
        if initial_equity > 0
        else Decimal("0")
    )
    maximum_drawdown = max(drawdown, maximum_drawdown_observed, portfolio.drawdown)
    configured_max_drawdown = active_engine.portfolio_risk_guard.limits.max_drawdown
    if maximum_drawdown >= configured_max_drawdown and not risk_latched:
        invariant_failures.append("drawdown threshold was reached without a risk latch")
    if maximum_drawdown > configured_max_drawdown:
        if hard_limit_gap_breach:
            invariant_failures.append("HARD_LIMIT_GAP_BREACH")
        else:
            invariant_failures.append("hard drawdown limit breached")
    competition_volume_after = (
        active_engine.competition.weekly_volume
        if active_engine.competition is not None
        else Decimal("0")
    )
    if competition_volume_after - competition_volume_before != confirmed_volume:
        invariant_failures.append("competition volume included non-confirmed volume")
    if entry_submission_after_latch:
        invariant_failures.append("ENTRY order submitted after fair-play latch")
    if normal_intents_after_latch:
        invariant_failures.append("normal strategy intent generated after risk latch")
    if open_orders_after_shutdown != 0:
        invariant_failures.append("open paper orders remained after shutdown")

    if not inventory_limit_ok:
        invariant_failures.append("inventory exceeded the configured position limit")
    if risk_exit_inventory_increase:
        invariant_failures.append("risk exit increased inventory")
    if risk_latched and drawdown_at_latch is None:
        invariant_failures.append("risk latch status lacked a latch drawdown")
    maximum_drawdown_after_latch = max(
        maximum_drawdown_after_latch,
        maximum_drawdown if drawdown_at_latch is not None else Decimal("0"),
    )
    drawdown_overshoot = max(
        Decimal("0"),
        maximum_drawdown_after_latch - configured_drawdown_threshold,
    )

    return TrendStressResult(
        scenario=scenario.name,
        initial_mid_price=scenario.mid_prices[0],
        final_mid_price=final_mid_price,
        market_return=market_return,
        generated_orders=generated_orders,
        submitted_orders=submitted_orders,
        rejected_orders=rejected_orders,
        confirmed_fills=confirmed_fills,
        confirmed_volume=confirmed_volume,
        buy_fills=buy_fills,
        sell_fills=sell_fills,
        maximum_base_inventory=maximum_base_inventory,
        final_base_inventory=portfolio.base_position,
        maximum_notional_exposure=maximum_notional_exposure,
        initial_equity=initial_equity,
        final_equity=portfolio.equity,
        minimum_equity=minimum_equity,
        maximum_drawdown=maximum_drawdown,
        configured_drawdown_threshold=configured_drawdown_threshold,
        drawdown_at_latch=drawdown_at_latch,
        maximum_drawdown_after_latch=maximum_drawdown_after_latch,
        drawdown_overshoot=drawdown_overshoot,
        portfolio_risk_allowed=risk_allowed,
        portfolio_risk_latched=risk_latched,
        risk_exit_enabled=active_engine.paper_risk_exit_enabled,
        risk_exit_intents=risk_exit_intents,
        risk_exit_fills=risk_exit_fills,
        fair_play_allowed_count=fair_play_allowed_count,
        fair_play_rejected_count=fair_play_rejected_count,
        fair_play_latched=fair_play_latched,
        open_orders_after_shutdown=open_orders_after_shutdown,
        inventory_limit_ok=inventory_limit_ok,
        invariant_passed=not invariant_failures,
        invariant_failures=tuple(invariant_failures),
        steps=len(scenario.mid_prices),
        peak_equity=portfolio.peak_equity,
        realized_pnl=portfolio.realized_pnl,
        unrealized_pnl=portfolio.unrealized_pnl,
        fees_paid=portfolio.fees_paid,
        reserved_order_exposure=sum(
            (order.intent.notional for order in active_engine.broker.open_orders),
            Decimal("0"),
        ),
        open_order_exposure=sum(
            (order.intent.notional for order in active_engine.broker.open_orders),
            Decimal("0"),
        ),
        hard_limit_gap_breach=hard_limit_gap_breach,
        normal_intents_after_latch=normal_intents_after_latch,
        automatic_retries=0,
        replacement_count=0,
        risk_compliance_status=("compliant" if maximum_drawdown <= configured_max_drawdown else "noncompliant"),
        configured_preemptive_drawdown=active_engine.portfolio_risk_guard.limits.preemptive_drawdown,
        entry_halt_latched=entry_halt_latched,
        gap_risk_approved_count=gap_risk_approved_count,
        gap_risk_blocked_count=gap_risk_blocked_count,
        largest_adverse_step_return=largest_adverse_step_return,
        largest_adverse_step_from_price=largest_adverse_step_from_price,
        largest_adverse_step_to_price=largest_adverse_step_to_price,
        equity_before_largest_adverse_step=equity_before_largest_adverse_step,
        peak_equity_before_largest_adverse_step=peak_equity_before_largest_adverse_step,
        drawdown_before_largest_adverse_step=drawdown_before_largest_adverse_step,
        inventory_before_largest_adverse_step=inventory_before_largest_adverse_step,
        marked_exposure_before_largest_adverse_step=marked_exposure_before_largest_adverse_step,
        reserved_buy_exposure_before_largest_adverse_step=reserved_buy_exposure_before_largest_adverse_step,
        projected_equity_after_largest_adverse_step=projected_equity_after_largest_adverse_step,
        projected_drawdown_after_largest_adverse_step=projected_drawdown_after_largest_adverse_step,
        fee_slippage_contribution=fee_slippage_contribution,
        maximum_gap_safe_position_notional=maximum_gap_safe_position_notional,
    )


def run_all_scenarios() -> tuple[TrendStressResult, ...]:
    return tuple(run_scenario(scenario) for scenario in build_all_scenarios())


def run_fast_sell_off_comparison() -> tuple[TrendStressResult, TrendStressResult, TrendStressResult]:
    """Run legacy, gap-aware, and gap-aware emergency-exit profiles."""
    scenario = build_scenario("FAST_SELL_OFF")
    return (
        run_scenario(scenario, engine=build_engine(risk_exit_enabled=False, gap_aware=False)),
        run_scenario(scenario, engine=build_engine(risk_exit_enabled=False, gap_aware=True)),
        run_scenario(scenario, engine=build_engine(risk_exit_enabled=True, gap_aware=True)),
    )


# Descriptive aliases make the small offline harness convenient to use from
# notebooks and tests without creating a second execution path.
run_scenarios = run_all_scenarios


__all__ = [
    "SCENARIO_NAMES",
    "SCENARIO_START",
    "SYMBOL",
    "TrendStressScenario",
    "TrendStressResult",
    "build_all_scenarios",
    "build_engine",
    "build_scenario",
    "run_all_scenarios",
    "run_fast_sell_off_comparison",
    "run_scenarios",
    "run_scenario",
]
