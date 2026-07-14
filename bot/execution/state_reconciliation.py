"""Deterministic, offline execution-state reconciliation primitives.

This module deliberately does not submit or cancel exchange orders.  It keeps
the local execution state conservative until an authoritative simulated
exchange snapshot has been observed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Iterable, Mapping


class ReconciliationState(str, Enum):
    LOCAL = "LOCAL"
    RECONCILING = "RECONCILING"
    RECONCILED = "RECONCILED"
    UNKNOWN_SUBMISSION = "UNKNOWN_SUBMISSION"
    UNRESOLVED_ORDER = "UNRESOLVED_ORDER"
    BALANCE_MISMATCH = "BALANCE_MISMATCH"


def _utc(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _dec(value: Decimal | int | float | str) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


@dataclass(frozen=True)
class SimulatedExchangeOrder:
    exchange_order_id: str
    client_order_id: str | None = None
    symbol: str = ""
    side: str = "buy"
    price: Decimal = Decimal("0")
    quantity: Decimal = Decimal("0")
    remaining_quantity: Decimal = Decimal("0")
    status: str = "open"
    sequence: int = 0


@dataclass(frozen=True)
class ExecutionFillEvent:
    """Stable-id fill event used by the offline fault harness."""

    fill_id: str
    exchange_order_id: str
    symbol: str
    side: str
    price: Decimal
    quantity: Decimal
    notional: Decimal | None = None
    commission: Decimal = Decimal("0")
    timestamp: datetime | None = None

    def __post_init__(self) -> None:
        if self.notional is None:
            object.__setattr__(self, "notional", _dec(self.price) * _dec(self.quantity))
        object.__setattr__(self, "price", _dec(self.price))
        object.__setattr__(self, "quantity", _dec(self.quantity))
        object.__setattr__(self, "commission", _dec(self.commission))


@dataclass(frozen=True)
class ReconciliationAudit:
    event: str
    timestamp: datetime
    details: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ReconciliationResult:
    state: ReconciliationState
    trading_blocked: bool
    reason: str
    local_open_orders_count: int
    exchange_open_orders_count: int
    local_cash: Decimal | None = None
    exchange_cash: Decimal | None = None
    local_inventory: Decimal | None = None
    exchange_inventory: Decimal | None = None
    matched_orders: tuple[SimulatedExchangeOrder, ...] = ()
    unresolved_orders: tuple[str, ...] = ()


class ExecutionStateReconciler:
    """Small state machine for restart, timeout and fault recovery tests."""

    _STATUS_PRIORITY = {"open": 1, "partially_filled": 2, "cancelled": 3, "filled": 3}

    def __init__(self) -> None:
        self.state = ReconciliationState.LOCAL
        self.trading_blocked = False
        self.blocked_reason: str | None = None
        self.local_orders: dict[str, SimulatedExchangeOrder] = {}
        self.exchange_orders: dict[str, SimulatedExchangeOrder] = {}
        self._order_status: dict[str, SimulatedExchangeOrder] = {}
        self._unknown_submissions: dict[str, int] = {}
        self._seen_fill_ids: set[str] = set()
        self.audits: list[ReconciliationAudit] = []
        self.duplicate_fill_count = 0
        self.partial_fill_count = 0
        self.unknown_submission_count = 0
        self.unresolved_orders: set[str] = set()
        self._last_result: ReconciliationResult | None = None

    @property
    def audit_events(self) -> tuple[ReconciliationAudit, ...]:
        return tuple(self.audits)

    @property
    def can_submit_new_orders(self) -> bool:
        return not self.trading_blocked

    @property
    def open_order_ids(self) -> tuple[str, ...]:
        return tuple(sorted(
            key for key, order in self._order_status.items()
            if order.status in {"open", "partially_filled"}
        ))

    def order_status(self, exchange_order_id: str) -> str | None:
        order = self._order_status.get(exchange_order_id)
        return None if order is None else order.status

    def register_local_order(self, order: SimulatedExchangeOrder) -> None:
        key = order.exchange_order_id or (order.client_order_id or "")
        self.local_orders[key] = order

    def start_reconciliation(self) -> None:
        self.state = ReconciliationState.RECONCILING
        self.trading_blocked = True
        self.blocked_reason = "reconciliation_in_progress"
        self._audit("reconciliation_started")

    def reconcile(
        self,
        local_orders: Iterable[SimulatedExchangeOrder] | None = None,
        exchange_orders: Iterable[SimulatedExchangeOrder] | None = None,
        *,
        local_cash: Decimal | int | str | None = None,
        local_inventory: Decimal | int | str | None = None,
        exchange_cash: Decimal | int | str | None = None,
        exchange_inventory: Decimal | int | str | None = None,
        policy: str = "cancel",
    ) -> ReconciliationResult:
        self.start_reconciliation()
        try:
            local = list(self.local_orders.values() if local_orders is None else local_orders)
            exchange = list(() if exchange_orders is None else exchange_orders)
            self.local_orders = {o.exchange_order_id or (o.client_order_id or ""): o for o in local}
            self.exchange_orders = {o.exchange_order_id: o for o in exchange}
            for exchange_order in exchange:
                self._accept_status(exchange_order)
            matched: list[SimulatedExchangeOrder] = []
            unresolved: set[str] = set()
            exchange_by_client = {o.client_order_id: o for o in exchange if o.client_order_id}
            for local_order in local:
                found = self.exchange_orders.get(local_order.exchange_order_id)
                if found is None and local_order.client_order_id:
                    found = exchange_by_client.get(local_order.client_order_id)
                if found is None:
                    unresolved.add(local_order.exchange_order_id or (local_order.client_order_id or "unknown"))
                else:
                    matched.append(found)
                    self._accept_status(found)
            if policy not in {"cancel", "retain"}:
                raise ValueError("policy must be 'cancel' or 'retain'")
            self.unresolved_orders = unresolved
            client_ids = [o.client_order_id for o in local if o.client_order_id]
            duplicate_client_ids = {cid for cid in client_ids if client_ids.count(cid) > 1}
            if duplicate_client_ids:
                self.trading_blocked = True
                self.blocked_reason = "duplicate_local_order"
                self._audit("duplicate_local_order", {"client_order_ids": sorted(duplicate_client_ids)})
            mismatch = (
                exchange_cash is not None and local_cash is not None and _dec(exchange_cash) != _dec(local_cash)
            ) or (
                exchange_inventory is not None and local_inventory is not None and _dec(exchange_inventory) != _dec(local_inventory)
            )
            if unresolved:
                self.state = ReconciliationState.UNRESOLVED_ORDER
                self.trading_blocked = True
                self.blocked_reason = "unresolved_order"
                reason = self.blocked_reason
            elif duplicate_client_ids:
                self.state = ReconciliationState.UNRESOLVED_ORDER
                self.trading_blocked = True
                self.blocked_reason = "duplicate_local_order"
                reason = self.blocked_reason
            elif mismatch:
                self.state = ReconciliationState.BALANCE_MISMATCH
                self.trading_blocked = True
                self.blocked_reason = "balance_mismatch"
                reason = self.blocked_reason
            else:
                self.state = ReconciliationState.RECONCILED
                self.trading_blocked = False
                self.blocked_reason = None
                reason = "reconciled"
            result = ReconciliationResult(
                state=self.state,
                trading_blocked=self.trading_blocked,
                reason=reason,
                local_open_orders_count=len(local),
                exchange_open_orders_count=sum(o.status in {"open", "partially_filled"} for o in exchange),
                local_cash=None if local_cash is None else _dec(local_cash),
                exchange_cash=None if exchange_cash is None else _dec(exchange_cash),
                local_inventory=None if local_inventory is None else _dec(local_inventory),
                exchange_inventory=None if exchange_inventory is None else _dec(exchange_inventory),
                matched_orders=tuple(matched),
                unresolved_orders=tuple(sorted(unresolved)),
            )
            self._last_result = result
            self._audit("reconciliation_completed", {"local_open_orders": len(local), "exchange_open_orders": len(exchange), "policy": policy, "state": self.state.value})
            return result
        except Exception as exc:
            self.state = ReconciliationState.UNRESOLVED_ORDER
            self.trading_blocked = True
            self.blocked_reason = "reconciliation_failed"
            self._audit("reconciliation_failed", {"error_type": type(exc).__name__, "error_message": str(exc)[:500]})
            raise

    # Friendly alias used by callers that prefer explicit naming.
    reconcile_state = reconcile

    def mark_unknown_submission(self, client_order_id: str) -> None:
        self._unknown_submissions[client_order_id] = self._unknown_submissions.get(client_order_id, 0) + 1
        self.unknown_submission_count += 1
        self.state = ReconciliationState.UNKNOWN_SUBMISSION
        self.trading_blocked = True
        self.blocked_reason = "unknown_submission"
        self._audit("unknown_submission", {"client_order_id": client_order_id})

    def resolve_unknown_submission(self, client_order_id: str, found_order: SimulatedExchangeOrder | None) -> bool:
        if found_order is not None:
            key = found_order.exchange_order_id
            self.exchange_orders[key] = found_order
            self._accept_status(found_order)
            self.local_orders[key] = found_order
            self._unknown_submissions.pop(client_order_id, None)
            self.state = ReconciliationState.RECONCILED
            self.trading_blocked = False
            self.blocked_reason = None
            self._audit("unknown_submission_resolved", {"client_order_id": client_order_id, "exchange_order_id": key})
            return True
        self.state = ReconciliationState.UNRESOLVED_ORDER
        self.trading_blocked = True
        self.blocked_reason = "unresolved_order"
        self.unresolved_orders.add(client_order_id)
        self._audit("unknown_submission_not_found", {"client_order_id": client_order_id})
        return False

    def resolve_unknown_submission_after_checks(
        self,
        client_order_id: str,
        checks: int,
        max_checks: int,
        found_order: SimulatedExchangeOrder | None = None,
    ) -> bool:
        """Resolve a timeout after a bounded number of read-only searches."""
        if checks < 0 or max_checks < 1:
            raise ValueError("checks must be >= 0 and max_checks must be >= 1")
        if found_order is not None:
            return self.resolve_unknown_submission(client_order_id, found_order)
        if checks < max_checks:
            self._audit("unknown_submission_search", {"client_order_id": client_order_id, "checks": checks})
            return False
        return self.resolve_unknown_submission(client_order_id, None)

    def record_fill(self, event: ExecutionFillEvent) -> bool:
        if event.fill_id in self._seen_fill_ids:
            self.duplicate_fill_count += 1
            self._audit("duplicate_fill", {"fill_id": event.fill_id})
            return False
        self._seen_fill_ids.add(event.fill_id)
        self._accept_status(SimulatedExchangeOrder(event.exchange_order_id, status="partially_filled"))
        self._audit("confirmed_fill", {"fill_id": event.fill_id, "exchange_order_id": event.exchange_order_id})
        return True

    # Explicit names make it difficult for callers to accidentally bypass the
    # stable-id deduplication path.
    record_fill_event = record_fill

    def record_partial_fill(self, event: ExecutionFillEvent) -> bool:
        accepted = self.record_fill(event)
        if accepted:
            self.partial_fill_count += 1
            self._audit("partial_fill", {"fill_id": event.fill_id})
        return accepted

    def apply_authoritative_balance(self, *, cash: Decimal | int | str, inventory: Decimal | int | str) -> None:
        self._audit("authoritative_balance_applied", {"cash": str(cash), "inventory": str(inventory)})
        self.state = ReconciliationState.RECONCILED
        self.trading_blocked = False
        self.blocked_reason = None

    reconcile_balances = apply_authoritative_balance

    def observe_order_status(self, order: SimulatedExchangeOrder) -> SimulatedExchangeOrder:
        """Apply an order update without allowing stale state to regress."""
        self._accept_status(order)
        return self._order_status[order.exchange_order_id]

    apply_order_update = observe_order_status

    def mark_network_loss(self) -> None:
        self.trading_blocked = True
        self.blocked_reason = "network_loss"
        self._audit("network_loss")

    def shutdown(self, *, exchange_status_confirmed: bool = False) -> tuple[str, ...]:
        unresolved = tuple(sorted(self.unresolved_orders))
        if not exchange_status_confirmed:
            unresolved = tuple(sorted(set(unresolved) | {
                key for key, order in self.exchange_orders.items()
                if order.status in {"open", "partially_filled"}
            } | set(self.local_orders)))
        self.unresolved_orders = set(unresolved)
        self._audit("shutdown", {"unresolved_orders": len(unresolved)})
        return unresolved

    def _accept_status(self, order: SimulatedExchangeOrder) -> None:
        key = order.exchange_order_id
        previous = self._order_status.get(key)
        if previous is not None:
            old_priority = self._STATUS_PRIORITY.get(previous.status, 0)
            new_priority = self._STATUS_PRIORITY.get(order.status, 0)
            if new_priority < old_priority or (new_priority == old_priority and order.sequence < previous.sequence):
                return
        self._order_status[key] = order

    def _audit(self, event: str, details: Mapping[str, Any] | None = None) -> None:
        self.audits.append(ReconciliationAudit(event, _utc(None), dict(details or {})))


__all__ = [
    "ExecutionFillEvent",
    "ExecutionStateReconciler",
    "ReconciliationAudit",
    "ReconciliationResult",
    "ReconciliationState",
    "SimulatedExchangeOrder",
]
