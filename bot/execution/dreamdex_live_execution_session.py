"""Fail-closed live execution-session boundary.

This module composes existing typed preflight, journal, signing, submission,
confirmation and reconciliation boundaries.  It does not construct fake
production dependencies and it never auto-arms a live session.  Offline tests
may inject deterministic callbacks explicitly.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
from hashlib import sha256
import json
import threading
import time
from typing import Any, Callable, Mapping, Sequence

from bot.execution.dreamdex_execution_primitives import deterministic_fingerprint, mask_evm_address, mask_hex_hash, validate_evm_address

SCHEMA_VERSION = "1"


class DreamDexLiveExecutionState(str, Enum):
    NOT_STARTED = "not_started"
    GATE_REJECTED = "gate_rejected"
    LIVE_EVIDENCE_VALIDATED = "live_evidence_validated"
    PREFLIGHT_COMPLETED = "preflight_completed"
    INTENT_PERSISTED = "intent_persisted"
    NONCE_RESERVED = "nonce_reserved"
    NONCE_REVALIDATED = "nonce_revalidated"
    SIGNING_LEASE_ACQUIRED = "signing_lease_acquired"
    SIGNING_STARTED = "signing_started"
    SIGNED_VERIFIED = "signed_verified"
    SUBMISSION_STARTED = "submission_started"
    SUBMITTED = "submitted"
    CONFIRMATION_PENDING = "confirmation_pending"
    CONFIRMED_SUCCESS = "confirmed_success"
    CONFIRMED_REVERTED = "confirmed_reverted"
    CONFIRMED_MISSING_EVENT = "confirmed_missing_event"
    RECONCILED = "reconciled"
    COMPLETED = "completed"
    SUBMISSION_UNKNOWN = "submission_unknown"
    RECOVERY_REQUIRED = "recovery_required"
    FAILED = "failed"


SESSION_OPERATIONS = frozenset({"place_order", "cancel_order", "reduce_order"})


def _tuple(values: Any) -> tuple[str, ...]:
    return tuple(dict.fromkeys(str(item) for item in (values or ()) if str(item)))


def _fp(value: Any, domain: str) -> str:
    return sha256((domain + ":" + json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)).encode()).hexdigest()


def _value(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


class DreamDexTerminalSessionRegistry:
    """Bounded process-local terminal-session protection.

    Only canonical fingerprints, status and monotonic expiry are retained.
    No dependency/object identity, paths, timestamps or transaction material is
    used.  Callers share an instance explicitly when reconstructing a session.
    """
    def __init__(self, *, maximum_entries: int = 256, entry_ttl_ms: int = 3_600_000) -> None:
        if isinstance(maximum_entries, bool) or maximum_entries < 1 or isinstance(entry_ttl_ms, bool) or entry_ttl_ms <= 0:
            raise ValueError("terminal_registry_limits_invalid")
        self.maximum_entries = int(maximum_entries)
        self.entry_ttl_ms = int(entry_ttl_ms)
        self._entries: dict[str, tuple[str, int]] = {}
        self._lock = threading.RLock()
        self._expired_removed = 0
        self._capacity_reached = False

    @staticmethod
    def canonical_identity(*, session_request_fingerprint: str, operation: str, intent_fingerprint: str | None = None, rehearsal_intent_fingerprint: str | None = None, launch_decision_fingerprint: str | None = None, journal_snapshot_fingerprint: str | None = None, market_evidence_fingerprint: str | None = None, account_evidence_fingerprint: str | None = None) -> str:
        return _fp({"session_request_fingerprint": session_request_fingerprint, "operation": operation, "intent_fingerprint": intent_fingerprint, "rehearsal_intent_fingerprint": rehearsal_intent_fingerprint, "launch_decision_fingerprint": launch_decision_fingerprint, "journal_snapshot_fingerprint": journal_snapshot_fingerprint, "market_evidence_fingerprint": market_evidence_fingerprint, "account_evidence_fingerprint": account_evidence_fingerprint}, "dreamdex/terminal-session")

    def _cleanup(self, now_ms: int) -> int:
        expired = [key for key, (_, expiry) in self._entries.items() if expiry <= now_ms]
        for key in expired:
            self._entries.pop(key, None)
        self._expired_removed += len(expired)
        return len(expired)

    def check_or_record_rejected(self, identity: str, *, now_monotonic_ms: int | None = None) -> tuple[bool, str]:
        now = int(now_monotonic_ms if now_monotonic_ms is not None else time.monotonic() * 1000)
        with self._lock:
            self._cleanup(now)
            if identity in self._entries:
                return True, "live_execution_terminal_session_reused"
            if len(self._entries) >= self.maximum_entries:
                self._capacity_reached = True
                return True, "live_execution_terminal_registry_capacity"
            self._entries[identity] = ("gate_rejected", now + self.entry_ttl_ms)
            return False, ""

    def check(self, identity: str, *, now_monotonic_ms: int | None = None) -> tuple[bool, str]:
        now = int(now_monotonic_ms if now_monotonic_ms is not None else time.monotonic() * 1000)
        with self._lock:
            self._cleanup(now)
            if identity in self._entries:
                return True, "live_execution_terminal_session_reused"
            if len(self._entries) >= self.maximum_entries:
                self._capacity_reached = True
                return True, "live_execution_terminal_registry_capacity"
            return False, ""

    def record_rejected(self, identity: str, *, now_monotonic_ms: int | None = None) -> tuple[bool, str]:
        now = int(now_monotonic_ms if now_monotonic_ms is not None else time.monotonic() * 1000)
        with self._lock:
            self._cleanup(now)
            if identity in self._entries:
                return True, "live_execution_terminal_session_reused"
            if len(self._entries) >= self.maximum_entries:
                self._capacity_reached = True
                return True, "live_execution_terminal_registry_capacity"
            self._entries[identity] = ("gate_rejected", now + self.entry_ttl_ms)
            return False, ""

    def diagnostics(self, *, now_monotonic_ms: int | None = None) -> dict[str, Any]:
        now = int(now_monotonic_ms if now_monotonic_ms is not None else time.monotonic() * 1000)
        with self._lock:
            self._cleanup(now)
            return {"active_entry_count": len(self._entries), "expired_entries_removed": self._expired_removed, "capacity": self.maximum_entries, "capacity_reached": self._capacity_reached, "persistence": "NO"}


# Used only for the extended logical-session identity.  Legacy callers retain
# their dependency-local behavior for compatibility; new callers get process-
# local protection even when dependencies are reconstructed.
_EXTENDED_TERMINAL_SESSION_REGISTRY = DreamDexTerminalSessionRegistry()


@dataclass(frozen=True, repr=False)
class DreamDexLiveExecutionSessionPolicy:
    schema_version: str = SCHEMA_VERSION
    required_chain_id: int = 5031
    required_market_address: str | None = None
    required_signer_address: str | None = None
    allow_place_order: bool = True
    allow_cancel_order: bool = True
    allow_reduce_order: bool = False
    maximum_operations_per_session: int = 2
    maximum_submission_attempts_per_operation: int = 1
    require_runtime_launch_approval: bool = True
    require_authoritative_account_state: bool = True
    require_authoritative_market_state: bool = True
    require_clean_journal: bool = True
    require_live_preflight: bool = True
    require_nonce_revalidation: bool = True
    require_signing_lease: bool = True
    require_signed_transaction_verification: bool = True
    require_exact_submission_hash_match: bool = True
    require_receipt_confirmation: bool = True
    require_expected_contract_event: bool = True
    require_final_reconciliation: bool = True
    allow_automatic_retry: bool = False
    allow_replacement: bool = False
    allow_real_signing: bool = False
    allow_real_submission: bool = False
    authoritative: bool = False
    unresolved_reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError("live_session_policy_schema_invalid")
        if isinstance(self.required_chain_id, bool) or not isinstance(self.required_chain_id, int) or self.required_chain_id != 5031:
            raise ValueError("live_session_chain_id_invalid")
        for name in ("required_market_address", "required_signer_address"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, validate_evm_address(value, field=name))
        for name in ("maximum_operations_per_session", "maximum_submission_attempts_per_operation"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name}_invalid")
        if self.maximum_submission_attempts_per_operation != 1:
            raise ValueError("maximum_submission_attempts_must_be_one")
        if self.allow_automatic_retry or self.allow_replacement:
            raise ValueError("live_session_retry_or_replacement_disabled")
        # Production is never authoritative merely because a local policy was
        # constructed.  Real signing/submission require explicit arming too.
        object.__setattr__(self, "authoritative", False)
        object.__setattr__(self, "unresolved_reasons", _tuple(self.unresolved_reasons))

    @property
    def complete(self) -> bool:
        return self.required_market_address is not None and self.required_signer_address is not None and not self.unresolved_reasons

    def operation_allowed(self, operation: str) -> bool:
        return {"place_order": self.allow_place_order, "cancel_order": self.allow_cancel_order, "reduce_order": self.allow_reduce_order}.get(operation, False)

    def safe_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "required_chain_id": self.required_chain_id,
            "required_market_address_masked": mask_evm_address(self.required_market_address),
            "required_signer_address_masked": mask_evm_address(self.required_signer_address),
            "allow_place_order": self.allow_place_order,
            "allow_cancel_order": self.allow_cancel_order,
            "allow_reduce_order": self.allow_reduce_order,
            "maximum_operations_per_session": self.maximum_operations_per_session,
            "maximum_submission_attempts_per_operation": 1,
            "require_runtime_launch_approval": self.require_runtime_launch_approval,
            "require_authoritative_account_state": self.require_authoritative_account_state,
            "require_authoritative_market_state": self.require_authoritative_market_state,
            "require_clean_journal": self.require_clean_journal,
            "require_live_preflight": self.require_live_preflight,
            "require_nonce_revalidation": self.require_nonce_revalidation,
            "require_signing_lease": self.require_signing_lease,
            "require_signed_transaction_verification": self.require_signed_transaction_verification,
            "require_exact_submission_hash_match": self.require_exact_submission_hash_match,
            "require_receipt_confirmation": self.require_receipt_confirmation,
            "require_expected_contract_event": self.require_expected_contract_event,
            "require_final_reconciliation": self.require_final_reconciliation,
            "allow_automatic_retry": False,
            "allow_replacement": False,
            "allow_real_signing": self.allow_real_signing,
            "allow_real_submission": self.allow_real_submission,
            "authoritative": False,
            "complete": self.complete,
            "unresolved_reasons": self.unresolved_reasons,
        }

    def __repr__(self) -> str:
        return f"DreamDexLiveExecutionSessionPolicy(chain_id=5031, operations={self.maximum_operations_per_session}, real_submission=False)"


@dataclass(frozen=True, repr=False)
class DreamDexExecutionArmingEvidence:
    runtime_launch_approved: bool = False
    journal_clean: bool = False
    market_identity_confirmed: bool = False
    account_identity_confirmed: bool = False
    signer_metadata_confirmed: bool = False
    signer_unlock_verified_current_session: bool = False
    rpc_chain_confirmed: bool = False
    live_preflight_confirmed: bool = False
    nonce_revalidated: bool = False
    signing_lease_active: bool = False
    operation_allowlisted: bool = False
    explicit_session_approval: bool = False
    real_signing_policy_enabled: bool = False
    real_submission_policy_enabled: bool = False
    execution_approval_present: bool = False
    execution_approval_binding_match: bool = False
    execution_approval_current: bool = False
    execution_approval_consumed_for_session: bool = False
    post_approval_revalidation_confirmed: bool = False
    arming_fingerprint: str = ""
    armed_for_signing: bool = False
    armed_for_submission: bool = False
    authoritative: bool = False
    blockers: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "authoritative", False)
        object.__setattr__(self, "blockers", _tuple(self.blockers))
        if not self.arming_fingerprint:
            object.__setattr__(self, "arming_fingerprint", _fp(self.safe_dict(), "dreamdex/arming"))

    def safe_dict(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__ if name != "arming_fingerprint"} | {"arming_fingerprint": mask_hex_hash(self.arming_fingerprint), "authoritative": False}

    def __repr__(self) -> str:
        return f"DreamDexExecutionArmingEvidence(armed_for_signing={self.armed_for_signing!r}, armed_for_submission={self.armed_for_submission!r}, authoritative=False)"


def evaluate_execution_arming(policy: DreamDexLiveExecutionSessionPolicy, evidence: DreamDexExecutionArmingEvidence) -> DreamDexExecutionArmingEvidence:
    if not isinstance(policy, DreamDexLiveExecutionSessionPolicy) or not isinstance(evidence, DreamDexExecutionArmingEvidence):
        raise TypeError("typed_arming_inputs_required")
    checks = (
        ("live_execution_policy_incomplete", policy.complete),
        ("live_execution_launch_unapproved", evidence.runtime_launch_approved),
        ("live_execution_journal_unavailable", evidence.journal_clean),
        ("live_execution_market_evidence_non_authoritative", evidence.market_identity_confirmed),
        ("live_execution_account_evidence_non_authoritative", evidence.account_identity_confirmed),
        ("live_execution_signer_metadata_unconfirmed", evidence.signer_metadata_confirmed),
        ("live_execution_signer_unlock_unverified", evidence.signer_unlock_verified_current_session),
        ("live_execution_rpc_chain_unconfirmed", evidence.rpc_chain_confirmed),
        ("live_execution_preflight_unconfirmed", evidence.live_preflight_confirmed),
        ("live_execution_nonce_unvalidated", evidence.nonce_revalidated),
        ("live_execution_signing_lease_unavailable", evidence.signing_lease_active),
        ("live_execution_operation_not_allowlisted", evidence.operation_allowlisted),
        ("live_execution_explicit_approval_missing", evidence.explicit_session_approval),
        ("execution_approval_unavailable", evidence.execution_approval_present),
        ("execution_approval_binding_mismatch", evidence.execution_approval_binding_match),
        ("execution_approval_expired", evidence.execution_approval_current),
        ("execution_approval_replay_detected", evidence.execution_approval_consumed_for_session),
        ("post_approval_revalidation_failed", evidence.post_approval_revalidation_confirmed),
    )
    blockers = [reason for reason, passed in checks if not passed]
    signing = not blockers and evidence.real_signing_policy_enabled and policy.allow_real_signing
    if not signing:
        blockers.append("live_execution_signing_disabled")
    submission = signing and evidence.real_submission_policy_enabled and policy.allow_real_submission
    if not submission:
        blockers.append("live_execution_submission_disabled")
    result = replace(evidence, armed_for_signing=signing, armed_for_submission=submission, blockers=tuple(dict.fromkeys(blockers)), authoritative=False)
    object.__setattr__(result, "arming_fingerprint", _fp({"policy": policy.safe_dict(), "evidence": result.safe_dict()}, "dreamdex/arming"))
    return result


@dataclass(frozen=True, repr=False)
class DreamDexLiveExecutionSessionRequest:
    schema_version: str
    operation: str
    market_address: str
    signer_address: str
    unsigned_request_fingerprint: str
    launch_decision_fingerprint: str
    journal_snapshot_fingerprint: str
    session_request_fingerprint: str = ""
    intent_fingerprint: str | None = None
    rehearsal_intent_fingerprint: str | None = None
    market_evidence_fingerprint: str | None = None
    account_evidence_fingerprint: str | None = None
    authoritative: bool = False
    blockers: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION or self.operation not in SESSION_OPERATIONS:
            raise ValueError("live_session_request_invalid")
        object.__setattr__(self, "market_address", validate_evm_address(self.market_address, field="market_address"))
        object.__setattr__(self, "signer_address", validate_evm_address(self.signer_address, field="signer_address"))
        for name in ("unsigned_request_fingerprint", "launch_decision_fingerprint", "journal_snapshot_fingerprint"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise ValueError(f"{name}_required")
        for name in ("intent_fingerprint", "rehearsal_intent_fingerprint", "market_evidence_fingerprint", "account_evidence_fingerprint"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not value):
                raise ValueError(f"{name}_invalid")
        object.__setattr__(self, "authoritative", False)
        object.__setattr__(self, "blockers", _tuple(self.blockers))
        if not self.session_request_fingerprint:
            object.__setattr__(self, "session_request_fingerprint", _fp({"operation": self.operation, "market": self.market_address, "signer": self.signer_address, "unsigned": self.unsigned_request_fingerprint, "launch": self.launch_decision_fingerprint, "journal": self.journal_snapshot_fingerprint}, "dreamdex/live-session-request"))

    def safe_dict(self) -> dict[str, Any]:
        return {"schema_version": self.schema_version, "operation": self.operation, "market_address_masked": mask_evm_address(self.market_address), "signer_address_masked": mask_evm_address(self.signer_address), "unsigned_request_fingerprint": mask_hex_hash(self.unsigned_request_fingerprint), "launch_decision_fingerprint": mask_hex_hash(self.launch_decision_fingerprint), "journal_snapshot_fingerprint": mask_hex_hash(self.journal_snapshot_fingerprint), "session_request_fingerprint": mask_hex_hash(self.session_request_fingerprint), "intent_fingerprint": mask_hex_hash(self.intent_fingerprint), "rehearsal_intent_fingerprint": mask_hex_hash(self.rehearsal_intent_fingerprint), "market_evidence_fingerprint": mask_hex_hash(self.market_evidence_fingerprint), "account_evidence_fingerprint": mask_hex_hash(self.account_evidence_fingerprint), "authoritative": False, "blockers": self.blockers}

    def __repr__(self) -> str:
        return f"DreamDexLiveExecutionSessionRequest(operation={self.operation!r}, market={mask_evm_address(self.market_address)!r}, signer={mask_evm_address(self.signer_address)!r})"


def build_live_execution_session_request(*, policy: DreamDexLiveExecutionSessionPolicy, operation: str, market_address: str, signer_address: str, unsigned_request_fingerprint: str, launch_decision_fingerprint: str, journal_snapshot_fingerprint: str, intent_fingerprint: str | None = None, rehearsal_intent_fingerprint: str | None = None, market_evidence_fingerprint: str | None = None, account_evidence_fingerprint: str | None = None) -> DreamDexLiveExecutionSessionRequest:
    blockers: list[str] = []
    if not policy.complete:
        blockers.append("live_execution_policy_incomplete")
    if operation not in SESSION_OPERATIONS or not policy.operation_allowed(operation):
        blockers.append("live_execution_operation_not_allowlisted")
    if policy.required_market_address is None or market_address.lower() != policy.required_market_address.lower():
        blockers.append("live_execution_market_mismatch")
    if policy.required_signer_address is None or signer_address.lower() != policy.required_signer_address.lower():
        blockers.append("live_execution_signer_mismatch")
    return DreamDexLiveExecutionSessionRequest(SCHEMA_VERSION, operation, market_address, signer_address, unsigned_request_fingerprint, launch_decision_fingerprint, journal_snapshot_fingerprint, intent_fingerprint=intent_fingerprint, rehearsal_intent_fingerprint=rehearsal_intent_fingerprint, market_evidence_fingerprint=market_evidence_fingerprint, account_evidence_fingerprint=account_evidence_fingerprint, blockers=tuple(blockers))


@dataclass(frozen=True, repr=False)
class DreamDexLiveExecutionStageResult:
    stage: str
    status: str
    execution_performed: bool
    network_execution_performed: bool
    journal_mutation_performed: bool
    signer_invocation_performed: bool
    submission_call_performed: bool
    input_fingerprint: str
    output_fingerprint: str
    journal_state: str
    blockers: tuple[str, ...] = ()
    validation_errors: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "blockers", _tuple(self.blockers)); object.__setattr__(self, "validation_errors", _tuple(self.validation_errors))

    def safe_dict(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__ if name not in {"input_fingerprint", "output_fingerprint"}} | {"input_fingerprint": mask_hex_hash(self.input_fingerprint), "output_fingerprint": mask_hex_hash(self.output_fingerprint)}


@dataclass(frozen=True, repr=False)
class DreamDexLiveExecutionSessionResult:
    schema_version: str
    session_id: str
    operation: str
    final_state: str
    stage_results: tuple[DreamDexLiveExecutionStageResult, ...]
    intent_id: str | None = None
    reservation_id: str | None = None
    lease_id: str | None = None
    transaction_hash: str | None = None
    confirmed_order_identity_status: str = "unavailable"
    journal_integrity_status: str = "unavailable"
    reconciliation_status: str = "incomplete"
    signer_invocation_count: int = 0
    submission_call_count: int = 0
    receipt_observation_count: int = 0
    automatic_retry_count: int = 0
    replacement_count: int = 0
    production_network_used: bool = False
    production_secret_used: bool = False
    real_transaction_signed: bool = False
    real_transaction_submitted: bool = False
    session_fingerprint: str = ""
    authoritative: bool = False
    completed: bool = False
    recovery_required: bool = False
    blockers: tuple[str, ...] = ()
    validation_errors: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION or self.operation not in SESSION_OPERATIONS:
            raise ValueError("live_session_result_invalid")
        if self.final_state not in {item.value for item in DreamDexLiveExecutionState}:
            raise ValueError("live_session_state_invalid")
        for name in ("signer_invocation_count", "submission_call_count", "receipt_observation_count", "automatic_retry_count", "replacement_count"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name}_invalid")
        if self.automatic_retry_count != 0 or self.replacement_count != 0:
            raise ValueError("live_session_retry_replacement_forbidden")
        if self.production_network_used or self.production_secret_used:
            # The module may orchestrate a test-armed fake transport, but it
            # never labels that as production execution.
            raise ValueError("production_execution_not_allowed_in_offline_session")
        object.__setattr__(self, "authoritative", False)
        object.__setattr__(self, "blockers", _tuple(self.blockers)); object.__setattr__(self, "validation_errors", _tuple(self.validation_errors))
        if not self.session_fingerprint:
            object.__setattr__(self, "session_fingerprint", _fp({"session_id": self.session_id, "operation": self.operation, "state": self.final_state, "stages": tuple((item.stage, item.status, item.output_fingerprint) for item in self.stage_results), "blockers": self.blockers}, "dreamdex/live-session-result"))

    def safe_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "session_id": mask_hex_hash(self.session_id),
            "operation": self.operation,
            "final_state": self.final_state,
            "stage_results": tuple(item.safe_dict() for item in self.stage_results),
            "intent_id": mask_hex_hash(self.intent_id), "reservation_id": mask_hex_hash(self.reservation_id), "lease_id": mask_hex_hash(self.lease_id), "transaction_hash": mask_hex_hash(self.transaction_hash),
            "confirmed_order_identity_status": self.confirmed_order_identity_status,
            "journal_integrity_status": self.journal_integrity_status,
            "reconciliation_status": self.reconciliation_status,
            "signer_invocation_count": self.signer_invocation_count,
            "submission_call_count": self.submission_call_count,
            "receipt_observation_count": self.receipt_observation_count,
            "automatic_retry_count": 0, "replacement_count": 0,
            "production_network_used": False, "production_secret_used": False,
            "real_transaction_signed": self.real_transaction_signed,
            "real_transaction_submitted": self.real_transaction_submitted,
            "session_fingerprint": mask_hex_hash(self.session_fingerprint),
            "authoritative": False, "completed": self.completed, "recovery_required": self.recovery_required,
            "blockers": self.blockers, "validation_errors": self.validation_errors,
        }

    def __repr__(self) -> str:
        return f"DreamDexLiveExecutionSessionResult(state={self.final_state!r}, completed={self.completed!r}, recovery_required={self.recovery_required!r})"


@dataclass(frozen=True)
class DreamDexLiveExecutionSessionDependencies:
    """Explicit callback boundary for deterministic integration tests."""
    preflight: Callable[[DreamDexLiveExecutionSessionRequest], Any] | None = None
    persist_intent: Callable[[DreamDexLiveExecutionSessionRequest, Any], Any] | None = None
    reserve_nonce: Callable[[DreamDexLiveExecutionSessionRequest, Any], Any] | None = None
    revalidate_nonce: Callable[[DreamDexLiveExecutionSessionRequest, Any], Any] | None = None
    acquire_signing_lease: Callable[[DreamDexLiveExecutionSessionRequest, Any], Any] | None = None
    sign_and_verify: Callable[[DreamDexLiveExecutionSessionRequest, Any], Any] | None = None
    submit_once: Callable[[DreamDexLiveExecutionSessionRequest, Any], Any] | None = None
    confirm: Callable[[DreamDexLiveExecutionSessionRequest, Any], Any] | None = None
    reconcile: Callable[[DreamDexLiveExecutionSessionRequest, Any], Any] | None = None
    mark_recovery_required: Callable[[DreamDexLiveExecutionSessionRequest, str], Any] | None = None
    runtime_evidence: Any = None
    runtime_launch_decision: Any = None
    unsigned_request: Any = None
    finalized_envelope: Any = None
    rpc: Any = None
    journal: Any = None
    signer: Any = None
    submitter: Any = None
    receipt_reader: Any = None
    reconciliation_builder: Any = None
    clock: Any = None
    # Process-local only.  It is intentionally not persisted and records that a
    # rejected request cannot be upgraded/reused by the same ceremony object.
    terminal_session_fingerprints: set[str] = field(default_factory=set, compare=False, repr=False)
    terminal_session_registry: DreamDexTerminalSessionRegistry | None = field(default=None, compare=False, repr=False)

    def complete(self) -> bool:
        return all(callable(getattr(self, name)) for name in ("preflight", "persist_intent", "reserve_nonce", "revalidate_nonce", "acquire_signing_lease", "sign_and_verify", "submit_once", "confirm", "reconcile"))


def _stage(results: list[DreamDexLiveExecutionStageResult], stage: str, status: str, value: Any, *, state: str, execution_performed: bool = True, network: bool = False, journal: bool = False, signer: bool = False, submit: bool = False, blockers: Sequence[str] = (), errors: Sequence[str] = ()) -> None:
    results.append(DreamDexLiveExecutionStageResult(stage, status, execution_performed, network, journal, signer, submit, _fp({"stage": stage}, "dreamdex/live-input"), _fp({"stage": stage, "value": value}, "dreamdex/live-output"), state, tuple(blockers), tuple(errors)))


def _result(*, request: DreamDexLiveExecutionSessionRequest, state: DreamDexLiveExecutionState, stages: Sequence[DreamDexLiveExecutionStageResult], blockers: Sequence[str] = (), validation_errors: Sequence[str] = (), intent_id: str | None = None, reservation_id: str | None = None, lease_id: str | None = None, tx_hash: str | None = None, order_status: str = "unavailable", journal_status: str = "unavailable", reconciliation: str = "incomplete", signer_count: int = 0, submit_count: int = 0, receipt_count: int = 0, completed: bool = False, recovery: bool = False) -> DreamDexLiveExecutionSessionResult:
    return DreamDexLiveExecutionSessionResult(SCHEMA_VERSION, _fp({"request": request.session_request_fingerprint}, "dreamdex/live-session-id"), request.operation, state.value, tuple(stages), intent_id, reservation_id, lease_id, tx_hash, order_status, journal_status, reconciliation, signer_count, submit_count, receipt_count, 0, 0, False, False, False, False, "", False, completed, recovery, tuple(dict.fromkeys((*request.blockers, *blockers))), tuple(validation_errors))


def run_live_execution_session(*, policy: DreamDexLiveExecutionSessionPolicy, arming_evidence: DreamDexExecutionArmingEvidence | None = None, request: DreamDexLiveExecutionSessionRequest, dependencies: DreamDexLiveExecutionSessionDependencies | None = None, runtime_evidence: Any = None, runtime_launch_decision: Any = None, monotonic_fn: Callable[[], float] = time.monotonic) -> DreamDexLiveExecutionSessionResult:
    """Run one explicitly armed session; default configuration is inert."""
    if not isinstance(policy, DreamDexLiveExecutionSessionPolicy) or not isinstance(request, DreamDexLiveExecutionSessionRequest):
        raise TypeError("typed_live_session_inputs_required")
    if arming_evidence is None:
        decision = runtime_launch_decision
        evidence = runtime_evidence
        arming_evidence = DreamDexExecutionArmingEvidence(
            runtime_launch_approved=bool(_value(decision, "allowed_to_invoke_production_signer", False) or _value(decision, "allowed_to_run_live_execution", False)),
            journal_clean=str(_value(evidence, "journal_status", "")) == "clean",
            market_identity_confirmed=str(_value(evidence, "market_identity_status", "")) in {"confirmed", "source_confirmed"},
            account_identity_confirmed=str(_value(evidence, "account_identity_status", "")) in {"confirmed", "source_confirmed"},
            signer_metadata_confirmed=str(_value(evidence, "signer_metadata_status", "")) in {"confirmed", "valid"},
            signer_unlock_verified_current_session=str(_value(evidence, "keystore_unlock_status", "")) in {"verified", "confirmed"},
            rpc_chain_confirmed=str(_value(evidence, "rpc_chain_status", "")) in {"confirmed", "source_confirmed"},
            live_preflight_confirmed=str(_value(evidence, "preflight_status", "")) in {"completed", "confirmed"},
            nonce_revalidated=bool(_value(evidence, "nonce_revalidated", False)),
            signing_lease_active=bool(_value(evidence, "signing_lease_active", False)),
            operation_allowlisted=True,
            explicit_session_approval=False,
            real_signing_policy_enabled=False,
            real_submission_policy_enabled=False,
        )
    if not isinstance(arming_evidence, DreamDexExecutionArmingEvidence):
        raise TypeError("typed_arming_evidence_required")
    armed = evaluate_execution_arming(policy, arming_evidence)
    stages: list[DreamDexLiveExecutionStageResult] = []
    terminal_identity = DreamDexTerminalSessionRegistry.canonical_identity(session_request_fingerprint=request.session_request_fingerprint, operation=request.operation, intent_fingerprint=request.intent_fingerprint, rehearsal_intent_fingerprint=request.rehearsal_intent_fingerprint, launch_decision_fingerprint=request.launch_decision_fingerprint, journal_snapshot_fingerprint=request.journal_snapshot_fingerprint, market_evidence_fingerprint=request.market_evidence_fingerprint, account_evidence_fingerprint=request.account_evidence_fingerprint)
    registry = None
    if dependencies is not None:
        registry = dependencies.terminal_session_registry
    if registry is None and (request.intent_fingerprint or request.rehearsal_intent_fingerprint or request.market_evidence_fingerprint or request.account_evidence_fingerprint):
        registry = _EXTENDED_TERMINAL_SESSION_REGISTRY
    if registry is not None:
        reused, reason = registry.check(terminal_identity)
        if reused:
            _stage(stages, "session_reuse", "blocked", None, state=DreamDexLiveExecutionState.GATE_REJECTED.value, execution_performed=False, blockers=(reason,))
            return _result(request=request, state=DreamDexLiveExecutionState.GATE_REJECTED, stages=stages, blockers=(reason,))
    if dependencies is not None and request.session_request_fingerprint in dependencies.terminal_session_fingerprints:
        _stage(stages, "session_reuse", "blocked", None, state=DreamDexLiveExecutionState.GATE_REJECTED.value, execution_performed=False, blockers=("live_execution_terminal_session_reused",))
        return _result(request=request, state=DreamDexLiveExecutionState.GATE_REJECTED, stages=stages, blockers=("live_execution_terminal_session_reused",))
    if request.blockers or not armed.armed_for_submission:
        reasons = tuple(dict.fromkeys((*armed.blockers, "live_execution_launch_unapproved" if request.blockers else "")))
        reasons = tuple(item for item in reasons if item)
        _stage(stages, "arming", "blocked", None, state=DreamDexLiveExecutionState.GATE_REJECTED.value, execution_performed=False, blockers=reasons)
        if dependencies is not None:
            dependencies.terminal_session_fingerprints.add(request.session_request_fingerprint)
            if registry is not None:
                registry.record_rejected(terminal_identity)
        return _result(request=request, state=DreamDexLiveExecutionState.GATE_REJECTED, stages=stages, blockers=reasons)
    if dependencies is None or not dependencies.complete():
        _stage(stages, "dependencies", "blocked", None, state=DreamDexLiveExecutionState.GATE_REJECTED.value, execution_performed=False, blockers=("live_execution_dependencies_incomplete",))
        return _result(request=request, state=DreamDexLiveExecutionState.GATE_REJECTED, stages=stages, blockers=("live_execution_dependencies_incomplete",))
    started = monotonic_fn()
    current: Any = None
    intent_id = reservation_id = lease_id = tx_hash = None
    signer_count = submit_count = receipt_count = 0
    try:
        current = dependencies.preflight(request)  # type: ignore[misc]
        _stage(stages, "live_preflight", "completed", current, state=DreamDexLiveExecutionState.PREFLIGHT_COMPLETED.value, network=True)
        current = dependencies.persist_intent(request, current)  # type: ignore[misc]
        intent_id = str(_value(current, "intent_id", _value(current, "id", "")) or "") or None
        _stage(stages, "intent_persisted", "completed", current, state=DreamDexLiveExecutionState.INTENT_PERSISTED.value, journal=True)
        current = dependencies.reserve_nonce(request, current)  # type: ignore[misc]
        reservation_id = str(_value(current, "reservation_id", "")) or None
        _stage(stages, "nonce_reserved", "completed", current, state=DreamDexLiveExecutionState.NONCE_RESERVED.value, journal=True)
        current = dependencies.revalidate_nonce(request, current)  # type: ignore[misc]
        _stage(stages, "nonce_revalidated", "completed", current, state=DreamDexLiveExecutionState.NONCE_REVALIDATED.value, network=True)
        current = dependencies.acquire_signing_lease(request, current)  # type: ignore[misc]
        lease_id = str(_value(current, "lease_id", "")) or None
        _stage(stages, "signing_lease_acquired", "completed", current, state=DreamDexLiveExecutionState.SIGNING_LEASE_ACQUIRED.value, journal=True)
        _stage(stages, "signing_started", "started", current, state=DreamDexLiveExecutionState.SIGNING_STARTED.value, journal=True)
        signer_count = 1
        current = dependencies.sign_and_verify(request, current)  # type: ignore[misc]
        _stage(stages, "signed_verified", "completed", current, state=DreamDexLiveExecutionState.SIGNED_VERIFIED.value, signer=True, journal=True)
        signed_hash = _value(current, "transaction_hash", _value(current, "signed_transaction_hash"))
        _stage(stages, "submission_started", "started", current, state=DreamDexLiveExecutionState.SUBMISSION_STARTED.value, network=True, journal=True, submit=True)
        submit_count = 1
        current = dependencies.submit_once(request, current)  # type: ignore[misc]
        tx_hash = str(_value(current, "transaction_hash", _value(current, "rpc_returned_transaction_hash", signed_hash))) if _value(current, "transaction_hash", _value(current, "rpc_returned_transaction_hash", signed_hash)) else None
        if signed_hash is not None and tx_hash is not None and str(signed_hash).lower() != str(tx_hash).lower():
            raise RuntimeError("rpc_hash_mismatch")
        _stage(stages, "submitted", "completed", current, state=DreamDexLiveExecutionState.SUBMITTED.value, network=True, journal=True, submit=True)
        _stage(stages, "confirmation_pending", "pending", current, state=DreamDexLiveExecutionState.CONFIRMATION_PENDING.value, network=True)
        current = dependencies.confirm(request, current)  # type: ignore[misc]
        receipt_count = 1
        confirm_status = str(_value(current, "status", ""))
        if confirm_status not in {"confirmed_success", "success", "confirmed"}:
            raise RuntimeError("confirmation_failed")
        _stage(stages, "confirmed_success", "completed", current, state=DreamDexLiveExecutionState.CONFIRMED_SUCCESS.value, network=True, journal=True)
        current = dependencies.reconcile(request, current)  # type: ignore[misc]
        if str(_value(current, "status", "")) not in {"complete", "reconciled", "confirmed"}:
            raise RuntimeError("reconciliation_incomplete")
        _stage(stages, "reconciled", "completed", current, state=DreamDexLiveExecutionState.RECONCILED.value, journal=True)
        return _result(request=request, state=DreamDexLiveExecutionState.COMPLETED, stages=stages, intent_id=intent_id, reservation_id=reservation_id, lease_id=lease_id, tx_hash=tx_hash, order_status="confirmed", journal_status="clean", reconciliation="complete", signer_count=signer_count, submit_count=submit_count, receipt_count=receipt_count, completed=True)
    except Exception as exc:
        category = type(exc).__name__
        if dependencies.mark_recovery_required is not None:
            try:
                dependencies.mark_recovery_required(request, category)
            except Exception:
                pass
        if stages and stages[-1].stage == "submission_started":
            state = DreamDexLiveExecutionState.SUBMISSION_UNKNOWN
        elif signer_count or any(item.stage in {"intent_persisted", "nonce_reserved", "nonce_revalidated", "signing_lease_acquired", "signing_started"} for item in stages):
            state = DreamDexLiveExecutionState.RECOVERY_REQUIRED
        else:
            state = DreamDexLiveExecutionState.FAILED
        _stage(stages, "failed", "failed", None, state=state.value, blockers=("live_execution_recovery_required",), errors=(category,))
        recovery = state in {DreamDexLiveExecutionState.SUBMISSION_UNKNOWN, DreamDexLiveExecutionState.RECOVERY_REQUIRED}
        blockers = ("live_execution_recovery_required",) if recovery else ("live_execution_stage_failed",)
        return _result(request=request, state=state, stages=stages, blockers=blockers, validation_errors=(category,), intent_id=intent_id, reservation_id=reservation_id, lease_id=lease_id, tx_hash=tx_hash, journal_status="recovery_required" if recovery else "clean", signer_count=signer_count, submit_count=submit_count, receipt_count=receipt_count, recovery=recovery)
    finally:
        _ = started


def cancel_live_execution_session(*, result: DreamDexLiveExecutionSessionResult) -> DreamDexLiveExecutionSessionResult:
    if not isinstance(result, DreamDexLiveExecutionSessionResult):
        raise TypeError("typed_live_session_result_required")
    if result.final_state in {DreamDexLiveExecutionState.COMPLETED.value, DreamDexLiveExecutionState.CONFIRMED_SUCCESS.value, DreamDexLiveExecutionState.CONFIRMED_REVERTED.value, DreamDexLiveExecutionState.RECONCILED.value}:
        return replace(result, blockers=tuple(dict.fromkeys((*result.blockers, "live_execution_cancel_not_allowed_after_signing"))))
    if result.final_state in {DreamDexLiveExecutionState.NOT_STARTED.value, DreamDexLiveExecutionState.GATE_REJECTED.value, DreamDexLiveExecutionState.LIVE_EVIDENCE_VALIDATED.value, DreamDexLiveExecutionState.PREFLIGHT_COMPLETED.value, DreamDexLiveExecutionState.INTENT_PERSISTED.value, DreamDexLiveExecutionState.NONCE_RESERVED.value, DreamDexLiveExecutionState.NONCE_REVALIDATED.value, DreamDexLiveExecutionState.SIGNING_LEASE_ACQUIRED.value}:
        return replace(result, final_state=DreamDexLiveExecutionState.FAILED.value, completed=False, blockers=tuple(dict.fromkeys((*result.blockers, "live_execution_cancelled_before_signing"))))
    return replace(result, final_state=DreamDexLiveExecutionState.RECOVERY_REQUIRED.value, completed=False, recovery_required=True, blockers=tuple(dict.fromkeys((*result.blockers, "live_execution_recovery_required"))))


def build_live_execution_session_preview(result: DreamDexLiveExecutionSessionResult | None = None) -> dict[str, Any]:
    if result is None:
        return {"live_session_model": "available_offline", "session_execution_performed": False, "armed_for_signing": False, "armed_for_submission": False, "production_network_used": False, "production_secret_used": False, "real_transaction_signed": False, "real_transaction_submitted": False, "automatic_retry_count": 0, "replacement_count": 0, "blockers": ("live_execution_session_unavailable",)}
    return result.safe_dict()


def serialize_live_execution_session_diagnostics(value: DreamDexLiveExecutionSessionResult | DreamDexExecutionArmingEvidence | Mapping[str, Any] | None = None) -> dict[str, Any]:
    if value is None:
        return build_live_execution_session_preview()
    if hasattr(value, "safe_dict"):
        return value.safe_dict()  # type: ignore[no-any-return]
    if isinstance(value, Mapping):
        forbidden = {"raw_signed_transaction", "raw_transaction", "private_key", "password", "rpc_url", "rpc_payload", "rpc_response"}
        if forbidden & set(value):
            raise ValueError("live_session_sensitive_diagnostic_forbidden")
        return dict(value)
    raise TypeError("unsupported_live_session_diagnostics_type")


__all__ = [
    "SCHEMA_VERSION", "DreamDexLiveExecutionState", "DreamDexLiveExecutionSessionPolicy",
    "DreamDexExecutionArmingEvidence", "DreamDexLiveExecutionSessionRequest",
    "DreamDexLiveExecutionStageResult", "DreamDexLiveExecutionSessionResult",
    "DreamDexLiveExecutionSessionDependencies", "evaluate_execution_arming",
    "build_live_execution_session_request", "run_live_execution_session",
    "cancel_live_execution_session", "build_live_execution_session_preview",
    "serialize_live_execution_session_diagnostics",
]
