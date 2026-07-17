"""Fail-closed, idempotent raw transaction submission boundary.

The module is intentionally narrow: a typed submitter may dispatch exactly one
``eth_sendRawTransaction`` call and a typed recovery reader may perform one
``eth_getTransactionByHash`` lookup.  Signed bytes are accepted only as an
ephemeral in-memory value and are never placed in records, diagnostics,
exceptions, queues, or files.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from typing import Any, Mapping, Protocol

from bot.execution.dreamdex_execution_journal import DreamDexExecutionJournal, JournalState
from bot.execution.dreamdex_execution_primitives import (
    deterministic_fingerprint,
    ensure_no_raw_sensitive_fields,
    mask_evm_address,
    mask_hex_hash,
    validate_evm_address,
    validate_tx_hash,
)
from bot.execution.dreamdex_readonly_rpc import (
    DreamDexReadOnlyRpcTransport,
    DreamDexRpcError,
    validate_rpc_response,
)
from bot.execution.dreamdex_signed_transaction import (
    DreamDexEphemeralSignedTransaction,
    DreamDexTransactionSigningMaterial,
    DreamDexVerifiedSignedTransactionArtifact,
    decode_signed_transaction,
    verify_signed_transaction,
)

SCHEMA_VERSION = "1"
SUBMISSION_STATUSES = frozenset({"submission_started", "submitted", "rejected", "submission_unknown", "submission_hash_conflict", "submitted_recovered", "recovery_required"})
RESPONSE_STATUSES = frozenset({"accepted", "already_known", "rpc_error", "malformed_response", "transport_error", "rejected"})
LOOKUP_STATUSES = frozenset({"not_checked", "not_found", "found", "already_checked", "error", "mismatch"})


def _tuple(values: Any) -> tuple[str, ...]:
    return tuple(dict.fromkeys(str(value) for value in (values or ()) if str(value)))


def _mask(value: Any) -> str:
    return mask_hex_hash(value)


def _canonical_hash(value: Any, field: str) -> str:
    parsed = validate_tx_hash(value, field=field)
    assert parsed is not None
    return parsed


def _fingerprint(payload: Any, domain: str) -> str:
    return deterministic_fingerprint(payload, domain=domain, schema_version=SCHEMA_VERSION)


@dataclass(frozen=True, repr=False)
class DreamDexTransactionSubmissionPolicy:
    schema_version: str = SCHEMA_VERSION
    required_chain_id: int | None = None
    required_signer_address: str | None = None
    required_target_address: str | None = None
    require_active_signing_lease: bool = True
    require_verified_signed_artifact: bool = True
    require_local_transaction_hash: bool = True
    require_exact_rpc_hash_match: bool = True
    maximum_signed_payload_bytes: int = 1_000_000
    maximum_submission_attempts: int = 1
    allow_already_known_recovery: bool = True
    automatic_retry_allowed: bool = False
    replacement_allowed: bool = False
    authoritative: bool = False
    unresolved_reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError("submission_policy_schema_version_invalid")
        if self.required_chain_id is not None and (isinstance(self.required_chain_id, bool) or not isinstance(self.required_chain_id, int) or self.required_chain_id < 0):
            raise ValueError("required_chain_id_invalid")
        if self.required_signer_address is not None:
            object.__setattr__(self, "required_signer_address", validate_evm_address(self.required_signer_address, field="required_signer_address"))
        if self.required_target_address is not None:
            object.__setattr__(self, "required_target_address", validate_evm_address(self.required_target_address, field="required_target_address"))
        if isinstance(self.maximum_signed_payload_bytes, bool) or not isinstance(self.maximum_signed_payload_bytes, int) or self.maximum_signed_payload_bytes <= 0:
            raise ValueError("maximum_signed_payload_bytes_invalid")
        if self.maximum_submission_attempts != 1:
            raise ValueError("maximum_submission_attempts_must_be_one")
        if self.automatic_retry_allowed:
            raise ValueError("automatic_submission_retry_disabled")
        if self.replacement_allowed:
            raise ValueError("transaction_replacement_disabled")
        object.__setattr__(self, "authoritative", False)
        object.__setattr__(self, "unresolved_reasons", _tuple(self.unresolved_reasons))

    def safe_dict(self) -> dict[str, Any]:
        return {"schema_version": self.schema_version, "required_chain_id": self.required_chain_id, "required_signer_address_masked": mask_evm_address(self.required_signer_address), "required_target_address_masked": mask_evm_address(self.required_target_address), "require_active_signing_lease": self.require_active_signing_lease, "require_verified_signed_artifact": self.require_verified_signed_artifact, "require_local_transaction_hash": self.require_local_transaction_hash, "require_exact_rpc_hash_match": self.require_exact_rpc_hash_match, "maximum_signed_payload_bytes": self.maximum_signed_payload_bytes, "maximum_submission_attempts": 1, "allow_already_known_recovery": self.allow_already_known_recovery, "automatic_retry_allowed": False, "replacement_allowed": False, "authoritative": False, "unresolved_reasons": self.unresolved_reasons}

    def __repr__(self) -> str:
        return f"DreamDexTransactionSubmissionPolicy(maximum_submission_attempts=1, automatic_retry_allowed=False, replacement_allowed=False, authoritative=False)"


@dataclass(frozen=True, repr=False)
class DreamDexRawTransactionSubmissionResponse:
    response_status: str
    locally_calculated_transaction_hash: str | None
    rpc_returned_transaction_hash: str | None
    exact_hash_match: bool | None
    request_dispatch_status: str
    response_received: bool
    transport_outcome: str
    source_status: str
    authoritative: bool
    blockers: tuple[str, ...] = ()
    validation_errors: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.response_status not in RESPONSE_STATUSES:
            raise ValueError("submission_response_status_invalid")
        if self.locally_calculated_transaction_hash is not None:
            object.__setattr__(self, "locally_calculated_transaction_hash", _canonical_hash(self.locally_calculated_transaction_hash, "locally_calculated_transaction_hash"))
        if self.rpc_returned_transaction_hash is not None:
            object.__setattr__(self, "rpc_returned_transaction_hash", _canonical_hash(self.rpc_returned_transaction_hash, "rpc_returned_transaction_hash"))
        object.__setattr__(self, "authoritative", False)
        object.__setattr__(self, "blockers", _tuple(self.blockers))
        object.__setattr__(self, "validation_errors", _tuple(self.validation_errors))

    def safe_dict(self) -> dict[str, Any]:
        return {"response_status": self.response_status, "locally_calculated_transaction_hash": _mask(self.locally_calculated_transaction_hash), "rpc_returned_transaction_hash": _mask(self.rpc_returned_transaction_hash), "exact_hash_match": self.exact_hash_match, "request_dispatch_status": self.request_dispatch_status, "response_received": self.response_received, "transport_outcome": self.transport_outcome, "source_status": self.source_status, "authoritative": False, "blockers": self.blockers, "validation_errors": self.validation_errors}

    def __repr__(self) -> str:
        return f"DreamDexRawTransactionSubmissionResponse(status={self.response_status!r}, response_received={self.response_received!r}, exact_hash_match={self.exact_hash_match!r})"


@dataclass(frozen=True, repr=False)
class DreamDexTransactionSubmissionRecord:
    schema_version: str
    submission_id: str
    intent_id: str
    reservation_id: str
    lease_id: str
    signed_transaction_hash: str
    signed_payload_length: int
    verified_artifact_fingerprint: str
    submission_status: str
    send_attempt_count: int
    send_attempt_started: bool
    response_received: bool
    rpc_hash_match: bool | None
    record_fingerprint: str
    authoritative: bool
    blockers: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION or self.submission_status not in SUBMISSION_STATUSES:
            raise ValueError("submission_record_schema_or_status_invalid")
        object.__setattr__(self, "signed_transaction_hash", _canonical_hash(self.signed_transaction_hash, "signed_transaction_hash"))
        if isinstance(self.signed_payload_length, bool) or not isinstance(self.signed_payload_length, int) or self.signed_payload_length <= 0:
            raise ValueError("signed_payload_length_invalid")
        if self.send_attempt_count not in {0, 1}:
            raise ValueError("send_attempt_count_invalid")
        object.__setattr__(self, "authoritative", False)
        object.__setattr__(self, "blockers", _tuple(self.blockers))

    def safe_dict(self) -> dict[str, Any]:
        return {"schema_version": self.schema_version, "submission_id": _mask(self.submission_id), "intent_id": _mask(self.intent_id), "reservation_id": _mask(self.reservation_id), "lease_id": _mask(self.lease_id), "signed_transaction_hash": _mask(self.signed_transaction_hash), "signed_payload_length": self.signed_payload_length, "verified_artifact_fingerprint": _mask(self.verified_artifact_fingerprint), "submission_status": self.submission_status, "send_attempt_count": self.send_attempt_count, "send_attempt_started": self.send_attempt_started, "response_received": self.response_received, "rpc_hash_match": self.rpc_hash_match, "record_fingerprint": _mask(self.record_fingerprint), "authoritative": False, "blockers": self.blockers}

    def __repr__(self) -> str:
        return f"DreamDexTransactionSubmissionRecord(intent_id={_mask(self.intent_id)!r}, status={self.submission_status!r}, attempts={self.send_attempt_count!r})"


@dataclass(frozen=True, repr=False)
class DreamDexTransactionSubmissionResult:
    schema_version: str
    status: str
    submission_record: DreamDexTransactionSubmissionRecord | None
    journal_state_before: str
    journal_state_after: str
    submission_execution_performed: bool
    send_attempt_count: int
    response_received: bool
    local_transaction_hash: str | None
    rpc_returned_hash: str | None
    hash_match: bool | None
    submitted: bool
    submission_unknown: bool
    automatic_retry_allowed: bool
    recovery_required: bool
    ready_for_receipt_lookup: bool
    ready_for_resubmission: bool
    result_fingerprint: str
    authoritative: bool
    blockers: tuple[str, ...] = ()
    validation_errors: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError("submission_result_schema_version_invalid")
        if self.local_transaction_hash is not None:
            object.__setattr__(self, "local_transaction_hash", _canonical_hash(self.local_transaction_hash, "local_transaction_hash"))
        if self.rpc_returned_hash is not None:
            object.__setattr__(self, "rpc_returned_hash", _canonical_hash(self.rpc_returned_hash, "rpc_returned_hash"))
        object.__setattr__(self, "automatic_retry_allowed", False)
        object.__setattr__(self, "ready_for_resubmission", False)
        object.__setattr__(self, "authoritative", False)
        object.__setattr__(self, "blockers", _tuple(self.blockers))
        object.__setattr__(self, "validation_errors", _tuple(self.validation_errors))

    def safe_dict(self) -> dict[str, Any]:
        return {"schema_version": self.schema_version, "status": self.status, "submission_record": self.submission_record.safe_dict() if self.submission_record else None, "journal_state_before": self.journal_state_before, "journal_state_after": self.journal_state_after, "submission_execution_performed": self.submission_execution_performed, "send_attempt_count": self.send_attempt_count, "response_received": self.response_received, "local_transaction_hash": _mask(self.local_transaction_hash), "rpc_returned_hash": _mask(self.rpc_returned_hash), "hash_match": self.hash_match, "submitted": self.submitted, "submission_unknown": self.submission_unknown, "automatic_retry_allowed": False, "recovery_required": self.recovery_required, "ready_for_receipt_lookup": self.ready_for_receipt_lookup, "ready_for_resubmission": False, "result_fingerprint": _mask(self.result_fingerprint), "authoritative": False, "blockers": self.blockers, "validation_errors": self.validation_errors}

    def __repr__(self) -> str:
        return f"DreamDexTransactionSubmissionResult(status={self.status!r}, submitted={self.submitted!r}, submission_unknown={self.submission_unknown!r}, ready_for_resubmission=False)"


@dataclass(frozen=True, repr=False)
class DreamDexSubmissionRecoveryEvidence:
    schema_version: str
    transaction_hash: str
    lookup_performed: bool
    transaction_found: bool | None
    chain_id: int | None
    sender_address: str | None
    nonce: int | None
    target_address: str | None
    value_wei: int | None
    gas_limit: int | None
    transaction_type: str | None
    selector: str | None
    exact_expected_fields_match: bool | None
    lookup_status: str
    authoritative: bool
    blockers: tuple[str, ...] = ()
    validation_errors: tuple[str, ...] = ()
    recovery_fingerprint: str = ""

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION or self.lookup_status not in LOOKUP_STATUSES:
            raise ValueError("recovery_evidence_schema_or_status_invalid")
        object.__setattr__(self, "transaction_hash", _canonical_hash(self.transaction_hash, "transaction_hash"))
        if self.sender_address is not None:
            object.__setattr__(self, "sender_address", validate_evm_address(self.sender_address, field="sender_address"))
        if self.target_address is not None:
            object.__setattr__(self, "target_address", validate_evm_address(self.target_address, field="target_address"))
        object.__setattr__(self, "authoritative", False)
        object.__setattr__(self, "blockers", _tuple(self.blockers))
        object.__setattr__(self, "validation_errors", _tuple(self.validation_errors))
        if not self.recovery_fingerprint:
            object.__setattr__(self, "recovery_fingerprint", _fingerprint({"transaction_hash": self.transaction_hash, "lookup_status": self.lookup_status, "transaction_found": self.transaction_found, "exact_expected_fields_match": self.exact_expected_fields_match, "blockers": self.blockers}, "dreamdex/submission-recovery"))

    def safe_dict(self) -> dict[str, Any]:
        return {"schema_version": self.schema_version, "transaction_hash": _mask(self.transaction_hash), "lookup_performed": self.lookup_performed, "transaction_found": self.transaction_found, "chain_id": self.chain_id, "sender_address_masked": mask_evm_address(self.sender_address), "nonce": self.nonce, "target_address_masked": mask_evm_address(self.target_address), "value_wei": self.value_wei, "gas_limit": self.gas_limit, "transaction_type": self.transaction_type, "selector": self.selector, "exact_expected_fields_match": self.exact_expected_fields_match, "lookup_status": self.lookup_status, "recovery_fingerprint": _mask(self.recovery_fingerprint), "authoritative": False, "blockers": self.blockers, "validation_errors": self.validation_errors}

    def __repr__(self) -> str:
        return f"DreamDexSubmissionRecoveryEvidence(hash={_mask(self.transaction_hash)!r}, status={self.lookup_status!r}, found={self.transaction_found!r})"


@dataclass(frozen=True, repr=False)
class DreamDexTransactionSubmissionPreview:
    submission_boundary_status: str
    production_submitter_status: str
    submission_execution_performed: bool
    raw_payload_available_in_memory: bool
    raw_payload_persisted: bool
    raw_payload_reference_released: bool
    local_transaction_hash_available: bool
    submission_record_persisted: bool
    send_attempt_started: bool
    send_attempt_count: int
    rpc_response_received: bool
    rpc_hash_match: bool | None
    journal_state: str
    submitted: bool
    submission_unknown: bool
    automatic_retry_allowed: bool
    replacement_allowed: bool
    recovery_lookup_available: bool
    recovery_lookup_performed: bool
    transaction_found_by_hash: bool | None
    ready_for_receipt_lookup: bool
    ready_for_resubmission: bool
    raw_signed_transaction_output_allowed: bool
    blockers: tuple[str, ...] = ()

    def safe_dict(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}

    def __repr__(self) -> str:
        return f"DreamDexTransactionSubmissionPreview(status={self.submission_boundary_status!r}, submitted={self.submitted!r}, ready_for_resubmission=False)"


class DreamDexRawTransactionSubmitter(Protocol):
    def submit_raw_transaction(self, ephemeral_signed_transaction: DreamDexEphemeralSignedTransaction) -> DreamDexRawTransactionSubmissionResponse: ...


class DreamDexTransactionRecoveryReader(Protocol):
    def get_transaction_by_hash(self, transaction_hash: str) -> Mapping[str, Any] | None: ...


class DreamDexRawTransactionHttpSubmitter:
    """Strict HTTP adapter exposing only the raw-send method."""
    ALLOWED_RPC_METHOD = "eth_sendRawTransaction"
    ALLOWED_RPC_METHODS = frozenset({ALLOWED_RPC_METHOD})
    RPC_METHOD_ALLOWLIST = ALLOWED_RPC_METHODS

    def __init__(self, rpc_url: str, *, timeout_seconds: float = 10.0, max_response_body_bytes: int = 1_000_000, maximum_signed_payload_bytes: int = 1_000_000, http_client: Any = None) -> None:
        self._transport = DreamDexReadOnlyRpcTransport(rpc_url, timeout_seconds=timeout_seconds, max_response_body_bytes=max_response_body_bytes, http_client=http_client)
        self._request_id = 0
        if isinstance(maximum_signed_payload_bytes, bool) or not isinstance(maximum_signed_payload_bytes, int) or maximum_signed_payload_bytes <= 0:
            raise ValueError("maximum_signed_payload_bytes_invalid")
        self._max_payload_bytes = maximum_signed_payload_bytes

    def describe_capabilities(self) -> Mapping[str, str]:
        return {"eth_sendRawTransaction": "available_opt_in_only", "mutation_methods": "unavailable", "arbitrary_rpc": "unavailable", "automatic_retry": "unavailable"}

    def submit_raw_transaction(self, ephemeral_signed_transaction: DreamDexEphemeralSignedTransaction) -> DreamDexRawTransactionSubmissionResponse:
        if not isinstance(ephemeral_signed_transaction, DreamDexEphemeralSignedTransaction):
            raise TypeError("typed_ephemeral_signed_transaction_required")
        raw = bytes(ephemeral_signed_transaction.raw_signed_transaction)
        if not raw or len(raw) > self._max_payload_bytes:
            raise ValueError("signed_payload_size_invalid")
        self._request_id += 1
        request_id = self._request_id
        try:
            payload = {"jsonrpc": "2.0", "id": request_id, "method": self.ALLOWED_RPC_METHOD, "params": ["0x" + raw.hex()]}
            response = self._transport._post(payload)  # type: ignore[attr-defined]
            result = validate_rpc_response(response, request_id)
        except DreamDexRpcError:
            raise
        except Exception:
            raise DreamDexRpcError("transport_unavailable") from None
        if not isinstance(result, str):
            return DreamDexRawTransactionSubmissionResponse("malformed_response", None, None, None, "dispatched", True, "malformed_response", "observed", False, ("submission_response_unavailable",), ("malformed_transaction_hash",))
        try:
            returned = _canonical_hash(result, "rpc_returned_transaction_hash")
        except ValueError:
            return DreamDexRawTransactionSubmissionResponse("malformed_response", None, None, None, "dispatched", True, "malformed_response", "observed", False, ("submission_response_unavailable",), ("malformed_transaction_hash",))
        return DreamDexRawTransactionSubmissionResponse("accepted", None, returned, None, "dispatched", True, "response_received", "observed", False)


class DreamDexTransactionByHashHttpReader:
    """One-shot read-only transaction lookup adapter."""
    ALLOWED_RPC_METHOD = "eth_getTransactionByHash"
    ALLOWED_RPC_METHODS = frozenset({ALLOWED_RPC_METHOD})
    RPC_METHOD_ALLOWLIST = ALLOWED_RPC_METHODS

    def __init__(self, rpc_url: str, *, timeout_seconds: float = 10.0, max_response_body_bytes: int = 1_000_000, http_client: Any = None) -> None:
        self._transport = DreamDexReadOnlyRpcTransport(rpc_url, timeout_seconds=timeout_seconds, max_response_body_bytes=max_response_body_bytes, http_client=http_client)
        self._request_id = 0

    def describe_capabilities(self) -> Mapping[str, str]:
        return {"eth_getTransactionByHash": "available_opt_in_only", "polling": "unavailable", "receipt_lookup": "unavailable", "arbitrary_rpc": "unavailable"}

    def get_transaction_by_hash(self, transaction_hash: str) -> Mapping[str, Any] | None:
        normalized = _canonical_hash(transaction_hash, "transaction_hash")
        self._request_id += 1
        try:
            payload = {"jsonrpc": "2.0", "id": self._request_id, "method": self.ALLOWED_RPC_METHOD, "params": [normalized]}
            response = self._transport._post(payload)  # type: ignore[attr-defined]
            result = validate_rpc_response(response, self._request_id)
        except DreamDexRpcError:
            raise
        except Exception:
            raise DreamDexRpcError("transport_unavailable") from None
        if result is None:
            return None
        if not isinstance(result, Mapping):
            raise DreamDexRpcError("malformed_transaction_lookup")
        return dict(result)


def _record_from_row(row: Mapping[str, Any] | None) -> DreamDexTransactionSubmissionRecord | None:
    if row is None:
        return None
    return DreamDexTransactionSubmissionRecord(SCHEMA_VERSION, str(row["submission_id"]), str(row["intent_id"]), str(row["reservation_id"]), str(row["lease_id"]), str(row["signed_transaction_hash"]), int(row["signed_payload_length"]), str(row["verified_artifact_fingerprint"]), str(row["submission_status"]), int(row["send_attempt_count"]), int(row["send_attempt_count"]) == 1, row.get("send_completed_at_unix_ms") is not None, True if row.get("local_hash_match_status") == "confirmed" else False if row.get("local_hash_match_status") == "mismatch" else None, str(row["record_fingerprint"]), False, ("signed_payload_not_durably_available",) if row["submission_status"] != "submitted" else ())


def _result(*, status: str, record: DreamDexTransactionSubmissionRecord | None, before: str, after: str, performed: bool, response_received: bool, local_hash: str | None, rpc_hash: str | None, hash_match: bool | None, blockers: tuple[str, ...] = (), errors: tuple[str, ...] = ()) -> DreamDexTransactionSubmissionResult:
    submitted = status in {"submitted", "submitted_recovered"}
    unknown = status in {"submission_unknown", "submission_hash_conflict", "recovery_required"}
    recovery = unknown or status in {"blocked"}
    fp = _fingerprint({"record": record.record_fingerprint if record else None, "before": before, "after": after, "status": status, "hash_match": hash_match, "unknown": unknown, "blockers": blockers}, "dreamdex/submission-result")
    return DreamDexTransactionSubmissionResult(SCHEMA_VERSION, status, record, before, after, performed, record.send_attempt_count if record else 0, response_received, local_hash, rpc_hash, hash_match, submitted, unknown, False, recovery, False, False, fp, False, blockers, errors)


def _precondition_errors(material: DreamDexTransactionSigningMaterial, artifact: DreamDexVerifiedSignedTransactionArtifact, ephemeral: DreamDexEphemeralSignedTransaction, policy: DreamDexTransactionSubmissionPolicy) -> tuple[str, ...]:
    errors: list[str] = []
    if artifact.intent_id != material.intent_id:
        errors.append("submission_intent_mismatch")
    if artifact.lease_id != material.lease_id or ephemeral.lease_fingerprint != material.lease_fingerprint:
        errors.append("submission_lease_mismatch")
    if artifact.signing_request_fingerprint != material.signing_request_fingerprint or ephemeral.signing_request_fingerprint != material.signing_request_fingerprint:
        errors.append("submission_signing_request_mismatch")
    if artifact.signature_status != "verified" or not artifact.verification_fingerprint:
        errors.append("submission_verified_artifact_required")
    if len(ephemeral.raw_signed_transaction) > policy.maximum_signed_payload_bytes:
        errors.append("submission_payload_limit_exceeded")
    if policy.required_chain_id is not None and artifact.chain_id != policy.required_chain_id:
        errors.append("submission_chain_mismatch")
    if policy.required_signer_address is not None and artifact.signer_address != policy.required_signer_address:
        errors.append("submission_signer_mismatch")
    if policy.required_target_address is not None and artifact.target_address != policy.required_target_address:
        errors.append("submission_target_mismatch")
    return tuple(dict.fromkeys(errors))


def run_transaction_submission_session(*, journal: DreamDexExecutionJournal, material: DreamDexTransactionSigningMaterial, artifact: DreamDexVerifiedSignedTransactionArtifact, ephemeral_signed_transaction: DreamDexEphemeralSignedTransaction, submitter: DreamDexRawTransactionSubmitter, policy: DreamDexTransactionSubmissionPolicy | None = None) -> DreamDexTransactionSubmissionResult:
    policy = policy or DreamDexTransactionSubmissionPolicy()
    existing = journal.get_transaction_submission(intent_id=material.intent_id)
    intent = journal.get_execution_intent(material.intent_id)
    before = intent.state if intent else "unavailable"
    if existing is not None:
        record = _record_from_row(existing)
        return _result(status="existing", record=record, before=before, after=before, performed=False, response_received=existing.get("send_completed_at_unix_ms") is not None, local_hash=existing.get("signed_transaction_hash"), rpc_hash=existing.get("rpc_returned_hash"), hash_match=True if existing.get("local_hash_match_status") == "confirmed" else False if existing.get("local_hash_match_status") == "mismatch" else None, blockers=("submission_attempt_already_started", "automatic_submission_retry_disabled"))
    errors = list(_precondition_errors(material, artifact, ephemeral_signed_transaction, policy))
    if intent is None or intent.state != JournalState.SIGNED.value:
        errors.append("submission_precondition_failed")
    if errors:
        return _result(status="blocked", record=None, before=before, after=before, performed=False, response_received=False, local_hash=None, rpc_hash=None, hash_match=None, blockers=("submission_precondition_failed",), errors=tuple(errors))
    raw_ref: DreamDexEphemeralSignedTransaction | None = ephemeral_signed_transaction
    try:
        try:
            decoded = decode_signed_transaction(raw_ref.raw_signed_transaction)
            verification = verify_signed_transaction(material, raw_ref, decoded)
        except Exception:
            decoded = None
            verification = None
        if decoded is None or verification is None or not verification.verified or decoded.signed_transaction_hash != artifact.signed_transaction_hash or decoded.signed_payload_length != artifact.signed_payload_length or verification.verification_fingerprint != artifact.verification_fingerprint:
            return _result(status="blocked", record=None, before=before, after=before, performed=False, response_received=False, local_hash=decoded.signed_transaction_hash if decoded else None, rpc_hash=None, hash_match=False, blockers=("submission_precondition_failed", "signed_transaction_verification_failed"), errors=("verified_artifact_fingerprint_mismatch",))
        local_hash = decoded.signed_transaction_hash
        record_fp = _fingerprint({"schema_version": SCHEMA_VERSION, "intent_id": material.intent_id, "reservation_id": material.reservation_id, "lease_id": material.lease_id, "verified_artifact_fingerprint": artifact.verification_fingerprint, "signed_transaction_hash": local_hash, "payload_length": len(raw_ref.raw_signed_transaction), "submission_status": "submission_started", "attempt_count": 1}, "dreamdex/submission-record")
        submission_id = _fingerprint({"intent_id": material.intent_id, "signed_transaction_hash": local_hash}, "dreamdex/submission-id")
        status, row, reasons = journal.persist_submission_started(submission_id=submission_id, intent_id=material.intent_id, reservation_id=material.reservation_id, lease_id=material.lease_id, verified_artifact_fingerprint=artifact.verification_fingerprint, signed_transaction_hash=local_hash, signed_payload_length=len(raw_ref.raw_signed_transaction), record_fingerprint=record_fp)
        if status == "existing":
            record = _record_from_row(row)
            return _result(status="existing", record=record, before=before, after=journal.get_execution_intent(material.intent_id).state if journal.get_execution_intent(material.intent_id) else before, performed=False, response_received=False, local_hash=local_hash, rpc_hash=row.get("rpc_returned_hash") if row else None, hash_match=True if row and row.get("local_hash_match_status") == "confirmed" else None, blockers=("submission_attempt_already_started", "automatic_submission_retry_disabled"))
        if status != "created" or row is None:
            return _result(status="blocked", record=None, before=before, after=before, performed=False, response_received=False, local_hash=local_hash, rpc_hash=None, hash_match=None, blockers=tuple(dict.fromkeys((*reasons, "submission_precondition_failed"))))
        try:
            response = submitter.submit_raw_transaction(raw_ref)
        except DreamDexRpcError:
            journal.mark_submission_unknown(intent_id=material.intent_id)
            row = journal.get_transaction_submission(intent_id=material.intent_id)
            return _result(status="submission_unknown", record=_record_from_row(row), before=before, after=JournalState.SUBMISSION_UNKNOWN.value, performed=True, response_received=False, local_hash=local_hash, rpc_hash=None, hash_match=None, blockers=("submission_outcome_unknown", "automatic_submission_retry_disabled", "submission_recovery_required"))
        except Exception:
            journal.mark_submission_unknown(intent_id=material.intent_id)
            row = journal.get_transaction_submission(intent_id=material.intent_id)
            return _result(status="submission_unknown", record=_record_from_row(row), before=before, after=JournalState.SUBMISSION_UNKNOWN.value, performed=True, response_received=False, local_hash=local_hash, rpc_hash=None, hash_match=None, blockers=("submission_outcome_unknown", "automatic_submission_retry_disabled", "submission_recovery_required"))
        if not isinstance(response, DreamDexRawTransactionSubmissionResponse):
            journal.mark_submission_unknown(intent_id=material.intent_id)
            row = journal.get_transaction_submission(intent_id=material.intent_id)
            return _result(status="submission_unknown", record=_record_from_row(row), before=before, after=JournalState.SUBMISSION_UNKNOWN.value, performed=True, response_received=True, local_hash=local_hash, rpc_hash=None, hash_match=None, blockers=("submission_response_unavailable", "submission_outcome_unknown"))
        rpc_hash = response.rpc_returned_transaction_hash
        if response.response_status == "accepted" and rpc_hash is not None:
            match = rpc_hash == local_hash
            if match and policy.require_exact_rpc_hash_match:
                journal.mark_submission_submitted(intent_id=material.intent_id, rpc_returned_hash=rpc_hash)
                row = journal.get_transaction_submission(intent_id=material.intent_id)
                return _result(status="submitted", record=_record_from_row(row), before=before, after=JournalState.SUBMITTED.value, performed=True, response_received=True, local_hash=local_hash, rpc_hash=rpc_hash, hash_match=True)
            journal.mark_submission_hash_conflict(intent_id=material.intent_id, rpc_returned_hash=rpc_hash)
            row = journal.get_transaction_submission(intent_id=material.intent_id)
            return _result(status="submission_hash_conflict", record=_record_from_row(row), before=before, after=JournalState.RECOVERY_REQUIRED.value, performed=True, response_received=True, local_hash=local_hash, rpc_hash=rpc_hash, hash_match=False, blockers=("submission_hash_mismatch", "submission_recovery_required", "automatic_submission_retry_disabled"))
        if response.response_status == "rejected":
            journal.mark_submission_rejected(intent_id=material.intent_id)
            row = journal.get_transaction_submission(intent_id=material.intent_id)
            return _result(status="rejected", record=_record_from_row(row), before=before, after=JournalState.FAILED_PRE_SUBMISSION.value, performed=True, response_received=response.response_received, local_hash=local_hash, rpc_hash=rpc_hash, hash_match=False if rpc_hash else None, blockers=("submission_deterministic_rejection", "automatic_submission_retry_disabled"))
        journal.mark_submission_unknown(intent_id=material.intent_id)
        row = journal.get_transaction_submission(intent_id=material.intent_id)
        return _result(status="submission_unknown", record=_record_from_row(row), before=before, after=JournalState.SUBMISSION_UNKNOWN.value, performed=True, response_received=response.response_received, local_hash=local_hash, rpc_hash=rpc_hash, hash_match=False if rpc_hash else None, blockers=("submission_outcome_unknown", "automatic_submission_retry_disabled", "submission_recovery_required"))
    finally:
        raw_ref = None


def _lookup_value(value: Any, *names: str) -> Any:
    if isinstance(value, Mapping):
        for name in names:
            if name in value:
                return value[name]
    return None


def _norm_quantity(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return int(value, 16) if value.lower().startswith("0x") else int(value)
        except (TypeError, ValueError):
            return value
    return value


def _norm_tx_type(value: Any) -> Any:
    if isinstance(value, str) and value.lower() in {"0x0", "0x1", "0x2", "0x3"}:
        return {"0x0": "legacy", "0x1": "0x1", "0x2": "eip1559", "0x3": "0x3"}[value.lower()]
    return value


def _parse_lookup(found: Any) -> dict[str, Any]:
    if found is None:
        return {}
    return {
        "hash": _lookup_value(found, "hash", "transactionHash"),
        "chain_id": _norm_quantity(_lookup_value(found, "chainId", "chain_id")),
        "sender": _lookup_value(found, "from", "sender", "sender_address"),
        "nonce": _norm_quantity(_lookup_value(found, "nonce")),
        "target": _lookup_value(found, "to", "target", "target_address"),
        "value": _norm_quantity(_lookup_value(found, "value", "value_wei")),
        "gas": _norm_quantity(_lookup_value(found, "gas", "gasLimit", "gas_limit")),
        "type": _norm_tx_type(_lookup_value(found, "type", "transaction_type")),
        "gas_price": _norm_quantity(_lookup_value(found, "gasPrice", "gas_price_wei")),
        "max_fee": _norm_quantity(_lookup_value(found, "maxFeePerGas", "max_fee_per_gas_wei")),
        "priority": _norm_quantity(_lookup_value(found, "maxPriorityFeePerGas", "max_priority_fee_per_gas_wei")),
        "input": _lookup_value(found, "input", "data", "calldata"),
    }


def recover_transaction_submission(*, journal: DreamDexExecutionJournal, intent_id: str, reader: DreamDexTransactionRecoveryReader, artifact: DreamDexVerifiedSignedTransactionArtifact, material: DreamDexTransactionSigningMaterial | None = None) -> tuple[DreamDexTransactionSubmissionResult, DreamDexSubmissionRecoveryEvidence]:
    row = journal.get_transaction_submission(intent_id=intent_id)
    intent = journal.get_execution_intent(intent_id)
    before = intent.state if intent else "unavailable"
    if row is None:
        evidence = DreamDexSubmissionRecoveryEvidence(SCHEMA_VERSION, "0x" + "1" * 64, False, None, None, None, None, None, None, None, None, None, None, "not_checked", False, ("submission_record_unavailable",))
        return _result(status="blocked", record=None, before=before, after=before, performed=False, response_received=False, local_hash=None, rpc_hash=None, hash_match=None, blockers=("submission_record_unavailable",)), evidence
    record = _record_from_row(row)
    tx_hash = str(row["signed_transaction_hash"])
    if row.get("recovery_checked_at_unix_ms") is not None:
        evidence = DreamDexSubmissionRecoveryEvidence(SCHEMA_VERSION, tx_hash, False, None, None, None, None, None, None, None, None, None, None, "already_checked", False, ("submission_recovery_required",))
        return _result(status=str(row["submission_status"]), record=record, before=before, after=before, performed=False, response_received=False, local_hash=tx_hash, rpc_hash=row.get("rpc_returned_hash"), hash_match=None, blockers=("submission_recovery_required",)), evidence
    try:
        found = reader.get_transaction_by_hash(tx_hash)
    except Exception:
        journal.mark_submission_recovery_checked(intent_id=intent_id)
        evidence = DreamDexSubmissionRecoveryEvidence(SCHEMA_VERSION, tx_hash, True, None, None, None, None, None, None, None, None, None, None, "error", False, ("submission_recovery_required",))
        return _result(status="submission_unknown", record=_record_from_row(journal.get_transaction_submission(intent_id=intent_id)), before=before, after=JournalState.SUBMISSION_UNKNOWN.value, performed=False, response_received=False, local_hash=tx_hash, rpc_hash=None, hash_match=None, blockers=("submission_recovery_required",)), evidence
    if found is None:
        journal.mark_submission_recovery_checked(intent_id=intent_id)
        evidence = DreamDexSubmissionRecoveryEvidence(SCHEMA_VERSION, tx_hash, True, False, None, None, None, None, None, None, None, None, None, "not_found", False, ("submission_transaction_not_found", "submission_recovery_required"))
        return _result(status="submission_unknown", record=_record_from_row(journal.get_transaction_submission(intent_id=intent_id)), before=before, after=JournalState.SUBMISSION_UNKNOWN.value, performed=False, response_received=True, local_hash=tx_hash, rpc_hash=None, hash_match=None, blockers=("submission_transaction_not_found", "submission_recovery_required")), evidence
    values = _parse_lookup(found)
    expected = {"hash": tx_hash, "chain_id": artifact.chain_id, "sender": artifact.signer_address, "nonce": artifact.nonce, "target": artifact.target_address, "value": material.finalized_envelope.value_wei if material else None, "gas": material.finalized_envelope.gas_limit if material else None, "type": artifact.transaction_type}
    comparisons = []
    for key, expected_value in expected.items():
        if expected_value is not None and values.get(key) is not None:
            comparisons.append(values.get(key) == expected_value if key not in {"hash", "sender", "target"} else str(values.get(key)).lower() == str(expected_value).lower())
    input_value = values.get("input")
    input_match = None
    if material is not None and input_value is not None:
        try:
            raw_input = bytes.fromhex(input_value[2:] if isinstance(input_value, str) and input_value.startswith("0x") else str(input_value)) if isinstance(input_value, str) else bytes(input_value)
            input_match = sha256(raw_input).hexdigest() == material.finalized_envelope.calldata_sha256
        except (TypeError, ValueError):
            input_match = False
        comparisons.append(input_match)
    if material is not None:
        env = material.finalized_envelope
        if artifact.transaction_type == "legacy" and values.get("gas_price") is not None and env.gas_price_wei is not None:
            comparisons.append(values.get("gas_price") == env.gas_price_wei)
        if artifact.transaction_type == "eip1559":
            if values.get("max_fee") is not None and env.max_fee_per_gas_wei is not None:
                comparisons.append(values.get("max_fee") == env.max_fee_per_gas_wei)
            if values.get("priority") is not None and env.max_priority_fee_per_gas_wei is not None:
                comparisons.append(values.get("priority") == env.max_priority_fee_per_gas_wei)
    exact = bool(comparisons) and all(comparisons)
    if exact:
        journal.mark_submission_recovered(intent_id=intent_id, rpc_returned_hash=tx_hash)
        evidence = DreamDexSubmissionRecoveryEvidence(SCHEMA_VERSION, tx_hash, True, True, values.get("chain_id"), values.get("sender"), values.get("nonce"), values.get("target"), values.get("value"), values.get("gas"), values.get("type"), None, True, "found", False)
        return _result(status="submitted_recovered", record=_record_from_row(journal.get_transaction_submission(intent_id=intent_id)), before=before, after=JournalState.SUBMITTED.value, performed=False, response_received=True, local_hash=tx_hash, rpc_hash=tx_hash, hash_match=True), evidence
    journal.mark_submission_recovery_required(intent_id=intent_id)
    evidence = DreamDexSubmissionRecoveryEvidence(SCHEMA_VERSION, tx_hash, True, True, values.get("chain_id"), values.get("sender"), values.get("nonce"), values.get("target"), values.get("value"), values.get("gas"), values.get("type"), None, False, "mismatch", False, ("submission_transaction_field_mismatch", "submission_recovery_required"))
    return _result(status="recovery_required", record=_record_from_row(journal.get_transaction_submission(intent_id=intent_id)), before=before, after=JournalState.RECOVERY_REQUIRED.value, performed=False, response_received=True, local_hash=tx_hash, rpc_hash=values.get("hash"), hash_match=False, blockers=("submission_transaction_field_mismatch", "submission_recovery_required")), evidence


def build_transaction_submission_preview(result: DreamDexTransactionSubmissionResult | None = None, *, production_submitter_status: str = "unavailable", recovery_lookup_available: bool = True, recovery: DreamDexSubmissionRecoveryEvidence | None = None) -> DreamDexTransactionSubmissionPreview:
    blockers = result.blockers if result else ("raw_transaction_submission_unavailable", "transaction_signer_unavailable")
    return DreamDexTransactionSubmissionPreview(result.status if result else "unavailable", production_submitter_status, bool(result and result.submission_execution_performed), False, False, True, bool(result and result.local_transaction_hash), bool(result and result.submission_record is not None), bool(result and result.submission_record and result.submission_record.send_attempt_started), result.send_attempt_count if result else 0, bool(result and result.response_received), result.hash_match if result else None, result.journal_state_after if result else "unavailable", bool(result and result.submitted), bool(result and result.submission_unknown), False, False, recovery_lookup_available, recovery.lookup_performed if recovery else False, recovery.transaction_found if recovery else None, False, False, False, blockers)


def build_submission_recovery_preview(evidence: DreamDexSubmissionRecoveryEvidence | None = None) -> dict[str, Any]:
    return evidence.safe_dict() if evidence else {"lookup_performed": False, "transaction_found": None, "lookup_status": "not_checked", "authoritative": False, "blockers": ("submission_recovery_required",)}


def serialize_transaction_submission_diagnostics(value: DreamDexTransactionSubmissionResult | DreamDexTransactionSubmissionPreview | DreamDexSubmissionRecoveryEvidence | Mapping[str, Any] | None = None) -> dict[str, Any]:
    if value is None:
        return build_transaction_submission_preview().safe_dict()
    if hasattr(value, "safe_dict"):
        return value.safe_dict()  # type: ignore[no-any-return]
    if isinstance(value, Mapping):
        output = dict(value)
        for forbidden in ("raw_signed_transaction", "raw_signature", "calldata", "rpc_payload", "rpc_response", "transaction_hash", "signed_transaction_hash", "rpc_returned_transaction_hash"):
            if forbidden in output:
                raise ValueError("raw_submission_diagnostic_forbidden")
        return ensure_no_raw_sensitive_fields(output)
    raise TypeError("unsupported_submission_diagnostics_type")


submit_transaction_once = run_transaction_submission_session
recover_submission_by_hash = recover_transaction_submission
DreamDexRawTransactionSubmissionTransport = DreamDexRawTransactionHttpSubmitter
DreamDexReadOnlyTransactionRecoveryReader = DreamDexTransactionByHashHttpReader


def __getattr__(name: str) -> Any:
    """Lazily expose the disarmed production adapter without a circular import."""
    if name == "HttpDreamDexRawTransactionSubmitter":
        from bot.execution.dreamdex_production_rpc import HttpDreamDexRawTransactionSubmitter
        return HttpDreamDexRawTransactionSubmitter
    if name == "HttpDreamDexTransactionRecoveryReader":
        from bot.execution.dreamdex_production_rpc import HttpDreamDexTransactionRecoveryReader
        return HttpDreamDexTransactionRecoveryReader
    if name == "DreamDexProductionRpcPolicy":
        from bot.execution.dreamdex_production_rpc import DreamDexProductionRpcPolicy
        return DreamDexProductionRpcPolicy
    raise AttributeError(name)


__all__ = [name for name in globals() if name.startswith("DreamDex") or name.startswith("run_") or name.startswith("recover_") or name.startswith("build_") or name.startswith("serialize_")] + ["submit_transaction_once", "recover_submission_by_hash", "HttpDreamDexRawTransactionSubmitter", "HttpDreamDexTransactionRecoveryReader", "DreamDexProductionRpcPolicy"]
