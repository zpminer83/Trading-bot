"""Pure, fail-closed production-readiness evaluation for DreamDEX.

The module consumes materialised evidence only.  It deliberately has no
environment access, network client, journal handle, signer, or submitter.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from hashlib import sha256
import json
from typing import Any, Sequence

from bot.execution.dreamdex_execution_primitives import (
    build_execution_capability_matrix,
    mask_evm_address,
    mask_hex_hash,
    validate_evm_address,
)

SCHEMA_VERSION = "1"


def _tuple(values: Sequence[str] | None = None) -> tuple[str, ...]:
    return tuple(dict.fromkeys(str(value) for value in (values or ()) if str(value)))


def _fp(value: Any, domain: str) -> str:
    return sha256((domain + ":" + json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)).encode()).hexdigest()


def _ok(value: str, accepted: set[str] | None = None) -> bool:
    return str(value) in (accepted or {"available", "confirmed", "fresh", "clean", "valid", "configured", "enabled", "complete", "source_confirmed", "test_confirmed"})


@dataclass(frozen=True, repr=False)
class DreamDexProductionReadinessPolicy:
    schema_version: str = SCHEMA_VERSION
    required_chain_id: int = 5031
    required_market_address: str | None = None
    required_signer_address: str | None = None
    require_clean_journal: bool = True
    require_schema_v3_journal: bool = True
    require_authoritative_market_state: bool = True
    require_authoritative_account_state: bool = True
    require_market_rules: bool = True
    require_trading_enabled: bool = True
    require_runtime_launch_gate: bool = True
    require_live_preflight: bool = True
    require_nonce_revalidation: bool = True
    require_signing_lease: bool = True
    require_encrypted_keystore_signer: bool = True
    require_secure_secret_provider: bool = True
    require_production_rpc_policy: bool = True
    require_submission_boundary: bool = True
    require_receipt_confirmation: bool = True
    require_contract_event_confirmation: bool = True
    require_reconciliation: bool = True
    require_human_approval: bool = True
    require_post_approval_revalidation: bool = True
    maximum_approval_age_ms: int = 60_000
    maximum_approval_attempts: int = 1
    allow_unattended_approval: bool = False
    allow_approval_persistence: bool = False
    allow_automatic_retry: bool = False
    allow_replacement: bool = False
    allow_real_signing: bool = False
    allow_real_submission: bool = False
    authoritative: bool = False
    unresolved_reasons: tuple[str, ...] = ()
    maximum_drawdown_fraction: Decimal = Decimal("0.10")
    preemptive_drawdown_fraction: Decimal = Decimal("0.08")

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION or self.required_chain_id != 5031:
            raise ValueError("production_readiness_policy_invalid")
        for name in ("required_market_address", "required_signer_address"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, validate_evm_address(value, field=name))
        if isinstance(self.maximum_approval_age_ms, bool) or not isinstance(self.maximum_approval_age_ms, int) or self.maximum_approval_age_ms <= 0:
            raise ValueError("maximum_approval_age_ms_invalid")
        if isinstance(self.maximum_approval_attempts, bool) or self.maximum_approval_attempts != 1:
            raise ValueError("maximum_approval_attempts_must_be_one")
        if self.allow_unattended_approval or self.allow_approval_persistence or self.allow_automatic_retry or self.allow_replacement:
            raise ValueError("unsafe_production_readiness_policy")
        for name in ("maximum_drawdown_fraction", "preemptive_drawdown_fraction"):
            value = Decimal(str(getattr(self, name)))
            if not value.is_finite() or value < 0 or value > 1:
                raise ValueError(f"{name}_invalid")
            object.__setattr__(self, name, value)
        if self.preemptive_drawdown_fraction >= self.maximum_drawdown_fraction:
            raise ValueError("preemptive_drawdown_fraction_invalid")
        object.__setattr__(self, "allow_real_signing", bool(self.allow_real_signing))
        object.__setattr__(self, "allow_real_submission", bool(self.allow_real_submission))
        object.__setattr__(self, "authoritative", False)
        object.__setattr__(self, "unresolved_reasons", _tuple(self.unresolved_reasons))

    @property
    def complete(self) -> bool:
        return self.required_market_address is not None and self.required_signer_address is not None and not self.unresolved_reasons

    def safe_dict(self) -> dict[str, Any]:
        return {
            **{name: getattr(self, name) for name in self.__dataclass_fields__ if name not in {"required_market_address", "required_signer_address"}},
            "required_market_address_masked": mask_evm_address(self.required_market_address),
            "required_signer_address_masked": mask_evm_address(self.required_signer_address),
            "complete": self.complete, "authoritative": False,
        }

    def __repr__(self) -> str:
        return f"DreamDexProductionReadinessPolicy(chain_id=5031, complete={self.complete!r}, real_submission=False)"


@dataclass(frozen=True, repr=False)
class DreamDexProductionReadinessEvidence:
    runtime_launch_status: str = "unavailable"
    market_evidence_status: str = "unavailable"
    account_evidence_status: str = "unavailable"
    market_rules_status: str = "unavailable"
    trading_status: str = "unavailable"
    journal_schema_status: str = "unavailable"
    journal_integrity_status: str = "unavailable"
    journal_recovery_status: str = "unavailable"
    active_intent_status: str = "unavailable"
    nonce_reservation_status: str = "unavailable"
    signing_lease_status: str = "unavailable"
    preflight_status: str = "unavailable"
    signer_implementation_status: str = "unavailable"
    signer_configuration_status: str = "unavailable"
    secret_provider_status: str = "unavailable"
    signer_unlock_status: str = "unavailable"
    production_rpc_policy_status: str = "unavailable"
    production_rpc_chain_status: str = "unavailable"
    submission_boundary_status: str = "unavailable"
    confirmation_status: str = "unavailable"
    reconciliation_status: str = "incomplete"
    human_approval_capability_status: str = "available_offline"
    post_approval_revalidation_status: str = "unavailable"
    production_network_status: str = "not_used"
    conflicts: tuple[str, ...] = ()
    unresolved_reasons: tuple[str, ...] = ()
    risk_control_status: str = "unavailable"
    drawdown_fraction: Decimal | None = None
    preemptive_drawdown_fraction: Decimal | None = None
    kill_switch_latched: bool = False
    entry_halt_latched: bool = False
    emergency_exit_requested: bool = False
    emergency_exit_completed: bool = False

    def __post_init__(self) -> None:
        for name in ("drawdown_fraction", "preemptive_drawdown_fraction"):
            value = getattr(self, name)
            if value is not None:
                parsed = Decimal(str(value))
                if not parsed.is_finite() or parsed < 0 or parsed > 1:
                    raise ValueError(f"{name}_invalid")
                object.__setattr__(self, name, parsed)
        object.__setattr__(self, "conflicts", _tuple(self.conflicts))
        object.__setattr__(self, "unresolved_reasons", _tuple(self.unresolved_reasons))

    def safe_dict(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}

    def __repr__(self) -> str:
        return f"DreamDexProductionReadinessEvidence(runtime={self.runtime_launch_status!r}, journal={self.journal_integrity_status!r}, rpc={self.production_rpc_chain_status!r})"


@dataclass(frozen=True, repr=False)
class DreamDexProductionReadinessDecision:
    schema_version: str
    readiness_status: str
    architecture_ready: bool
    configuration_ready: bool
    market_ready: bool
    account_ready: bool
    journal_ready: bool
    signer_ready: bool
    RPC_ready: bool
    transaction_pipeline_ready: bool
    confirmation_ready: bool
    reconciliation_ready: bool
    human_approval_ready: bool
    allowed_to_prepare_transaction_preview: bool
    allowed_to_request_human_approval: bool
    allowed_to_revalidate_after_approval: bool
    allowed_to_invoke_production_signer: bool
    allowed_to_submit_real_transaction: bool
    readiness_fingerprint: str
    authoritative: bool = False
    blockers: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    unresolved_reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "authoritative", False)
        object.__setattr__(self, "allowed_to_invoke_production_signer", False)
        object.__setattr__(self, "allowed_to_submit_real_transaction", False)
        object.__setattr__(self, "blockers", _tuple(self.blockers))
        object.__setattr__(self, "warnings", _tuple(self.warnings))
        object.__setattr__(self, "unresolved_reasons", _tuple(self.unresolved_reasons))

    def safe_dict(self) -> dict[str, Any]:
        return {**{name: getattr(self, name) for name in self.__dataclass_fields__}, "readiness_fingerprint": mask_hex_hash(self.readiness_fingerprint), "authoritative": False, "allowed_to_invoke_production_signer": False, "allowed_to_submit_real_transaction": False}

    def __repr__(self) -> str:
        return f"DreamDexProductionReadinessDecision(status={self.readiness_status!r}, signer=False, submission=False)"


def evaluate_production_readiness(policy: DreamDexProductionReadinessPolicy, evidence: DreamDexProductionReadinessEvidence) -> DreamDexProductionReadinessDecision:
    if not isinstance(policy, DreamDexProductionReadinessPolicy) or not isinstance(evidence, DreamDexProductionReadinessEvidence):
        raise TypeError("typed_production_readiness_inputs_required")
    capabilities = build_execution_capability_matrix()
    architecture = all(capabilities.by_name(name).status in {"available_offline", "partial"} for name in (
        "production_rpc_policy", "live_execution_session_model", "execution_journal_model", "transaction_receipt_model"))
    configuration = policy.complete
    market = _ok(evidence.market_evidence_status) and (not policy.require_market_rules or _ok(evidence.market_rules_status)) and (not policy.require_trading_enabled or _ok(evidence.trading_status, {"enabled", "confirmed"}))
    account = _ok(evidence.account_evidence_status)
    journal = (not policy.require_schema_v3_journal or _ok(evidence.journal_schema_status, {"v3", "confirmed", "valid"})) and (not policy.require_clean_journal or _ok(evidence.journal_integrity_status, {"clean", "valid"})) and evidence.journal_recovery_status in {"clear", "none", "not_required"} and _ok(evidence.active_intent_status, {"clear", "none", "available"})
    signer = (not policy.require_encrypted_keystore_signer or _ok(evidence.signer_implementation_status)) and _ok(evidence.signer_configuration_status, {"configured", "confirmed"}) and (not policy.require_secure_secret_provider or _ok(evidence.secret_provider_status)) and _ok(evidence.signer_unlock_status, {"verified", "confirmed"})
    rpc = (not policy.require_production_rpc_policy or _ok(evidence.production_rpc_policy_status)) and _ok(evidence.production_rpc_chain_status, {"confirmed", "source_confirmed"})
    pipeline = (not policy.require_runtime_launch_gate or _ok(evidence.runtime_launch_status, {"approved", "pass", "confirmed"})) and (not policy.require_live_preflight or _ok(evidence.preflight_status, {"confirmed", "complete"})) and (not policy.require_nonce_revalidation or _ok(evidence.nonce_reservation_status, {"confirmed", "reserved"})) and (not policy.require_signing_lease or _ok(evidence.signing_lease_status, {"confirmed", "active"})) and (not policy.require_submission_boundary or _ok(evidence.submission_boundary_status))
    confirmation = (not policy.require_receipt_confirmation or _ok(evidence.confirmation_status))
    reconciliation = (not policy.require_reconciliation or _ok(evidence.reconciliation_status, {"complete", "confirmed"}))
    risk_control = (
        _ok(evidence.risk_control_status, {"available", "confirmed", "approved", "within_limit"})
        and evidence.drawdown_fraction is not None
        and evidence.drawdown_fraction < policy.preemptive_drawdown_fraction
        and not evidence.entry_halt_latched
        and not evidence.kill_switch_latched
        and not evidence.emergency_exit_requested
    )
    human = (not policy.require_human_approval or evidence.human_approval_capability_status in {"available_offline", "available", "partial"})
    revalidation = (not policy.require_post_approval_revalidation or evidence.post_approval_revalidation_status in {"available_offline", "available", "partial", "confirmed"})
    blockers: list[str] = []
    for passed, reason in ((configuration, "production_configuration_incomplete"), (market, "market_launch_gate_failed"), (account, "account_launch_gate_failed"), (journal, "execution_journal_unavailable"), (signer, "transaction_signer_unavailable"), (rpc, "production_rpc_policy_incomplete"), (pipeline, "live_execution_preflight_unconfirmed"), (confirmation, "receipt_lookup_unavailable"), (reconciliation, "direct_order_reconciliation_unavailable"), (human, "execution_approval_unavailable"), (revalidation, "post_approval_revalidation_unavailable")):
        if not passed: blockers.append(reason)
    if not risk_control:
        blockers.append("portfolio_risk_control_unavailable")
    if evidence.conflicts: blockers.append("production_readiness_conflict")
    if not policy.allow_real_signing: blockers.append("production_signer_invocation_blocked")
    if not policy.allow_real_submission: blockers.append("real_submission_launch_gate_blocked")
    preview = architecture and configuration and market and account and journal and rpc and pipeline and risk_control
    request_approval = preview and human
    revalidate = request_approval and revalidation
    payload = {"policy": policy.safe_dict(), "evidence": evidence.safe_dict(), "architecture": architecture, "blockers": _tuple(blockers)}
    return DreamDexProductionReadinessDecision(SCHEMA_VERSION, "ready" if revalidate and signer and confirmation and reconciliation else "blocked", architecture, configuration, market, account, journal, signer, rpc, pipeline, confirmation, reconciliation, human, preview, request_approval, revalidate, False, False, _fp(payload, "dreamdex/production-readiness"), False, tuple(blockers), (), _tuple((*policy.unresolved_reasons, *evidence.unresolved_reasons)))


def build_production_readiness_preview(decision: DreamDexProductionReadinessDecision | None = None) -> dict[str, Any]:
    if decision is None:
        return {"architecture_ready": True, "configuration_ready": False, "human_approval_capability": "available_offline", "production_signer_invocation_allowed": False, "real_submission_allowed": False}
    return decision.safe_dict()


def serialize_production_readiness_diagnostics(value: DreamDexProductionReadinessPolicy | DreamDexProductionReadinessEvidence | DreamDexProductionReadinessDecision | None = None) -> dict[str, Any]:
    if value is None:
        return build_production_readiness_preview()
    if not isinstance(value, (DreamDexProductionReadinessPolicy, DreamDexProductionReadinessEvidence, DreamDexProductionReadinessDecision)):
        raise TypeError("unsupported_production_readiness_diagnostics_type")
    return value.safe_dict()


__all__ = [name for name in globals() if name.startswith("DreamDex") or name.startswith("evaluate_") or name.startswith("build_") or name.startswith("serialize_")]
