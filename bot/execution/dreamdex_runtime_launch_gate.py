"""Pure, fail-closed launch gate for DreamDEX execution sessions.

The gate consumes already materialised evidence.  It performs no I/O and does
not calculate market, account, risk, or fair-play metrics itself.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from hashlib import sha256
import json
from typing import Any, Mapping, Sequence

SCHEMA_VERSION = "1"
_STATUSES = frozenset({"pass", "blocked", "unavailable", "not_applicable"})


def _tuple(values: Sequence[str] | None) -> tuple[str, ...]:
    return tuple(dict.fromkeys(str(value) for value in (values or ()) if str(value)))


def _decimal(value: Any, field: str, *, positive: bool = False) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"{field}_invalid")
    try:
        parsed = Decimal(str(value))
    except Exception as exc:
        raise ValueError(f"{field}_invalid") from exc
    if not parsed.is_finite() or parsed < 0 or (positive and parsed <= 0):
        raise ValueError(f"{field}_invalid")
    return parsed


def _fp(value: Any, domain: str = "runtime_launch") -> str:
    body = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return sha256((domain + ":" + body).encode()).hexdigest()


@dataclass(frozen=True, repr=False)
class DreamDexRuntimeLaunchPolicy:
    schema_version: str = SCHEMA_VERSION
    required_chain_id: int | None = None
    required_market_symbol: str | None = None
    required_market_address: str | None = None
    required_signer_address: str | None = None
    maximum_market_data_age_ms: int | None = None
    maximum_account_data_age_ms: int | None = None
    maximum_orderbook_spread_bps: Decimal | None = None
    maximum_orderbook_cross_depth: int | None = None
    minimum_orderbook_depth: int | None = None
    maximum_order_notional: Decimal | None = None
    maximum_position_notional: Decimal | None = None
    maximum_open_orders: int | None = None
    maximum_daily_loss: Decimal | None = None
    maximum_drawdown_fraction: Decimal | None = None
    maximum_transaction_fee_wei: int | None = None
    maximum_active_intents: int | None = None
    maximum_active_nonce_reservations: int | None = None
    maximum_active_signing_leases: int | None = None
    require_market_rules: bool = True
    require_trading_enabled: bool = True
    require_account_identity: bool = True
    require_balance_evidence: bool = True
    require_open_order_evidence: bool = True
    require_clean_journal: bool = True
    require_fair_play_approval: bool = True
    require_risk_approval: bool = True
    require_preflight: bool = True
    require_confirmation: bool = True
    allow_reduce_order: bool = False
    allow_automatic_retry: bool = False
    allow_replacement: bool = False
    allow_real_submission: bool = False
    authoritative: bool = False
    unresolved_reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError("runtime_launch_policy_schema_invalid")
        if self.required_chain_id is not None and (isinstance(self.required_chain_id, bool) or not isinstance(self.required_chain_id, int) or self.required_chain_id < 0):
            raise ValueError("required_chain_id_invalid")
        if not isinstance(self.required_market_symbol, (str, type(None))) or not isinstance(self.required_market_address, (str, type(None))) or not isinstance(self.required_signer_address, (str, type(None))):
            raise ValueError("required_identity_invalid")
        for name in ("maximum_market_data_age_ms", "maximum_account_data_age_ms", "maximum_orderbook_cross_depth", "minimum_orderbook_depth", "maximum_open_orders", "maximum_transaction_fee_wei", "maximum_active_intents", "maximum_active_nonce_reservations", "maximum_active_signing_leases"):
            value = getattr(self, name)
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
                raise ValueError(f"{name}_invalid")
        for name in ("maximum_orderbook_spread_bps", "maximum_order_notional", "maximum_position_notional", "maximum_daily_loss", "maximum_drawdown_fraction"):
            parsed = _decimal(getattr(self, name), name)
            if parsed is not None:
                object.__setattr__(self, name, parsed)
        if self.maximum_drawdown_fraction is not None and self.maximum_drawdown_fraction > 1:
            raise ValueError("maximum_drawdown_fraction_invalid")
        object.__setattr__(self, "allow_reduce_order", bool(self.allow_reduce_order))
        object.__setattr__(self, "allow_automatic_retry", False)
        object.__setattr__(self, "allow_replacement", False)
        object.__setattr__(self, "allow_real_submission", False)
        object.__setattr__(self, "authoritative", False)
        object.__setattr__(self, "unresolved_reasons", _tuple(self.unresolved_reasons))

    @property
    def complete(self) -> bool:
        required = (self.required_chain_id, self.required_market_symbol, self.required_market_address, self.required_signer_address, self.maximum_market_data_age_ms, self.maximum_account_data_age_ms, self.maximum_orderbook_spread_bps, self.maximum_orderbook_cross_depth, self.minimum_orderbook_depth, self.maximum_order_notional, self.maximum_position_notional, self.maximum_open_orders, self.maximum_daily_loss, self.maximum_drawdown_fraction, self.maximum_transaction_fee_wei, self.maximum_active_intents, self.maximum_active_nonce_reservations, self.maximum_active_signing_leases)
        return not self.unresolved_reasons and all(item is not None for item in required)

    def safe_dict(self) -> dict[str, Any]:
        data = {name: getattr(self, name) for name in self.__dataclass_fields__ if name not in {"required_market_address", "required_signer_address"}}
        data["required_market_address_present"] = self.required_market_address is not None
        data["required_signer_address_present"] = self.required_signer_address is not None
        data["complete"] = self.complete
        data["allow_automatic_retry"] = False
        data["allow_replacement"] = False
        data["allow_real_submission"] = False
        data["authoritative"] = False
        return data

    def __repr__(self) -> str:
        return f"DreamDexRuntimeLaunchPolicy(chain_id={self.required_chain_id!r}, market={self.required_market_symbol!r}, complete={self.complete!r}, allow_real_submission=False)"


@dataclass(frozen=True, repr=False)
class DreamDexRuntimeLaunchEvidence:
    market_identity_status: str = "unavailable"
    market_rules_status: str = "unavailable"
    market_trading_status: str = "unavailable"
    market_data_status: str = "unavailable"
    market_data_age_ms: int | None = None
    orderbook_status: str = "unavailable"
    spread_bps: Decimal | None = None
    orderbook_depth_status: str = "unavailable"
    account_identity_status: str = "unavailable"
    account_data_status: str = "unavailable"
    account_data_age_ms: int | None = None
    balance_status: str = "unavailable"
    open_order_status: str = "unavailable"
    open_order_count: int | None = None
    position_status: str = "unavailable"
    position_notional: Decimal | None = None
    daily_pnl_status: str = "unavailable"
    daily_pnl: Decimal | None = None
    drawdown_status: str = "unavailable"
    drawdown_fraction: Decimal | None = None
    fair_play_status: str = "unavailable"
    risk_status: str = "unavailable"
    journal_status: str = "unavailable"
    journal_recovery_required: bool = True
    active_intent_count: int | None = None
    active_nonce_reservation_count: int | None = None
    active_signing_lease_count: int | None = None
    preflight_capability_status: str = "unavailable"
    signer_capability_status: str = "unavailable"
    submission_capability_status: str = "unavailable"
    confirmation_capability_status: str = "unavailable"
    reconciliation_status: str = "incomplete"
    source_authority_status: str = "unavailable"
    conflicts: tuple[str, ...] = ()
    unresolved_reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("market_data_age_ms", "account_data_age_ms", "open_order_count", "active_intent_count", "active_nonce_reservation_count", "active_signing_lease_count"):
            value = getattr(self, name)
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
                raise ValueError(f"{name}_invalid")
        for name in ("spread_bps", "position_notional", "daily_pnl", "drawdown_fraction"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _decimal(value, name))
        object.__setattr__(self, "conflicts", _tuple(self.conflicts))
        object.__setattr__(self, "unresolved_reasons", _tuple(self.unresolved_reasons))

    def safe_dict(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}

    def __repr__(self) -> str:
        return f"DreamDexRuntimeLaunchEvidence(market={self.market_identity_status!r}, account={self.account_identity_status!r}, risk={self.risk_status!r}, authority={self.source_authority_status!r})"


@dataclass(frozen=True, repr=False)
class DreamDexRuntimeLaunchDecision:
    schema_version: str
    decision_status: str
    market_gate_passed: bool
    account_gate_passed: bool
    orderbook_gate_passed: bool
    risk_gate_passed: bool
    fair_play_gate_passed: bool
    journal_gate_passed: bool
    execution_pipeline_gate_passed: bool
    dry_run_gate_passed: bool
    production_signer_gate_passed: bool
    production_submission_gate_passed: bool
    allowed_to_build_intent: bool
    allowed_to_run_synthetic_dry_run: bool
    allowed_to_start_live_preflight: bool
    allowed_to_invoke_production_signer: bool
    allowed_to_submit_real_transaction: bool
    decision_fingerprint: str
    authoritative: bool
    blockers: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    unresolved_reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "authoritative", False)
        object.__setattr__(self, "allowed_to_start_live_preflight", False)
        object.__setattr__(self, "allowed_to_invoke_production_signer", False)
        object.__setattr__(self, "allowed_to_submit_real_transaction", False)
        object.__setattr__(self, "blockers", _tuple(self.blockers)); object.__setattr__(self, "warnings", _tuple(self.warnings)); object.__setattr__(self, "unresolved_reasons", _tuple(self.unresolved_reasons))

    def safe_dict(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}

    def __repr__(self) -> str:
        return f"DreamDexRuntimeLaunchDecision(status={self.decision_status!r}, synthetic={self.allowed_to_run_synthetic_dry_run!r}, real_submission=False)"


@dataclass(frozen=True)
class DreamDexRuntimeChecklistItem:
    area: str
    status: str
    reason: str = ""

    def __post_init__(self) -> None:
        if self.status not in _STATUSES:
            raise ValueError("checklist_status_invalid")


def evaluate_runtime_launch_gate(policy: DreamDexRuntimeLaunchPolicy, evidence: DreamDexRuntimeLaunchEvidence, *, synthetic_dependencies_supplied: bool = False) -> DreamDexRuntimeLaunchDecision:
    if not isinstance(policy, DreamDexRuntimeLaunchPolicy) or not isinstance(evidence, DreamDexRuntimeLaunchEvidence):
        raise TypeError("typed_runtime_inputs_required")
    blockers: list[str] = []
    unresolved: list[str] = list(policy.unresolved_reasons) + list(evidence.unresolved_reasons)
    policy_ok = policy.complete
    if not policy_ok: blockers.append("runtime_launch_policy_incomplete")

    def status_ok(value: str, accepted: set[str] | None = None) -> bool:
        return value in (accepted or {"available", "confirmed", "fresh", "approved", "clean", "pass", "source_confirmed", "test_confirmed"})

    market = policy_ok and status_ok(evidence.market_identity_status) and status_ok(evidence.market_rules_status) and status_ok(evidence.source_authority_status, {"source_confirmed", "authoritative", "confirmed"}) if policy.require_market_rules else policy_ok
    market = market and (status_ok(evidence.market_trading_status, {"enabled", "confirmed", "available"}) if policy.require_trading_enabled else True)
    market = market and status_ok(evidence.market_data_status, {"fresh", "available", "confirmed"}) and evidence.market_data_age_ms is not None and evidence.market_data_age_ms <= (policy.maximum_market_data_age_ms or -1)
    if not market: blockers.append("market_launch_gate_failed")
    orderbook = policy_ok and status_ok(evidence.orderbook_status, {"available", "fresh", "confirmed"}) and status_ok(evidence.orderbook_depth_status, {"sufficient", "available", "confirmed"}) and evidence.spread_bps is not None and policy.maximum_orderbook_spread_bps is not None and evidence.spread_bps <= policy.maximum_orderbook_spread_bps
    if not orderbook: blockers.append("orderbook_launch_gate_failed")
    account = policy_ok and (status_ok(evidence.account_identity_status, {"confirmed", "available", "source_confirmed"}) if policy.require_account_identity else True) and status_ok(evidence.account_data_status, {"fresh", "available", "confirmed"}) and evidence.account_data_age_ms is not None and evidence.account_data_age_ms <= (policy.maximum_account_data_age_ms or -1) and (status_ok(evidence.balance_status) if policy.require_balance_evidence else True) and (status_ok(evidence.open_order_status) if policy.require_open_order_evidence else True)
    if not account: blockers.append("account_launch_gate_failed")
    risk = policy_ok and status_ok(evidence.position_status, {"within_limits", "available", "confirmed"}) and (status_ok(evidence.risk_status, {"approved", "available", "confirmed"}) if policy.require_risk_approval else True) and evidence.position_notional is not None and evidence.position_notional <= (policy.maximum_position_notional or Decimal("-1")) and evidence.open_order_count is not None and evidence.open_order_count <= (policy.maximum_open_orders or -1) and status_ok(evidence.daily_pnl_status, {"within_limit", "available", "confirmed"}) and evidence.daily_pnl is not None and evidence.daily_pnl >= -(policy.maximum_daily_loss or Decimal("-1")) and status_ok(evidence.drawdown_status, {"within_limit", "available", "confirmed"}) and evidence.drawdown_fraction is not None and evidence.drawdown_fraction <= (policy.maximum_drawdown_fraction or Decimal("-1"))
    if not risk: blockers.append("risk_gate_failed")
    fair = policy_ok and (status_ok(evidence.fair_play_status, {"approved", "available", "confirmed"}) if policy.require_fair_play_approval else True)
    if not fair: blockers.append("fair_play_gate_failed")
    journal = policy_ok and status_ok(evidence.journal_status, {"clean", "available", "confirmed"}) and not evidence.journal_recovery_required and evidence.active_intent_count is not None and evidence.active_intent_count <= (policy.maximum_active_intents or -1) and evidence.active_nonce_reservation_count is not None and evidence.active_nonce_reservation_count <= (policy.maximum_active_nonce_reservations or -1) and evidence.active_signing_lease_count is not None and evidence.active_signing_lease_count <= (policy.maximum_active_signing_leases or -1)
    if not journal: blockers.append("journal_launch_gate_failed")
    pipeline = policy_ok and (status_ok(evidence.preflight_capability_status, {"available_offline", "available", "confirmed", "test_confirmed"}) if policy.require_preflight else True) and (status_ok(evidence.signer_capability_status, {"available_offline", "test_only", "available", "confirmed", "test_confirmed"})) and (status_ok(evidence.submission_capability_status, {"available_offline", "test_only", "available", "confirmed", "test_confirmed"})) and (status_ok(evidence.confirmation_capability_status, {"available_offline", "available", "confirmed", "test_confirmed"}) if policy.require_confirmation else True)
    if not pipeline: blockers.append("execution_pipeline_launch_gate_failed")
    prior = policy_ok and market and orderbook and account and risk and fair and journal and pipeline
    dry = prior and synthetic_dependencies_supplied
    if not dry: blockers.append("synthetic_dry_run_unavailable" if not synthetic_dependencies_supplied else "synthetic_dry_run_failed")
    unique = _tuple(blockers)
    payload = {"policy": policy.safe_dict(), "evidence": evidence.safe_dict(), "blockers": unique, "synthetic": dry}
    return DreamDexRuntimeLaunchDecision(SCHEMA_VERSION, "allowed" if dry else "blocked", market, account, orderbook, risk, fair, journal, pipeline, dry, False, False, prior, dry, False, False, False, _fp(payload), False, unique, (), _tuple(unresolved))


def build_runtime_launch_checklist(decision: DreamDexRuntimeLaunchDecision) -> tuple[DreamDexRuntimeChecklistItem, ...]:
    values = (
        ("market", decision.market_gate_passed, False),
        ("account", decision.account_gate_passed, False),
        ("risk", decision.risk_gate_passed, False),
        ("fair_play", decision.fair_play_gate_passed, False),
        ("journal", decision.journal_gate_passed, False),
        ("transaction_preflight", decision.execution_pipeline_gate_passed, False),
        ("signer", decision.production_signer_gate_passed, True),
        ("submission", decision.production_submission_gate_passed, True),
        ("confirmation", decision.execution_pipeline_gate_passed, False),
        ("reconciliation", decision.execution_pipeline_gate_passed, False),
        ("operations", False, True),
        ("synthetic_dry_run", decision.allowed_to_run_synthetic_dry_run, False),
    )
    items = []
    for area, passed, unavailable in values:
        if passed:
            status, reason = "pass", ""
        elif area == "operations":
            status, reason = "not_applicable", "production_operations_disabled"
        elif unavailable:
            status, reason = "unavailable", "capability_unavailable"
        else:
            status, reason = "blocked", "launch_gate_blocked"
        items.append(DreamDexRuntimeChecklistItem(area, status, reason))
    return tuple(items)


def serialize_runtime_launch_diagnostics(value: DreamDexRuntimeLaunchPolicy | DreamDexRuntimeLaunchEvidence | DreamDexRuntimeLaunchDecision | DreamDexRuntimeChecklistItem | Sequence[DreamDexRuntimeChecklistItem]) -> Any:
    if isinstance(value, (DreamDexRuntimeLaunchPolicy, DreamDexRuntimeLaunchEvidence, DreamDexRuntimeLaunchDecision, DreamDexRuntimeChecklistItem)):
        return value.safe_dict() if hasattr(value, "safe_dict") else {"area": value.area, "status": value.status, "reason": value.reason}
    return tuple({"area": item.area, "status": item.status, "reason": item.reason} for item in value)


__all__ = ["SCHEMA_VERSION", "DreamDexRuntimeLaunchPolicy", "DreamDexRuntimeLaunchEvidence", "DreamDexRuntimeLaunchDecision", "DreamDexRuntimeChecklistItem", "evaluate_runtime_launch_gate", "build_runtime_launch_checklist", "serialize_runtime_launch_diagnostics"]
