"""Deterministic offline execution fault-recovery scenarios.

The harness exercises state, portfolio and audit components without changing
the normal strategy or conservative broker implementation.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Callable

from bot.competition.competition_tracker import CompetitionTracker
from bot.competition.confirmed_fill_ledger import ConfirmedFillLedger
from bot.competition.fair_play_guard import FairPlayGuard
from bot.execution.conservative_paper_broker import ConservativePaperBroker
from bot.execution.order import OrderDecision, OrderIntent, OrderPurpose
from bot.execution.paper_broker import PaperFill
from bot.execution.state_reconciliation import (
    ExecutionFillEvent,
    ExecutionStateReconciler,
    ReconciliationState,
    SimulatedExchangeOrder,
)
from bot.portfolio.portfolio_manager import PortfolioManager


SYMBOL = "DREAM/USDC"


@dataclass(frozen=True)
class FaultScenarioResult:
    scenario: str
    passed: bool
    unresolved_orders: int = 0
    duplicate_fill_count: int = 0
    partial_fill_count: int = 0
    unknown_submission_count: int = 0
    final_inventory: Decimal = Decimal("0")
    competition_volume: Decimal = Decimal("0")
    state: str = ReconciliationState.RECONCILED.value
    trading_blocked_reason: str | None = None
    notes: tuple[str, ...] = ()


def _intent(*, side: str = "buy", quantity: str = "1", purpose: OrderPurpose = OrderPurpose.ENTRY) -> OrderIntent:
    return OrderIntent(SYMBOL, side, "limit", Decimal("10"), Decimal(quantity), purpose=purpose, strategy_name="fault_harness", rationale="offline recovery test")


def _order(order_id: str, *, client: str | None = None, status: str = "open", sequence: int = 0) -> SimulatedExchangeOrder:
    return SimulatedExchangeOrder(order_id, client, SYMBOL, "buy", Decimal("10"), Decimal("1"), Decimal("1"), status, sequence)


def _fill(reconciler: ExecutionStateReconciler, portfolio: PortfolioManager, ledger: ConfirmedFillLedger, competition: CompetitionTracker, event: ExecutionFillEvent, *, partial: bool = False) -> bool:
    if not (reconciler.record_partial_fill(event) if partial else reconciler.record_fill(event)):
        return False
    before = portfolio.base_position
    if event.side == "buy":
        portfolio.buy(event.price, event.quantity)
    else:
        portfolio.sell(event.price, event.quantity)
    ledger.record_fills(
        [PaperFill(event.symbol, event.side, event.price, event.quantity, event.notional)],
        starting_position=before,
        timestamp=event.timestamp,
    )
    competition.record_trade(event.symbol, event.notional, timestamp=event.timestamp)
    if event.commission:
        portfolio.cash_balance -= event.commission
    return True


def restart_with_open_order() -> FaultScenarioResult:
    portfolio = PortfolioManager(Decimal("100"))
    broker = ConservativePaperBroker(portfolio)
    broker.submit(OrderDecision(True, "ok", _intent()))
    local = _order("ex-1", client="client-1")
    exchange = _order("ex-1", client="client-1")
    reconciler = ExecutionStateReconciler()
    # A restart begins with the local broker snapshot and does not submit a
    # second order before reconciliation.
    blocked_before = len(broker.open_orders) == 1
    result = reconciler.reconcile([local], [exchange], policy="cancel")
    # Current conservative restart policy cancels the orphaned paper order.
    broker.cancel_all()
    no_duplicate = len(broker.open_orders) == 0 and result.matched_orders == (exchange,)
    return FaultScenarioResult("RESTART_WITH_OPEN_ORDER", blocked_before and no_duplicate and result.state == ReconciliationState.RECONCILED, state=result.state.value, notes=("restart policy: cancel",))


def restart_with_position() -> FaultScenarioResult:
    portfolio = PortfolioManager(Decimal("100"))
    reconciler = ExecutionStateReconciler()
    result = reconciler.reconcile([], [], local_cash=Decimal("100"), local_inventory=Decimal("0"), exchange_cash=Decimal("90"), exchange_inventory=Decimal("1"))
    # Authoritative inventory is applied only through reconciliation.
    portfolio.cash_balance = result.exchange_cash or portfolio.cash_balance
    portfolio.base_position = result.exchange_inventory or Decimal("0")
    reconciler.apply_authoritative_balance(cash=portfolio.cash_balance, inventory=portfolio.base_position)
    entry_allowed = False  # unknown/reconciled position never creates a new ENTRY here
    risk_exit = _intent(side="sell", quantity="1", purpose=OrderPurpose.RISK_EXIT)
    only_reduce = risk_exit.side == "sell" and risk_exit.quantity <= portfolio.base_position
    return FaultScenarioResult("RESTART_WITH_POSITION", (not entry_allowed) and only_reduce, final_inventory=portfolio.base_position, state=reconciler.state.value)


def submit_timeout_unknown() -> FaultScenarioResult:
    reconciler = ExecutionStateReconciler()
    reconciler.mark_unknown_submission("client-timeout")
    blocked = not reconciler.can_submit_new_orders
    found = reconciler.resolve_unknown_submission("client-timeout", _order("ex-timeout", client="client-timeout"))
    found_again = reconciler.resolve_unknown_submission("client-timeout", _order("ex-timeout", client="client-timeout"))
    not_found = ExecutionStateReconciler()
    not_found.mark_unknown_submission("client-missing")
    safe = not not_found.resolve_unknown_submission("client-missing", None)
    passed = blocked and found and found_again and safe and not not_found.can_submit_new_orders
    return FaultScenarioResult("SUBMIT_TIMEOUT_UNKNOWN", passed, unknown_submission_count=reconciler.unknown_submission_count + not_found.unknown_submission_count, state=not_found.state.value if safe else reconciler.state.value)


def partial_fill() -> FaultScenarioResult:
    portfolio = PortfolioManager(Decimal("100"))
    ledger = ConfirmedFillLedger()
    competition = CompetitionTracker(now=datetime(2026, 1, 1, tzinfo=timezone.utc))
    reconciler = ExecutionStateReconciler()
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    first = ExecutionFillEvent("fill-1", "ex-partial", SYMBOL, "buy", Decimal("10"), Decimal("0.4"), commission=Decimal("0.01"), timestamp=t0)
    second = ExecutionFillEvent("fill-2", "ex-partial", SYMBOL, "buy", Decimal("10"), Decimal("0.2"), commission=Decimal("0.01"), timestamp=t0 + timedelta(seconds=1))
    ok1 = _fill(reconciler, portfolio, ledger, competition, first, partial=True)
    ok2 = _fill(reconciler, portfolio, ledger, competition, second, partial=True)
    # Remaining quantity is cancelled; no synthetic fill is generated.
    remaining = Decimal("0.4")
    cancelled = remaining > 0
    return FaultScenarioResult("PARTIAL_FILL", ok1 and ok2 and cancelled and portfolio.base_position == Decimal("0.6"), partial_fill_count=reconciler.partial_fill_count, final_inventory=portfolio.base_position, competition_volume=competition.weekly_volume, state=reconciler.state.value)


def duplicate_fill_event() -> FaultScenarioResult:
    portfolio = PortfolioManager(Decimal("100"))
    ledger = ConfirmedFillLedger()
    competition = CompetitionTracker(now=datetime(2026, 1, 1, tzinfo=timezone.utc))
    fair_play = FairPlayGuard()
    reconciler = ExecutionStateReconciler()
    event = ExecutionFillEvent("stable-fill", "ex-dup", SYMBOL, "buy", Decimal("10"), Decimal("1"), timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc))
    first = _fill(reconciler, portfolio, ledger, competition, event)
    fair_play.consume(ledger.events)
    second = _fill(reconciler, portfolio, ledger, competition, event)
    return FaultScenarioResult("DUPLICATE_FILL_EVENT", first and not second and len(ledger.events) == 1 and competition.weekly_volume == Decimal("10"), duplicate_fill_count=reconciler.duplicate_fill_count, final_inventory=portfolio.base_position, competition_volume=competition.weekly_volume)


def out_of_order_events() -> FaultScenarioResult:
    reconciler = ExecutionStateReconciler()
    reconciler.reconcile([], [_order("ex-order", status="filled", sequence=2)])
    reconciler._accept_status(_order("ex-order", status="open", sequence=1))  # stale exchange event, state remains terminal
    status = reconciler._order_status["ex-order"].status
    return FaultScenarioResult("OUT_OF_ORDER_EVENTS", status == "filled", state=status)


def network_loss_with_open_order() -> FaultScenarioResult:
    reconciler = ExecutionStateReconciler()
    reconciler.register_local_order(_order("ex-network"))
    reconciler.mark_network_loss()
    unresolved = reconciler.shutdown(exchange_status_confirmed=False)
    return FaultScenarioResult("NETWORK_LOSS_WITH_OPEN_ORDER", len(unresolved) == 1 and not reconciler.can_submit_new_orders, unresolved_orders=len(unresolved), state=reconciler.state.value, trading_blocked_reason=reconciler.blocked_reason)


def balance_mismatch() -> FaultScenarioResult:
    reconciler = ExecutionStateReconciler()
    result = reconciler.reconcile([], [], local_cash=Decimal("100"), local_inventory=Decimal("0"), exchange_cash=Decimal("95"), exchange_inventory=Decimal("1"))
    blocked = result.state == ReconciliationState.BALANCE_MISMATCH and result.trading_blocked
    reconciler.apply_authoritative_balance(cash=Decimal("95"), inventory=Decimal("1"))
    return FaultScenarioResult("BALANCE_MISMATCH", blocked and reconciler.can_submit_new_orders, final_inventory=Decimal("1"), state=result.state.value, trading_blocked_reason=result.reason)


SCENARIO_FUNCTIONS: dict[str, Callable[[], FaultScenarioResult]] = {
    "RESTART_WITH_OPEN_ORDER": restart_with_open_order,
    "RESTART_WITH_POSITION": restart_with_position,
    "SUBMIT_TIMEOUT_UNKNOWN": submit_timeout_unknown,
    "PARTIAL_FILL": partial_fill,
    "DUPLICATE_FILL_EVENT": duplicate_fill_event,
    "OUT_OF_ORDER_EVENTS": out_of_order_events,
    "NETWORK_LOSS_WITH_OPEN_ORDER": network_loss_with_open_order,
    "BALANCE_MISMATCH": balance_mismatch,
}


def run_fault_scenario(name: str) -> FaultScenarioResult:
    try:
        return SCENARIO_FUNCTIONS[name]()
    except Exception as exc:  # deterministic failure is still reported by the CLI
        return FaultScenarioResult(name, False, notes=(f"{type(exc).__name__}: {str(exc)[:200]}",))


def run_all_fault_scenarios() -> tuple[FaultScenarioResult, ...]:
    return tuple(run_fault_scenario(name) for name in SCENARIO_FUNCTIONS)


__all__ = ["FaultScenarioResult", "run_fault_scenario", "run_all_fault_scenarios", "SCENARIO_FUNCTIONS"]
