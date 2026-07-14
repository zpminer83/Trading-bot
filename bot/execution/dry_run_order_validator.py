"""Side-effect-free hypothetical order validation."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
from typing import Any, Mapping

from bot.execution.order import OrderIntent
from bot.integrations.dreamdex_read_only import AccountSnapshot, MarketMetadata, ReconciliationReport


def _floor_step(value: Decimal, step: Decimal | None) -> Decimal:
    if step is None or step <= 0:
        return value
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def _aligned(value: Decimal, step: Decimal | None) -> bool:
    return step is None or step <= 0 or value % step == 0


@dataclass(frozen=True)
class DryRunValidationLimits:
    maximum_notional: Decimal = Decimal("100000")
    maximum_inventory: Decimal = Decimal("100000")

    def __post_init__(self) -> None:
        if self.maximum_notional <= 0:
            raise ValueError("maximum_notional must be > 0")
        if self.maximum_inventory < 0:
            raise ValueError("maximum_inventory must be >= 0")


@dataclass(frozen=True)
class DryRunValidationResult:
    approved: bool
    normalized_price: Decimal
    normalized_quantity: Decimal
    notional: Decimal
    reasons: tuple[str, ...]
    hypothetical_payload: Mapping[str, Any]

    @property
    def rejected(self) -> bool:
        return not self.approved

    @property
    def rejection_reasons(self) -> tuple[str, ...]:
        return self.reasons


class DryRunOrderValidator:
    """Validates an intent without exposing any submit/cancel operation."""

    def __init__(self, limits: DryRunValidationLimits | None = None) -> None:
        self.limits = limits or DryRunValidationLimits()

    def validate(
        self,
        intent: OrderIntent,
        *,
        market: MarketMetadata,
        account: AccountSnapshot,
        reconciliation: ReconciliationReport | None,
        market_fresh: bool,
        fair_play_decision: Any | None = None,
        risk_decision: Any | None = None,
    ) -> DryRunValidationResult:
        price = _floor_step(intent.price, market.price_tick_size)
        quantity = _floor_step(intent.quantity, market.quantity_step_size)
        notional = price * quantity
        reasons: list[str] = []
        if market.symbol != intent.symbol:
            reasons.append("market_not_found")
        if market.status is None:
            reasons.append("market_status_unavailable")
        elif not market.active:
            reasons.append("market_not_active")
        if market.supported_order_types and intent.order_type not in {item.lower() for item in market.supported_order_types}:
            reasons.append("unsupported_order_type")
        if market.price_tick_size and not _aligned(intent.price, market.price_tick_size):
            reasons.append("invalid_price_tick")
        if market.quantity_step_size and not _aligned(intent.quantity, market.quantity_step_size):
            reasons.append("invalid_quantity_step")
        if market.minimum_quantity is not None and quantity < market.minimum_quantity:
            reasons.append("minimum_quantity")
        if market.minimum_notional is not None and notional < market.minimum_notional:
            reasons.append("minimum_notional")
        quote = market.quote_asset or "USDso"
        base = market.base_asset or "SOMI"
        if getattr(account, "incomplete", False):
            reasons.append("incomplete_account_state")
        quote_balance = account.balance(quote)
        base_balance = account.balance(base)
        if quote_balance.available is None:
            reasons.append("quote_balance_unavailable")
        if base_balance.available is None:
            reasons.append("base_balance_unavailable")
        if intent.side == "buy" and quote_balance.available is not None and quote_balance.available < notional:
            reasons.append("insufficient_quote_balance")
        if intent.side == "sell" and base_balance.available is not None and base_balance.available < quantity:
            reasons.append("insufficient_base_balance")
        if notional > self.limits.maximum_notional:
            reasons.append("maximum_notional")
        if base_balance.total is None:
            reasons.append("inventory_unavailable")
            projected_inventory = quantity if intent.side == "buy" else -quantity
        else:
            projected_inventory = base_balance.total + (quantity if intent.side == "buy" else -quantity)
        if projected_inventory < 0:
            reasons.append("negative_inventory")
        if abs(projected_inventory) > self.limits.maximum_inventory:
            reasons.append("maximum_inventory")
        completed = bool(getattr(reconciliation, "completed", False))
        if reconciliation is not None and not hasattr(reconciliation, "completed"):
            completed = getattr(getattr(reconciliation, "state", None), "value", "") == "RECONCILED"
        mismatches = tuple(getattr(reconciliation, "mismatches", ())) if reconciliation is not None else ()
        blocked = bool(getattr(reconciliation, "trading_blocked", True)) if reconciliation is not None else True
        if reconciliation is None or not completed:
            reasons.append("reconciliation_incomplete")
        elif blocked or mismatches:
            reasons.append("reconciliation_blocked")
        if reconciliation is not None and getattr(reconciliation, "unresolved_orders", ()):
            reasons.append("unresolved_order")
        if not market_fresh:
            reasons.append("market_data_stale")
        if fair_play_decision is None:
            reasons.append("fair_play_unavailable")
        elif not bool(fair_play_decision if isinstance(fair_play_decision, bool) else getattr(fair_play_decision, "allowed", False)):
            reasons.append(str(getattr(fair_play_decision, "reason", "fair_play_blocked")))
        if risk_decision is None:
            reasons.append("risk_unavailable")
        elif not bool(risk_decision if isinstance(risk_decision, bool) else getattr(risk_decision, "allowed", False)):
            reasons.append(str(getattr(risk_decision, "reason", "risk_blocked")))
        return DryRunValidationResult(
            approved=not reasons,
            normalized_price=price,
            normalized_quantity=quantity,
            notional=notional,
            reasons=tuple(dict.fromkeys(reasons)),
            hypothetical_payload={
                "symbol": intent.symbol, "side": intent.side, "order_type": intent.order_type,
                "price": str(price), "quantity": str(quantity), "notional": str(notional),
                "purpose": getattr(intent.purpose, "value", str(intent.purpose)),
            },
        )


__all__ = ["DryRunOrderValidator", "DryRunValidationLimits", "DryRunValidationResult", "HypotheticalOrderValidation"]

HypotheticalOrderValidation = DryRunValidationResult
