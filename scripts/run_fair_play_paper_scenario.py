"""Deterministic, offline fair-play safety scenarios for paper trading.

This module never fetches market data and never sends real orders.  Scenario
two submits to ``ConservativePaperBroker`` directly only as an isolated test
harness so that its existing fill rules create the audited ``PaperFill``
objects.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from bot.competition.competition_tracker import CompetitionTracker
from bot.competition.confirmed_fill_ledger import (
    ConfirmedFillEvent,
    ConfirmedFillLedger,
)
from bot.competition.fair_play_guard import FairPlayGuard
from bot.core.conservative_paper_trading_engine import (
    ConservativePaperTradingEngine,
)
from bot.execution.conservative_paper_broker import ConservativePaperBroker
from bot.execution.execution_manager import ExecutionManager
from bot.execution.order import OrderDecision, OrderIntent
from bot.execution.order_manager import OrderManager
from bot.execution.paper_broker import PaperFill
from bot.market.market_cache import MarketCache
from bot.market.models import OrderBook, OrderBookLevel
from bot.portfolio.portfolio_manager import PortfolioManager
from bot.risk.market_freshness import MarketFreshnessGuard
from bot.risk.market_safety import MarketSafety
from bot.risk.portfolio_risk_guard import PortfolioRiskGuard, PortfolioRiskLimits
from bot.risk.risk_manager import RiskLimits, RiskManager
from bot.strategy.passive_market_maker import PassiveMarketMakerStrategy


SYMBOL = "SOMI:USDso"
T0 = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)


@dataclass(frozen=True)
class PreTradeCooldownScenarioResult:
    confirmed_buy_fills: tuple[PaperFill, ...]
    confirmed_fill_events: tuple[ConfirmedFillEvent, ...]
    position: Decimal
    blocked_intents: int
    fair_play_decision_reasons: tuple[str, ...]
    submitted_sell_orders: int
    execution_reviewed_sell_intents: int
    competition_volume: Decimal
    guard_latched: bool
    confirmed_fills_from_broker: bool
    passed: bool


@dataclass(frozen=True)
class ConfirmedRoundTripScenarioResult:
    buy_timestamp: datetime
    sell_timestamp: datetime
    broker_fills: tuple[PaperFill, ...]
    fill_events: tuple[ConfirmedFillEvent, ...]
    opposite_fill_delay: Decimal | None
    quantity_difference_ratio: Decimal | None
    short_window_detected: bool
    short_window_round_trip_count: int
    guard_latched: bool
    guard_reason: str
    reset_successful: bool
    competition_volume_created: bool
    normal_broker_portfolio_volume: Decimal
    broker_created_fills_only: bool
    passed: bool


def update_book(
    market: MarketCache,
    *,
    bid: Decimal,
    ask: Decimal,
    observed_at: datetime,
    nonce: str,
) -> None:
    market.update_orderbook(
        OrderBook(
            symbol=SYMBOL,
            bids=[OrderBookLevel(price=bid, quantity=Decimal("100"))],
            asks=[OrderBookLevel(price=ask, quantity=Decimal("100"))],
            timestamp=int(observed_at.timestamp()),
            nonce=nonce,
        )
    )


def build_engine() -> ConservativePaperTradingEngine:
    market = MarketCache()
    portfolio = PortfolioManager(initial_cash=Decimal("150"))
    risk_manager = RiskManager(limits=RiskLimits(max_drawdown=Decimal("0.10")))
    execution = ExecutionManager(portfolio=portfolio, risk_manager=risk_manager)
    broker = ConservativePaperBroker(portfolio=portfolio)
    order_manager = OrderManager(broker=broker, max_open_orders=2)
    competition = CompetitionTracker(now=T0)
    competition.set_pair_boost(symbol=SYMBOL, boost=Decimal("1"))
    portfolio_risk_guard = PortfolioRiskGuard(
        limits=PortfolioRiskLimits(max_drawdown=Decimal("0.10")),
        risk_manager=risk_manager,
    )

    return ConservativePaperTradingEngine(
        symbol=SYMBOL,
        market=market,
        portfolio=portfolio,
        strategy=PassiveMarketMakerStrategy(
            symbol=SYMBOL,
            order_size_usd=Decimal("5"),
        ),
        execution=execution,
        broker=broker,
        order_manager=order_manager,
        competition=competition,
        market_safety=MarketSafety(),
        market_freshness=MarketFreshnessGuard(),
        portfolio_risk_guard=portfolio_risk_guard,
        confirmed_fill_ledger=ConfirmedFillLedger(),
        fair_play_guard=FairPlayGuard(),
    )


def run_pre_trade_cooldown_scenario() -> PreTradeCooldownScenarioResult:
    """Runs the production engine against deterministic safe snapshots."""
    engine = build_engine()

    update_book(
        engine.market,
        bid=Decimal("100"),
        ask=Decimal("101"),
        observed_at=T0,
        nonce="scenario-1-initial",
    )
    first_step = engine.step(timestamp=T0)

    update_book(
        engine.market,
        bid=Decimal("99.01"),
        ask=Decimal("100"),
        observed_at=T0 + timedelta(seconds=10),
        nonce="scenario-1-cross-buy",
    )
    second_step = engine.step(timestamp=T0 + timedelta(seconds=10))

    confirmed_buy_fills = tuple(
        fill for fill in second_step.fills if fill.side == "buy"
    )
    confirmed_events = tuple(second_step.confirmed_fill_events)
    fair_play_reasons = tuple(
        decision.reason for decision in second_step.fair_play_decisions
    )
    submitted_sell_orders = sum(
        order.intent.side == "sell" for order in second_step.submitted_orders
    )
    execution_reviewed_sell_intents = sum(
        decision.intent.side == "sell" for decision in second_step.decisions
    )
    competition_volume = (
        engine.competition.weekly_volume if engine.competition is not None else Decimal("0")
    )
    confirmed_fills_from_broker = all(
        fill in engine.broker.fills for fill in confirmed_buy_fills
    )
    passed = (
        len(first_step.submitted_orders) == 1
        and len(confirmed_buy_fills) == 1
        and engine.portfolio.base_position > 0
        and second_step.fair_play_blocked_intents_count >= 1
        and "opposite_side_cooldown" in fair_play_reasons
        and submitted_sell_orders == 0
        and execution_reviewed_sell_intents == 0
        and not bool(second_step.fair_play_latched)
        and competition_volume == confirmed_buy_fills[0].notional
        and confirmed_fills_from_broker
    )

    return PreTradeCooldownScenarioResult(
        confirmed_buy_fills=confirmed_buy_fills,
        confirmed_fill_events=confirmed_events,
        position=engine.portfolio.base_position,
        blocked_intents=second_step.fair_play_blocked_intents_count,
        fair_play_decision_reasons=fair_play_reasons,
        submitted_sell_orders=submitted_sell_orders,
        execution_reviewed_sell_intents=execution_reviewed_sell_intents,
        competition_volume=competition_volume,
        guard_latched=bool(second_step.fair_play_latched),
        confirmed_fills_from_broker=confirmed_fills_from_broker,
        passed=passed,
    )


def run_confirmed_round_trip_latch_scenario() -> ConfirmedRoundTripScenarioResult:
    """Uses direct broker submission only as an isolated test harness."""
    market = MarketCache()
    portfolio = PortfolioManager(initial_cash=Decimal("150"))
    broker = ConservativePaperBroker(portfolio=portfolio)
    ledger = ConfirmedFillLedger()
    guard = FairPlayGuard()
    buy_timestamp = T0
    sell_timestamp = T0 + timedelta(seconds=20)

    update_book(
        market,
        bid=Decimal("0.99"),
        ask=Decimal("1.00"),
        observed_at=buy_timestamp,
        nonce="scenario-2-buy",
    )
    buy_order = broker.submit(
        OrderDecision(
            approved=True,
            reason="isolated_test_harness",
            intent=OrderIntent(
                symbol=SYMBOL,
                side="buy",
                order_type="limit",
                price=Decimal("1.00"),
                quantity=Decimal("5"),
            ),
        )
    )
    buy_fills = broker.process_market(market)
    buy_events = ledger.record_fills(
        buy_fills,
        starting_position=Decimal("0"),
        timestamp=buy_timestamp,
    )
    guard.consume(buy_events)

    update_book(
        market,
        bid=Decimal("1.01"),
        ask=Decimal("1.02"),
        observed_at=sell_timestamp,
        nonce="scenario-2-sell",
    )
    sell_order = broker.submit(
        OrderDecision(
            approved=True,
            reason="isolated_test_harness",
            intent=OrderIntent(
                symbol=SYMBOL,
                side="sell",
                order_type="limit",
                price=Decimal("1.01"),
                quantity=Decimal("5"),
            ),
        )
    )
    sell_fills = broker.process_market(market)
    sell_events = ledger.record_fills(
        sell_fills,
        starting_position=Decimal("5"),
        timestamp=sell_timestamp,
    )
    guard_decision = guard.consume(sell_events)
    second_event = sell_events[0] if sell_events else None
    all_fills = tuple(buy_fills + sell_fills)
    broker_created_fills_only = (
        buy_order is not None
        and sell_order is not None
        and all(fill in broker.fills for fill in all_fills)
    )

    guard.reset()
    reset_successful = not guard.latched
    passed = (
        len(buy_fills) == 1
        and len(sell_fills) == 1
        and second_event is not None
        and second_event.short_window_round_trip
        and second_event.seconds_since_opposite_fill == Decimal("20")
        and ledger.short_window_round_trip_count == 1
        and guard_decision.latched
        and guard_decision.reason == "short_window_round_trip"
        and reset_successful
        and broker_created_fills_only
    )

    return ConfirmedRoundTripScenarioResult(
        buy_timestamp=buy_timestamp,
        sell_timestamp=sell_timestamp,
        broker_fills=all_fills,
        fill_events=tuple(buy_events + sell_events),
        opposite_fill_delay=(
            second_event.seconds_since_opposite_fill if second_event else None
        ),
        quantity_difference_ratio=(
            second_event.opposite_quantity_difference_ratio if second_event else None
        ),
        short_window_detected=(
            second_event.short_window_round_trip if second_event else False
        ),
        short_window_round_trip_count=ledger.short_window_round_trip_count,
        guard_latched=guard_decision.latched,
        guard_reason=guard_decision.reason,
        reset_successful=reset_successful,
        competition_volume_created=False,
        normal_broker_portfolio_volume=portfolio.total_volume,
        broker_created_fills_only=broker_created_fills_only,
        passed=passed,
    )


def _format_decimal(value: Decimal | None) -> str:
    if value is None:
        return "n/a"
    return format(value, "f")


def print_report(
    cooldown: PreTradeCooldownScenarioResult,
    round_trip: ConfirmedRoundTripScenarioResult,
) -> None:
    print("FAIR-PLAY PAPER SAFETY SCENARIO")
    print()
    print("Scenario 1 - Pre-trade cooldown")
    print(f"  Confirmed buy fills : {len(cooldown.confirmed_buy_fills)}")
    print(f"  Position            : {_format_decimal(cooldown.position)}")
    print(f"  Blocked intents     : {cooldown.blocked_intents}")
    print(f"  Block reason        : {', '.join(cooldown.fair_play_decision_reasons)}")
    print(f"  Sell orders submitted: {cooldown.submitted_sell_orders}")
    print(f"  Competition volume  : {_format_decimal(cooldown.competition_volume)}")
    print(f"  Result: {'PASS' if cooldown.passed else 'FAIL'}")
    print()
    print("Scenario 2 - Confirmed round-trip latch (isolated direct broker test harness)")
    print(f"  Buy timestamp       : {round_trip.buy_timestamp.isoformat()}")
    print(f"  Sell timestamp      : {round_trip.sell_timestamp.isoformat()}")
    print(f"  Opposite-fill delay : {_format_decimal(round_trip.opposite_fill_delay)}s")
    print(
        "  Quantity difference ratio: "
        f"{_format_decimal(round_trip.quantity_difference_ratio)}"
    )
    print(f"  Short-window detection: {round_trip.short_window_detected}")
    print(f"  Guard latched       : {round_trip.guard_latched}")
    print(f"  Reset successful    : {round_trip.reset_successful}")
    print(
        "  No synthetic competition volume: "
        f"{not round_trip.competition_volume_created}"
    )
    print(f"  Result: {'PASS' if round_trip.passed else 'FAIL'}")
    print()
    overall = cooldown.passed and round_trip.passed
    print(f"Overall result: {'PASS' if overall else 'FAIL'}")


def main() -> int:
    try:
        cooldown = run_pre_trade_cooldown_scenario()
        round_trip = run_confirmed_round_trip_latch_scenario()
    except Exception as exc:
        print("FAIR-PLAY PAPER SAFETY SCENARIO")
        print(f"Overall result: FAIL ({type(exc).__name__}: {exc})")
        return 1

    print_report(cooldown, round_trip)
    return 0 if cooldown.passed and round_trip.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
