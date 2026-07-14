from datetime import datetime, timezone
from decimal import Decimal

from bot.execution.state_reconciliation import (
    ExecutionFillEvent,
    ExecutionStateReconciler,
    ReconciliationState,
    SimulatedExchangeOrder,
)


def order(order_id="ex-1", client_order_id="c-1", status="open", sequence=0):
    return SimulatedExchangeOrder(order_id, client_order_id, "DREAM/USDC", "buy", Decimal("10"), Decimal("1"), Decimal("1"), status, sequence)


def test_reconciliation_matches_by_exchange_or_client_id_and_blocks_unknown_state():
    r = ExecutionStateReconciler()
    r.mark_unknown_submission("c-timeout")
    assert not r.can_submit_new_orders
    assert r.resolve_unknown_submission("c-timeout", order("ex-timeout", "c-timeout"))
    assert r.can_submit_new_orders
    result = r.reconcile([order()], [order()])
    assert result.state is ReconciliationState.RECONCILED
    assert result.matched_orders[0].exchange_order_id == "ex-1"


def test_unknown_submission_not_found_is_unresolved_and_shutdown_is_not_false_zero():
    r = ExecutionStateReconciler()
    r.mark_unknown_submission("missing")
    assert not r.resolve_unknown_submission("missing", None)
    assert r.state is ReconciliationState.UNRESOLVED_ORDER
    assert r.shutdown() == ("missing",)


def test_balance_mismatch_is_authoritative_and_audited():
    r = ExecutionStateReconciler()
    result = r.reconcile([], [], local_cash=100, local_inventory=0, exchange_cash=95, exchange_inventory=1)
    assert result.state is ReconciliationState.BALANCE_MISMATCH
    assert result.trading_blocked
    assert any(a.event == "reconciliation_started" for a in r.audit_events)
    r.apply_authoritative_balance(cash=95, inventory=1)
    assert r.can_submit_new_orders


def test_fill_ids_are_deduplicated_and_partial_counts_are_audited():
    r = ExecutionStateReconciler()
    event = ExecutionFillEvent("fill-1", "ex-1", "DREAM/USDC", "buy", Decimal("10"), Decimal("0.5"), timestamp=datetime.now(timezone.utc))
    assert r.record_partial_fill(event)
    assert not r.record_partial_fill(event)
    assert r.partial_fill_count == 1
    assert r.duplicate_fill_count == 1
    assert any(a.event == "duplicate_fill" for a in r.audit_events)


def test_terminal_status_wins_over_stale_open_status():
    r = ExecutionStateReconciler()
    r.reconcile([], [order(status="filled", sequence=2)])
    r._accept_status(order(status="open", sequence=1))
    assert r._order_status["ex-1"].status == "filled"

