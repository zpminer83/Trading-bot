"""Offline-safe live nonce revalidation and local signing lease boundary.

This module is intentionally the last step before a future signer.  It only
uses the typed read-only RPC methods ``eth_chainId`` and pending nonce, and the
existing SQLite journal.  It never signs, serializes, submits or polls a
transaction.
"""
from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Any

from bot.execution.dreamdex_execution_journal import (
    DreamDexExecutionIntent,
    DreamDexExecutionJournal,
    DreamDexNonceReservation,
    JournalState,
)
from bot.execution.dreamdex_execution_primitives import (
    deterministic_fingerprint,
    mask_evm_address,
    mask_hex_hash,
    validate_evm_address,
    validate_uint,
)
from bot.execution.dreamdex_readonly_rpc import DreamDexReadOnlyRpc
from bot.execution.dreamdex_transaction_envelope import DreamDexUnsignedTransactionEnvelope, validate_unsigned_transaction_envelope
from bot.execution.dreamdex_transaction_signer import (
    DreamDexTransactionSigningPolicy,
    DreamDexTransactionSigningRequest,
    validate_transaction_signing_policy,
    validate_transaction_signing_request,
)

SCHEMA_VERSION = "1"
SOURCE_STATUSES = frozenset({"source_confirmed", "unavailable", "invalid"})
LEASE_STATUSES = frozenset({"unavailable", "blocked", "conflict", "acquired"})


def _now_ms() -> int:
    return int(time.time() * 1000)


def _tuple(values: Any) -> tuple[str, ...]:
    return tuple(dict.fromkeys(str(value) for value in (values or ()) if str(value)))


def _address(value: Any, field: str) -> str | None:
    if value is None:
        return None
    return validate_evm_address(value, field=field)


@dataclass(frozen=True, repr=False)
class DreamDexLiveNonceRevalidationPolicy:
    schema_version: str = SCHEMA_VERSION
    required_chain_id: int | None = None
    required_signer_address: str | None = None
    maximum_observation_age_ms: int | None = None
    require_pending_tag: bool = True
    require_exact_nonce_match: bool = True
    require_clean_journal: bool = True
    require_active_reservation: bool = True
    require_finalized_envelope_match: bool = True
    require_signing_request_match: bool = True
    require_signing_policy_approved: bool = True
    maximum_active_signing_leases_per_signer: int = 1
    block_on_any_recovery_required: bool = True
    authoritative: bool = False
    unresolved_reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError("unsupported_signing_lease_schema_version")
        if self.required_chain_id is not None:
            validate_uint(self.required_chain_id, field="required_chain_id")
        if self.required_signer_address is not None:
            object.__setattr__(self, "required_signer_address", _address(self.required_signer_address, "required_signer_address"))
        if self.maximum_observation_age_ms is not None and (isinstance(self.maximum_observation_age_ms, bool) or not isinstance(self.maximum_observation_age_ms, int) or self.maximum_observation_age_ms < 0):
            raise ValueError("maximum_observation_age_ms_invalid")
        if isinstance(self.maximum_active_signing_leases_per_signer, bool) or not isinstance(self.maximum_active_signing_leases_per_signer, int) or self.maximum_active_signing_leases_per_signer < 1:
            raise ValueError("maximum_active_signing_leases_per_signer_invalid")
        object.__setattr__(self, "unresolved_reasons", _tuple(self.unresolved_reasons))
        object.__setattr__(self, "authoritative", False)

    def safe_dict(self) -> dict[str, Any]:
        return {"schema_version": self.schema_version, "required_chain_id": self.required_chain_id, "required_signer_address_masked": mask_evm_address(self.required_signer_address), "maximum_observation_age_ms": self.maximum_observation_age_ms, "require_pending_tag": self.require_pending_tag, "require_exact_nonce_match": self.require_exact_nonce_match, "require_clean_journal": self.require_clean_journal, "require_active_reservation": self.require_active_reservation, "require_finalized_envelope_match": self.require_finalized_envelope_match, "require_signing_request_match": self.require_signing_request_match, "require_signing_policy_approved": self.require_signing_policy_approved, "maximum_active_signing_leases_per_signer": self.maximum_active_signing_leases_per_signer, "block_on_any_recovery_required": self.block_on_any_recovery_required, "authoritative": False, "unresolved_reasons": self.unresolved_reasons}

    def __repr__(self) -> str:
        return f"DreamDexLiveNonceRevalidationPolicy(chain_id={self.required_chain_id!r}, signer={mask_evm_address(self.required_signer_address)!r}, max_age_ms={self.maximum_observation_age_ms!r}, authoritative=False)"


@dataclass(frozen=True, repr=False)
class DreamDexLiveNonceEvidence:
    schema_version: str
    chain_id: int | None
    chain_match: bool | None
    signer_address: str | None
    pending_nonce: int | None
    pending_tag_used: bool
    observed_at_unix_ms: int
    observation_age_ms: int | None
    observation_fresh: bool | None
    source_status: str
    authoritative: bool
    blockers: tuple[str, ...] = ()
    validation_errors: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError("unsupported_signing_lease_schema_version")
        if self.chain_id is not None:
            validate_uint(self.chain_id, field="chain_id")
        if self.pending_nonce is not None:
            validate_uint(self.pending_nonce, field="pending_nonce")
        object.__setattr__(self, "signer_address", _address(self.signer_address, "signer_address"))
        object.__setattr__(self, "observation_age_ms", None if self.observation_age_ms is None else max(0, int(self.observation_age_ms)))
        if self.source_status not in SOURCE_STATUSES:
            raise ValueError("source_status_invalid")
        object.__setattr__(self, "authoritative", False)
        object.__setattr__(self, "blockers", _tuple(self.blockers))
        object.__setattr__(self, "validation_errors", _tuple(self.validation_errors))

    def safe_dict(self) -> dict[str, Any]:
        return {"schema_version": self.schema_version, "chain_id": self.chain_id, "chain_match": self.chain_match, "signer_address_masked": mask_evm_address(self.signer_address), "pending_nonce": self.pending_nonce, "pending_tag_used": self.pending_tag_used, "observed_at_unix_ms": self.observed_at_unix_ms, "observation_age_ms": self.observation_age_ms, "observation_fresh": self.observation_fresh, "source_status": self.source_status, "authoritative": False, "blockers": self.blockers, "validation_errors": self.validation_errors}

    def __repr__(self) -> str:
        return f"DreamDexLiveNonceEvidence(chain_id={self.chain_id!r}, pending_nonce={self.pending_nonce!r}, source_status={self.source_status!r}, authoritative=False)"


@dataclass(frozen=True, repr=False)
class DreamDexSigningLease:
    schema_version: str
    lease_id: str
    intent_id: str
    reservation_id: str
    chain_id: int
    signer_address: str
    reserved_nonce: int
    observed_pending_nonce: int
    nonce_match: bool
    finalized_envelope_fingerprint: str
    signing_request_fingerprint: str
    journal_event_id: str
    lease_status: str
    acquired_at_unix_ms: int
    lease_fingerprint: str
    network_nonce_snapshot_only: bool
    externally_exclusive: bool
    signer_invocation_performed: bool
    authoritative: bool
    blockers: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError("unsupported_signing_lease_schema_version")
        validate_uint(self.chain_id, field="chain_id")
        validate_uint(self.reserved_nonce, field="reserved_nonce")
        validate_uint(self.observed_pending_nonce, field="observed_pending_nonce")
        object.__setattr__(self, "signer_address", _address(self.signer_address, "signer_address"))
        if self.lease_status not in LEASE_STATUSES:
            raise ValueError("lease_status_invalid")
        object.__setattr__(self, "network_nonce_snapshot_only", True)
        object.__setattr__(self, "externally_exclusive", False)
        object.__setattr__(self, "signer_invocation_performed", False)
        object.__setattr__(self, "authoritative", False)
        object.__setattr__(self, "blockers", _tuple(self.blockers))

    def safe_dict(self) -> dict[str, Any]:
        return {"schema_version": self.schema_version, "lease_id": mask_hex_hash(self.lease_id), "intent_id": mask_hex_hash(self.intent_id), "reservation_id": mask_hex_hash(self.reservation_id), "chain_id": self.chain_id, "signer_address_masked": mask_evm_address(self.signer_address), "reserved_nonce": self.reserved_nonce, "observed_pending_nonce": self.observed_pending_nonce, "nonce_match": self.nonce_match, "finalized_envelope_fingerprint": mask_hex_hash(self.finalized_envelope_fingerprint), "signing_request_fingerprint": mask_hex_hash(self.signing_request_fingerprint), "journal_event_id": mask_hex_hash(self.journal_event_id), "lease_status": self.lease_status, "acquired_at_unix_ms": self.acquired_at_unix_ms, "lease_fingerprint": mask_hex_hash(self.lease_fingerprint), "network_nonce_snapshot_only": True, "externally_exclusive": False, "signer_invocation_performed": False, "authoritative": False, "blockers": self.blockers}

    def __repr__(self) -> str:
        return f"DreamDexSigningLease(lease_id={mask_hex_hash(self.lease_id)!r}, intent_id={mask_hex_hash(self.intent_id)!r}, nonce={self.reserved_nonce!r}, lease_status={self.lease_status!r}, signer_invocation_performed=False)"


@dataclass(frozen=True, repr=False)
class DreamDexSigningLeaseResult:
    schema_version: str
    status: str
    intent_id: str | None
    reservation_id: str | None
    lease: DreamDexSigningLease | None
    chain_revalidated: bool
    pending_nonce_revalidated: bool
    nonce_match: bool
    journal_snapshot_clean: bool
    intent_state_match: bool
    reservation_match: bool
    finalized_envelope_match: bool
    signing_request_match: bool
    policy_approved: bool
    lease_created: bool
    existing_lease_detected: bool
    conflict_detected: bool
    ready_for_signer_invocation: bool
    signer_invocation_allowed: bool
    transaction_submission_allowed: bool
    result_fingerprint: str
    blockers: tuple[str, ...] = ()
    validation_errors: tuple[str, ...] = ()
    evidence: DreamDexLiveNonceEvidence | None = None

    def __post_init__(self) -> None:
        if self.status not in LEASE_STATUSES:
            raise ValueError("lease_status_invalid")
        object.__setattr__(self, "blockers", _tuple(self.blockers))
        object.__setattr__(self, "validation_errors", _tuple(self.validation_errors))
        object.__setattr__(self, "ready_for_signer_invocation", False)
        object.__setattr__(self, "signer_invocation_allowed", False)
        object.__setattr__(self, "transaction_submission_allowed", False)

    def safe_dict(self) -> dict[str, Any]:
        return {"schema_version": self.schema_version, "status": self.status, "intent_id": mask_hex_hash(self.intent_id), "reservation_id": mask_hex_hash(self.reservation_id), "lease": self.lease.safe_dict() if self.lease else None, "evidence": self.evidence.safe_dict() if self.evidence else None, "chain_revalidated": self.chain_revalidated, "pending_nonce_revalidated": self.pending_nonce_revalidated, "nonce_match": self.nonce_match, "journal_snapshot_clean": self.journal_snapshot_clean, "intent_state_match": self.intent_state_match, "reservation_match": self.reservation_match, "finalized_envelope_match": self.finalized_envelope_match, "signing_request_match": self.signing_request_match, "policy_approved": self.policy_approved, "lease_created": self.lease_created, "existing_lease_detected": self.existing_lease_detected, "conflict_detected": self.conflict_detected, "ready_for_signer_invocation": False, "signer_invocation_allowed": False, "transaction_submission_allowed": False, "result_fingerprint": mask_hex_hash(self.result_fingerprint), "blockers": self.blockers, "validation_errors": self.validation_errors}

    def __repr__(self) -> str:
        return f"DreamDexSigningLeaseResult(status={self.status!r}, lease_created={self.lease_created!r}, nonce_match={self.nonce_match!r}, signer_invocation_allowed=False)"


@dataclass(frozen=True, repr=False)
class DreamDexSigningLeasePreview:
    lease_status: str
    network_execution_performed: bool
    chain_match: bool | None
    pending_nonce_status: str
    pending_nonce_snapshot_only: bool
    local_nonce_reserved: bool
    nonce_match: bool
    nonce_observation_fresh: bool | None
    journal_integrity_status: str
    recovery_required: bool
    active_signing_lease_count: int
    signing_lease_acquired: bool
    signer_invocation_performed: bool
    signer_invocation_allowed: bool
    transaction_signing_capability: str
    transaction_submission_allowed: bool
    lease_fingerprint: str | None
    blockers: tuple[str, ...] = ()

    def safe_dict(self) -> dict[str, Any]:
        return {"lease_status": self.lease_status, "network_execution_performed": self.network_execution_performed, "chain_match": self.chain_match, "pending_nonce_status": self.pending_nonce_status, "pending_nonce_snapshot_only": True, "local_nonce_reserved": self.local_nonce_reserved, "nonce_match": self.nonce_match, "nonce_observation_fresh": self.nonce_observation_fresh, "journal_integrity_status": self.journal_integrity_status, "recovery_required": self.recovery_required, "active_signing_lease_count": self.active_signing_lease_count, "signing_lease_acquired": self.signing_lease_acquired, "signer_invocation_performed": False, "signer_invocation_allowed": False, "transaction_signing_capability": self.transaction_signing_capability, "transaction_submission_allowed": False, "lease_fingerprint": mask_hex_hash(self.lease_fingerprint), "blockers": self.blockers}

    def __repr__(self) -> str:
        return f"DreamDexSigningLeasePreview(status={self.lease_status!r}, lease_acquired={self.signing_lease_acquired!r}, signer_invocation_allowed=False)"


def _result(*, status: str, intent_id: str | None = None, reservation_id: str | None = None, lease: DreamDexSigningLease | None = None, evidence: DreamDexLiveNonceEvidence | None = None, chain_revalidated: bool = False, pending_nonce_revalidated: bool = False, nonce_match: bool = False, journal_snapshot_clean: bool = False, intent_state_match: bool = False, reservation_match: bool = False, finalized_envelope_match: bool = False, signing_request_match: bool = False, policy_approved: bool = False, lease_created: bool = False, existing_lease_detected: bool = False, conflict_detected: bool = False, blockers: tuple[str, ...] = (), validation_errors: tuple[str, ...] = ()) -> DreamDexSigningLeaseResult:
    fp = deterministic_fingerprint({"status": status, "intent_id": intent_id, "reservation_id": reservation_id, "lease_id": lease.lease_id if lease else None, "chain_revalidated": chain_revalidated, "pending_nonce_revalidated": pending_nonce_revalidated, "nonce_match": nonce_match, "journal_snapshot_clean": journal_snapshot_clean, "intent_state_match": intent_state_match, "reservation_match": reservation_match, "finalized_envelope_match": finalized_envelope_match, "signing_request_match": signing_request_match, "policy_approved": policy_approved, "lease_created": lease_created, "existing_lease_detected": existing_lease_detected, "conflict_detected": conflict_detected, "blockers": blockers, "validation_errors": validation_errors}, domain="dreamdex_signing_lease_result")
    return DreamDexSigningLeaseResult(SCHEMA_VERSION, status, intent_id, reservation_id, lease, chain_revalidated, pending_nonce_revalidated, nonce_match, journal_snapshot_clean, intent_state_match, reservation_match, finalized_envelope_match, signing_request_match, policy_approved, lease_created, existing_lease_detected, conflict_detected, False, False, False, fp, blockers, validation_errors, evidence)


def _preflight_checks(*, journal: DreamDexExecutionJournal, intent: DreamDexExecutionIntent, reservation: DreamDexNonceReservation, envelope: DreamDexUnsignedTransactionEnvelope, signing_request: DreamDexTransactionSigningRequest, signing_policy: DreamDexTransactionSigningPolicy, policy: DreamDexLiveNonceRevalidationPolicy) -> tuple[tuple[str, ...], tuple[str, ...], Any]:
    blockers: list[str] = []
    errors: list[str] = []
    snapshot = journal.build_execution_journal_snapshot()
    journal_integrity_clean = snapshot.schema_status == "compatible" and snapshot.integrity_status == "passed"
    if policy.require_clean_journal and not journal_integrity_clean:
        blockers.append("execution_journal_integrity_failed")
    if policy.block_on_any_recovery_required and snapshot.recovery_required:
        blockers.append("execution_journal_recovery_required")
    if policy.unresolved_reasons or policy.maximum_observation_age_ms is None or policy.required_chain_id is None or policy.required_signer_address is None:
        blockers.append("signing_lease_policy_unresolved")
    stored_intent = journal.get_execution_intent(intent.intent_id)
    state_match = stored_intent is not None and stored_intent == intent and intent.state == JournalState.SIGNING_REVIEW_READY.value
    if not state_match:
        blockers.append("signing_lease_intent_invalid")
    stored_reservation = journal.get_nonce_reservation(reservation.reservation_id)
    reservation_match = stored_reservation is not None and stored_reservation == reservation and reservation.intent_id == intent.intent_id and reservation.reservation_status == "reserved"
    if reservation.chain_id != intent.chain_id or reservation.signer_address.lower() != intent.signer_address.lower():
        reservation_match = False
    if policy.require_active_reservation and not reservation_match:
        blockers.append("signing_lease_reservation_invalid")
    if envelope.chain_id != intent.chain_id or envelope.chain_id != policy.required_chain_id or signing_request.chain_id != envelope.chain_id:
        blockers.append("rpc_chain_mismatch")
    if envelope.from_address != intent.signer_address or signing_request.signer_address != intent.signer_address or intent.signer_address != policy.required_signer_address:
        blockers.append("direct_signer_binding_non_authoritative")
    if envelope.nonce is None or reservation.nonce != envelope.nonce:
        blockers.append("signing_lease_envelope_mismatch")
    finalized_match = envelope.envelope_fingerprint == intent.finalized_envelope_fingerprint
    if policy.require_finalized_envelope_match and not finalized_match:
        blockers.append("signing_lease_envelope_mismatch")
    request_match = signing_request.request_fingerprint == intent.request_fingerprint and signing_request.envelope_fingerprint == envelope.envelope_fingerprint and signing_request.signing_request_fingerprint == intent.signing_request_fingerprint
    if policy.require_signing_request_match and not request_match:
        blockers.append("signing_lease_request_mismatch")
    if not intent.preflight_fingerprint:
        blockers.append("finalized_envelope_unavailable")
    structural = validate_unsigned_transaction_envelope(envelope)
    errors.extend(structural.errors)
    if structural.errors:
        blockers.append("transaction_envelope_unavailable")
    if not isinstance(signing_request, DreamDexTransactionSigningRequest) or not isinstance(signing_policy, DreamDexTransactionSigningPolicy):
        blockers.append("transaction_signing_request_unavailable")
        errors.append("signing_input_type_invalid")
        validation = None
    else:
        validation = validate_transaction_signing_policy(envelope, signing_policy, signer_address=intent.signer_address)
        request_validation = validate_transaction_signing_request(signing_request, envelope=envelope, policy=signing_policy)
        if policy.require_signing_policy_approved and (not signing_request.policy_approved or not validation.approved or not request_validation.approved):
            blockers.append("transaction_signing_policy_rejected")
        if not signing_request.ready_for_signer_invocation:
            blockers.append("signing_lease_request_not_ready")
    return tuple(dict.fromkeys(blockers)), tuple(dict.fromkeys(errors)), (snapshot, validation)


def acquire_signing_lease(*, journal: DreamDexExecutionJournal, intent: DreamDexExecutionIntent, reservation: DreamDexNonceReservation, finalized_envelope: DreamDexUnsignedTransactionEnvelope, signing_request: DreamDexTransactionSigningRequest, signing_policy: DreamDexTransactionSigningPolicy, policy: DreamDexLiveNonceRevalidationPolicy, rpc: DreamDexReadOnlyRpc) -> DreamDexSigningLeaseResult:
    """Revalidate exactly two RPC facts, then acquire one local lease."""
    if isinstance(finalized_envelope, dict) or isinstance(signing_request, dict) or isinstance(intent, dict) or isinstance(reservation, dict):
        return _result(status="blocked", blockers=("signing_lease_input_type_invalid",), validation_errors=("typed_inputs_required",))
    rpc_typed = not isinstance(rpc, dict) and callable(getattr(rpc, "get_chain_id", None)) and callable(getattr(rpc, "get_pending_nonce", None))
    if not isinstance(journal, DreamDexExecutionJournal) or not isinstance(intent, DreamDexExecutionIntent) or not isinstance(reservation, DreamDexNonceReservation) or not isinstance(finalized_envelope, DreamDexUnsignedTransactionEnvelope) or not isinstance(signing_request, DreamDexTransactionSigningRequest) or not isinstance(signing_policy, DreamDexTransactionSigningPolicy) or not isinstance(policy, DreamDexLiveNonceRevalidationPolicy) or not rpc_typed:
        return _result(status="blocked", blockers=("signing_lease_input_type_invalid",), validation_errors=("typed_inputs_required",))
    current_intent = journal.get_execution_intent(intent.intent_id)
    if current_intent is not None and current_intent.state == JournalState.SIGNING_LEASE_ACQUIRED.value:
        return _result(status="conflict", intent_id=intent.intent_id, reservation_id=reservation.reservation_id, existing_lease_detected=True, conflict_detected=True, blockers=("signing_lease_conflict",))
    blockers, errors, context = _preflight_checks(journal=journal, intent=intent, reservation=reservation, envelope=finalized_envelope, signing_request=signing_request, signing_policy=signing_policy, policy=policy)
    snapshot, validation = context
    if blockers:
        return _result(status="blocked", intent_id=intent.intent_id, reservation_id=reservation.reservation_id, journal_snapshot_clean=not bool([x for x in blockers if x.startswith("execution_journal")]), intent_state_match="signing_lease_intent_invalid" not in blockers, reservation_match="signing_lease_reservation_invalid" not in blockers, finalized_envelope_match="signing_lease_envelope_mismatch" not in blockers, signing_request_match="signing_lease_request_mismatch" not in blockers, policy_approved=bool(validation and validation.approved and signing_request.policy_approved), blockers=blockers, validation_errors=errors)
    observed_at = _now_ms()
    try:
        chain_id = rpc.get_chain_id()
    except Exception:
        evidence = DreamDexLiveNonceEvidence(SCHEMA_VERSION, None, None, intent.signer_address, None, True, observed_at, 0, False, "unavailable", False, ("live_nonce_revalidation_unavailable",), ("chain_id_unavailable",))
        return _result(status="unavailable", intent_id=intent.intent_id, reservation_id=reservation.reservation_id, evidence=evidence, journal_snapshot_clean=True, intent_state_match=True, reservation_match=True, finalized_envelope_match=True, signing_request_match=True, policy_approved=True, blockers=evidence.blockers, validation_errors=evidence.validation_errors)
    chain_match = chain_id == policy.required_chain_id == intent.chain_id == finalized_envelope.chain_id
    if not chain_match:
        evidence = DreamDexLiveNonceEvidence(SCHEMA_VERSION, chain_id, False, intent.signer_address, None, True, observed_at, max(0, _now_ms() - observed_at), None, "invalid", False, ("live_nonce_chain_mismatch",), ("chain_id_mismatch",))
        return _result(status="blocked", intent_id=intent.intent_id, reservation_id=reservation.reservation_id, evidence=evidence, chain_revalidated=True, journal_snapshot_clean=True, intent_state_match=True, reservation_match=True, finalized_envelope_match=True, signing_request_match=True, policy_approved=True, blockers=evidence.blockers, validation_errors=evidence.validation_errors)
    try:
        pending_nonce = rpc.get_pending_nonce(intent.signer_address)
    except Exception:
        evidence = DreamDexLiveNonceEvidence(SCHEMA_VERSION, chain_id, True, intent.signer_address, None, True, observed_at, max(0, _now_ms() - observed_at), False, "unavailable", False, ("live_nonce_revalidation_unavailable",), ("pending_nonce_unavailable",))
        return _result(status="unavailable", intent_id=intent.intent_id, reservation_id=reservation.reservation_id, evidence=evidence, chain_revalidated=True, journal_snapshot_clean=True, intent_state_match=True, reservation_match=True, finalized_envelope_match=True, signing_request_match=True, policy_approved=True, blockers=evidence.blockers, validation_errors=evidence.validation_errors)
    age = max(0, _now_ms() - observed_at)
    fresh = age <= int(policy.maximum_observation_age_ms)
    nonce_match = pending_nonce == reservation.nonce == finalized_envelope.nonce
    evidence_blockers = ["external_nonce_exclusivity_unavailable"]
    if not fresh:
        evidence_blockers.append("live_nonce_observation_stale")
    if not nonce_match:
        evidence_blockers.append("live_nonce_mismatch")
    if not fresh or not nonce_match:
        evidence = DreamDexLiveNonceEvidence(SCHEMA_VERSION, chain_id, True, intent.signer_address, pending_nonce, True, observed_at, age, fresh, "source_confirmed", False, tuple(evidence_blockers), tuple(dict.fromkeys(("observation_stale" if not fresh else "", "nonce_mismatch" if not nonce_match else ""))))
        return _result(status="blocked", intent_id=intent.intent_id, reservation_id=reservation.reservation_id, evidence=evidence, chain_revalidated=True, pending_nonce_revalidated=True, nonce_match=nonce_match, journal_snapshot_clean=True, intent_state_match=True, reservation_match=True, finalized_envelope_match=True, signing_request_match=True, policy_approved=True, blockers=tuple(evidence_blockers), validation_errors=tuple(x for x in evidence.validation_errors if x))
    evidence = DreamDexLiveNonceEvidence(SCHEMA_VERSION, chain_id, True, intent.signer_address, pending_nonce, True, observed_at, age, True, "source_confirmed", False, ("external_nonce_exclusivity_unavailable",), ())
    event = journal.acquire_signing_lease(intent_id=intent.intent_id, reservation_id=reservation.reservation_id, signer_address=intent.signer_address, chain_id=chain_id, finalized_envelope_fingerprint=finalized_envelope.envelope_fingerprint, signing_request_fingerprint=signing_request.signing_request_fingerprint, maximum_active_leases_per_signer=policy.maximum_active_signing_leases_per_signer)
    if event is None:
        return _result(status="conflict", intent_id=intent.intent_id, reservation_id=reservation.reservation_id, chain_revalidated=True, pending_nonce_revalidated=True, nonce_match=True, journal_snapshot_clean=True, intent_state_match=True, reservation_match=True, finalized_envelope_match=True, signing_request_match=True, policy_approved=True, existing_lease_detected=True, conflict_detected=True, blockers=("signing_lease_conflict", "external_nonce_exclusivity_unavailable"))
    lease_fp = deterministic_fingerprint({"lease_id": event.event_id, "intent_id": intent.intent_id, "reservation_id": reservation.reservation_id, "chain_id": chain_id, "signer_address": intent.signer_address, "reserved_nonce": reservation.nonce, "observed_pending_nonce": pending_nonce, "finalized_envelope_fingerprint": finalized_envelope.envelope_fingerprint, "signing_request_fingerprint": signing_request.signing_request_fingerprint}, domain="dreamdex_signing_lease")
    lease = DreamDexSigningLease(SCHEMA_VERSION, event.event_id, intent.intent_id, reservation.reservation_id, chain_id, intent.signer_address, reservation.nonce, pending_nonce, True, finalized_envelope.envelope_fingerprint, signing_request.signing_request_fingerprint, event.event_id, "acquired", event.created_at_unix_ms, lease_fp, True, False, False, False, ("external_nonce_exclusivity_unavailable",))
    return _result(status="acquired", intent_id=intent.intent_id, reservation_id=reservation.reservation_id, lease=lease, evidence=evidence, chain_revalidated=True, pending_nonce_revalidated=True, nonce_match=True, journal_snapshot_clean=True, intent_state_match=True, reservation_match=True, finalized_envelope_match=True, signing_request_match=True, policy_approved=True, lease_created=True, blockers=("external_nonce_exclusivity_unavailable",))


def revalidate_and_acquire_signing_lease(**kwargs: Any) -> DreamDexSigningLeaseResult:
    return acquire_signing_lease(**kwargs)


def build_signing_lease_preview(result: DreamDexSigningLeaseResult | None = None, *, evidence: DreamDexLiveNonceEvidence | None = None, journal_snapshot: Any = None) -> DreamDexSigningLeasePreview:
    return DreamDexSigningLeasePreview(lease_status=result.status if result else "unavailable", network_execution_performed=bool(result and (result.chain_revalidated or result.pending_nonce_revalidated)), chain_match=True if result and result.chain_revalidated and result.status == "acquired" else None, pending_nonce_status="available" if result and result.pending_nonce_revalidated else "unavailable", pending_nonce_snapshot_only=True, local_nonce_reserved=bool(result and result.reservation_id), nonce_match=bool(result and result.nonce_match), nonce_observation_fresh=evidence.observation_fresh if evidence else None, journal_integrity_status="passed" if result and result.journal_snapshot_clean else "unavailable", recovery_required=False, active_signing_lease_count=1 if result and result.lease_created else 0, signing_lease_acquired=bool(result and result.lease_created), signer_invocation_performed=False, signer_invocation_allowed=False, transaction_signing_capability="unavailable", transaction_submission_allowed=False, lease_fingerprint=result.lease.lease_fingerprint if result and result.lease else None, blockers=result.blockers if result else ())


def serialize_signing_lease_diagnostics(result: DreamDexSigningLeaseResult | None = None, *, preview: DreamDexSigningLeasePreview | None = None) -> dict[str, Any]:
    item = preview or build_signing_lease_preview(result, evidence=result.evidence if result else None)
    return item.safe_dict()


__all__ = ["DreamDexLiveNonceRevalidationPolicy", "DreamDexLiveNonceEvidence", "DreamDexSigningLease", "DreamDexSigningLeaseResult", "DreamDexSigningLeasePreview", "acquire_signing_lease", "revalidate_and_acquire_signing_lease", "build_signing_lease_preview", "serialize_signing_lease_diagnostics"]
