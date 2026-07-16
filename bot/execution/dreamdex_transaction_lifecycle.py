"""Offline transaction lifecycle and receipt/event evidence.

Only explicitly supplied external metadata is accepted.  This module has no
RPC, HTTP, websocket, provider, signer, subprocess, polling or receipt-fetch
implementation.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
import json
import re
from typing import Any, Mapping, Sequence

from bot.execution.dreamdex_direct_order_encoding import ORDER_CANCELLED_TOPIC, ORDER_PLACED_TOPIC
from bot.execution.dreamdex_transaction_envelope import (
    CHAIN_ID,
    ENVELOPE_SCHEMA_VERSION,
    SOURCE_TYPES,
    DreamDexUnsignedTransactionEnvelope,
)


SCHEMA_VERSION = "1"
LIFECYCLE_STATES = frozenset({
    "prepared", "externally_signed", "externally_submitted", "pending_external_confirmation",
    "confirmed_success", "confirmed_reverted", "confirmed_missing_required_event",
    "replaced_external", "dropped_external", "unknown_external_state", "unavailable",
})
SUBMISSION_CHANNELS = frozenset({"external_wallet", "external_sidecar", "manual_import", "test_fixture", "unavailable"})
REPLACEMENT_REASONS = frozenset({"fee_bump", "cancel_replacement", "external_replacement", "unavailable"})
RECEIPT_STATUSES = frozenset({"success", "reverted", "unavailable"})
_HASH_RE = re.compile(r"^0x[0-9a-fA-F]{64}$", re.IGNORECASE)
_DIGEST_RE = re.compile(r"^(?:0x)?[0-9a-fA-F]{64}$", re.IGNORECASE)
_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$", re.IGNORECASE)
_MAX_UINT128 = (1 << 128) - 1


def _hash(value: Any, field: str, *, allow_none: bool = True) -> str | None:
    if value is None and allow_none:
        return None
    if isinstance(value, bool) or not isinstance(value, str) or not _HASH_RE.fullmatch(value):
        raise ValueError(f"{field}: invalid_hash")
    canonical = value.lower()
    if canonical == "0x" + "0" * 64:
        raise ValueError(f"{field}: zero_hash")
    return canonical


def _digest(value: Any, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not _DIGEST_RE.fullmatch(value):
        raise ValueError(f"{field}: invalid_digest")
    return value.lower()


def _address(value: Any, field: str, *, allow_none: bool = True) -> str | None:
    if value is None and allow_none:
        return None
    if not isinstance(value, str) or not _ADDRESS_RE.fullmatch(value):
        raise ValueError(f"{field}: invalid_address")
    return value.lower()


def _number(value: Any, field: str, *, allow_none: bool = True, maximum: int = (1 << 256) - 1) -> int | None:
    if value is None and allow_none:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > maximum:
        raise ValueError(f"{field}: invalid_nonnegative_integer")
    return value


def _unique(values: Sequence[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(str(value) for value in values))


def _canonical(value: Mapping[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _masked_hash(value: str | None) -> str:
    if not value:
        return "<missing>"
    return value[:6] + "..." + value[-4:]


def _masked_address(value: str | None) -> str:
    if not value:
        return "<missing>"
    return value[:4] + "..." + value[-4:]


@dataclass(frozen=True, repr=False)
class DreamDexTransactionLifecycleEvidence:
    source_type: str = "unavailable"
    source_status: str = "unavailable"
    transaction_hash_status: str = "unavailable"
    submission_status: str = "unavailable"
    receipt_status: str = "unavailable"
    receipt_success_status: str = "unavailable"
    block_number_status: str = "unavailable"
    block_hash_status: str = "unavailable"
    transaction_index_status: str = "unavailable"
    from_address_status: str = "unavailable"
    to_address_status: str = "unavailable"
    chain_id_status: str = "unavailable"
    event_status: str = "unavailable"
    replacement_status: str = "unavailable"
    authoritative: bool = False
    conflicts: tuple[str, ...] = ()
    unresolved_reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.source_type not in SOURCE_TYPES:
            raise ValueError("source_type: unsupported")
        if self.authoritative:
            raise ValueError("lifecycle evidence cannot be authoritative")
        object.__setattr__(self, "conflicts", _unique(self.conflicts))
        object.__setattr__(self, "unresolved_reasons", _unique(self.unresolved_reasons))

    def safe_dict(self) -> dict[str, Any]:
        return {
            "source_type": self.source_type, "source_status": self.source_status,
            "transaction_hash_status": self.transaction_hash_status, "submission_status": self.submission_status,
            "receipt_status": self.receipt_status, "receipt_success_status": self.receipt_success_status,
            "block_number_status": self.block_number_status, "block_hash_status": self.block_hash_status,
            "transaction_index_status": self.transaction_index_status, "from_address_status": self.from_address_status,
            "to_address_status": self.to_address_status, "chain_id_status": self.chain_id_status,
            "event_status": self.event_status, "replacement_status": self.replacement_status,
            "authoritative": False, "conflicts": self.conflicts, "unresolved_reasons": self.unresolved_reasons,
        }

    def __repr__(self) -> str:
        return f"DreamDexTransactionLifecycleEvidence(source_type={self.source_type!r}, source_status={self.source_status!r}, authoritative=False)"


@dataclass(frozen=True, repr=False)
class DreamDexTransactionReceiptEvidence:
    transaction_hash: str | None = None
    block_hash: str | None = None
    block_number: int | None = None
    transaction_index: int | None = None
    status: str = "unavailable"
    from_address: str | None = None
    to_address: str | None = None
    gas_used: int | None = None
    effective_gas_price: int | None = None
    contract_address: str | None = None
    logs_count: int | None = None
    evidence_source: str = "unavailable"
    authoritative: bool = False
    validation_errors: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "transaction_hash", _hash(self.transaction_hash, "transaction_hash"))
        object.__setattr__(self, "block_hash", _hash(self.block_hash, "block_hash"))
        object.__setattr__(self, "from_address", _address(self.from_address, "from_address"))
        object.__setattr__(self, "to_address", _address(self.to_address, "to_address"))
        object.__setattr__(self, "contract_address", _address(self.contract_address, "contract_address"))
        for name in ("block_number", "transaction_index", "gas_used", "effective_gas_price", "logs_count"):
            object.__setattr__(self, name, _number(getattr(self, name), name))
        if self.status not in RECEIPT_STATUSES:
            raise ValueError("status: unsupported")
        if self.evidence_source not in SOURCE_TYPES:
            raise ValueError("evidence_source: unsupported")
        if self.authoritative:
            raise ValueError("receipt evidence cannot be authoritative")
        object.__setattr__(self, "validation_errors", _unique(self.validation_errors))

    @property
    def fingerprint(self) -> str:
        body = {
            "transaction_hash": self.transaction_hash, "block_hash": self.block_hash,
            "block_number": self.block_number, "transaction_index": self.transaction_index,
            "status": self.status, "from": self.from_address, "to": self.to_address,
            "gas_used": self.gas_used, "effective_gas_price": self.effective_gas_price,
            "contract_address": self.contract_address, "logs_count": self.logs_count,
            "evidence_source": self.evidence_source, "validation_errors": self.validation_errors,
        }
        return sha256(_canonical(body).encode("utf-8")).hexdigest()

    def safe_dict(self) -> dict[str, Any]:
        return {
            "transaction_hash": _masked_hash(self.transaction_hash), "block_hash": _masked_hash(self.block_hash),
            "block_number": self.block_number, "transaction_index": self.transaction_index,
            "status": self.status, "from_address": _masked_address(self.from_address),
            "to_address": _masked_address(self.to_address), "gas_used": self.gas_used,
            "effective_gas_price": self.effective_gas_price, "contract_address": _masked_address(self.contract_address),
            "logs_count": self.logs_count, "evidence_source": self.evidence_source,
            "authoritative": False, "validation_errors": self.validation_errors, "receipt_fingerprint": self.fingerprint,
        }

    def __repr__(self) -> str:
        return f"DreamDexTransactionReceiptEvidence(transaction_hash={_masked_hash(self.transaction_hash)!r}, status={self.status!r}, block_number={self.block_number!r}, authoritative=False)"


@dataclass(frozen=True, repr=False)
class DreamDexTransactionEventEvidence:
    event_name: str
    event_signature: str | None = None
    topic0: str | None = None
    transaction_hash: str | None = None
    block_number: int | None = None
    log_index: int | None = None
    contract_address: str | None = None
    order_id: int | None = None
    owner_address: str | None = None
    raw_topics_sha256: str | None = None
    raw_data_sha256: str | None = None
    raw_topics_count: int | None = None
    raw_data_length: int | None = None
    source_status: str = "unavailable"
    authoritative: bool = False
    validation_errors: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "transaction_hash", _hash(self.transaction_hash, "transaction_hash"))
        object.__setattr__(self, "contract_address", _address(self.contract_address, "contract_address"))
        object.__setattr__(self, "owner_address", _address(self.owner_address, "owner_address"))
        object.__setattr__(self, "raw_topics_sha256", _digest(self.raw_topics_sha256, "raw_topics_sha256"))
        object.__setattr__(self, "raw_data_sha256", _digest(self.raw_data_sha256, "raw_data_sha256"))
        for name in ("block_number", "log_index", "raw_topics_count", "raw_data_length"):
            object.__setattr__(self, name, _number(getattr(self, name), name))
        if self.order_id is not None:
            object.__setattr__(self, "order_id", _number(self.order_id, "order_id", maximum=_MAX_UINT128))
        if not isinstance(self.event_name, str) or not self.event_name:
            raise ValueError("event_name: unavailable")
        if self.source_status not in {"unavailable", "observed", "source_confirmed", "blocked"}:
            raise ValueError("source_status: unsupported")
        if self.authoritative:
            raise ValueError("event evidence cannot be authoritative")
        object.__setattr__(self, "validation_errors", _unique(self.validation_errors))

    @property
    def fingerprint(self) -> str:
        body = {
            "event_name": self.event_name, "event_signature": self.event_signature, "topic0": self.topic0,
            "transaction_hash": self.transaction_hash, "block_number": self.block_number, "log_index": self.log_index,
            "contract_address": self.contract_address, "order_id": self.order_id, "owner_address": self.owner_address,
            "raw_topics_sha256": self.raw_topics_sha256, "raw_data_sha256": self.raw_data_sha256,
            "raw_topics_count": self.raw_topics_count, "raw_data_length": self.raw_data_length,
            "source_status": self.source_status, "validation_errors": self.validation_errors,
        }
        return sha256(_canonical(body).encode("utf-8")).hexdigest()

    def safe_dict(self) -> dict[str, Any]:
        return {
            "event_name": self.event_name, "event_signature": self.event_signature, "topic0": self.topic0,
            "transaction_hash": _masked_hash(self.transaction_hash), "block_number": self.block_number,
            "log_index": self.log_index, "contract_address": _masked_address(self.contract_address),
            "order_id": self.order_id, "owner_address": _masked_address(self.owner_address),
            "raw_topics_sha256": self.raw_topics_sha256, "raw_data_sha256": self.raw_data_sha256,
            "raw_topics_count": self.raw_topics_count, "raw_data_length": self.raw_data_length,
            "source_status": self.source_status, "authoritative": False,
            "validation_errors": self.validation_errors, "event_fingerprint": self.fingerprint,
        }

    def __repr__(self) -> str:
        return f"DreamDexTransactionEventEvidence(event_name={self.event_name!r}, topic0={self.topic0!r}, transaction_hash={_masked_hash(self.transaction_hash)!r}, order_id={self.order_id!r}, authoritative=False)"


@dataclass(frozen=True, repr=False)
class DreamDexExternalSubmissionEvidence:
    transaction_hash: str | None = None
    submitted_at: datetime | None = None
    submission_channel: str = "unavailable"
    source_type: str = "unavailable"
    source_status: str = "unavailable"
    signer_address: str | None = None
    chain_id: int | None = None
    target_address: str | None = None
    request_fingerprint: str | None = None
    envelope_fingerprint: str | None = None
    authoritative: bool = False
    conflicts: tuple[str, ...] = ()
    unresolved_reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "transaction_hash", _hash(self.transaction_hash, "transaction_hash"))
        object.__setattr__(self, "signer_address", _address(self.signer_address, "signer_address"))
        object.__setattr__(self, "target_address", _address(self.target_address, "target_address"))
        object.__setattr__(self, "chain_id", _number(self.chain_id, "chain_id"))
        if self.submission_channel not in SUBMISSION_CHANNELS:
            raise ValueError("submission_channel: unsupported")
        if self.source_type not in SOURCE_TYPES:
            raise ValueError("source_type: unsupported")
        if self.authoritative:
            raise ValueError("submission evidence cannot be authoritative")
        object.__setattr__(self, "conflicts", _unique(self.conflicts))
        object.__setattr__(self, "unresolved_reasons", _unique(self.unresolved_reasons))

    def safe_dict(self) -> dict[str, Any]:
        return {
            "transaction_hash": _masked_hash(self.transaction_hash), "submitted_at": self.submitted_at.isoformat() if isinstance(self.submitted_at, datetime) else None,
            "submission_channel": self.submission_channel, "source_type": self.source_type, "source_status": self.source_status,
            "signer_address": _masked_address(self.signer_address), "chain_id": self.chain_id,
            "target_address": _masked_address(self.target_address), "request_fingerprint": self.request_fingerprint,
            "envelope_fingerprint": self.envelope_fingerprint, "authoritative": False,
            "conflicts": self.conflicts, "unresolved_reasons": self.unresolved_reasons,
        }

    def __repr__(self) -> str:
        return f"DreamDexExternalSubmissionEvidence(transaction_hash={_masked_hash(self.transaction_hash)!r}, submission_channel={self.submission_channel!r}, authoritative=False)"


@dataclass(frozen=True, repr=False)
class DreamDexTransactionReplacementEvidence:
    original_transaction_hash: str
    replacement_transaction_hash: str
    replacement_reason: str = "unavailable"
    nonce_match_status: str = "unavailable"
    from_match_status: str = "unavailable"
    chain_match_status: str = "unavailable"
    source_type: str = "unavailable"
    source_status: str = "unavailable"
    authoritative: bool = False
    conflicts: tuple[str, ...] = ()
    unresolved_reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "original_transaction_hash", _hash(self.original_transaction_hash, "original_transaction_hash", allow_none=False))
        object.__setattr__(self, "replacement_transaction_hash", _hash(self.replacement_transaction_hash, "replacement_transaction_hash", allow_none=False))
        if self.original_transaction_hash == self.replacement_transaction_hash:
            raise ValueError("replacement_hashes_identical")
        if self.replacement_reason not in REPLACEMENT_REASONS:
            raise ValueError("replacement_reason: unsupported")
        if self.source_type not in SOURCE_TYPES:
            raise ValueError("source_type: unsupported")
        if self.authoritative:
            raise ValueError("replacement evidence cannot be authoritative")
        object.__setattr__(self, "conflicts", _unique(self.conflicts))
        object.__setattr__(self, "unresolved_reasons", _unique(self.unresolved_reasons))

    def safe_dict(self) -> dict[str, Any]:
        return {
            "original_transaction_hash": _masked_hash(self.original_transaction_hash),
            "replacement_transaction_hash": _masked_hash(self.replacement_transaction_hash),
            "replacement_reason": self.replacement_reason, "nonce_match_status": self.nonce_match_status,
            "from_match_status": self.from_match_status, "chain_match_status": self.chain_match_status,
            "source_type": self.source_type, "source_status": self.source_status, "authoritative": False,
            "conflicts": self.conflicts, "unresolved_reasons": self.unresolved_reasons,
        }

    def __repr__(self) -> str:
        return f"DreamDexTransactionReplacementEvidence(original={_masked_hash(self.original_transaction_hash)!r}, replacement={_masked_hash(self.replacement_transaction_hash)!r}, authoritative=False)"


@dataclass(frozen=True, repr=False)
class DreamDexTransactionLifecycleRecord:
    schema_version: str
    lifecycle_id: str
    operation: str
    request_fingerprint: str | None
    envelope_fingerprint: str | None
    transaction_hash: str | None
    current_state: str
    previous_state: str | None
    transition_reason: str
    receipt_evidence: DreamDexTransactionReceiptEvidence | None
    event_evidence: tuple[DreamDexTransactionEventEvidence, ...]
    evidence: DreamDexTransactionLifecycleEvidence
    order_id: int | None
    replacement_transaction_hash: str | None
    lifecycle_fingerprint: str
    authoritative: bool
    reconciliation_status: str
    blockers: tuple[str, ...]
    submission_evidence: DreamDexExternalSubmissionEvidence | None = None
    replacement_evidence: DreamDexTransactionReplacementEvidence | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "transaction_hash", _hash(self.transaction_hash, "transaction_hash"))
        object.__setattr__(self, "replacement_transaction_hash", _hash(self.replacement_transaction_hash, "replacement_transaction_hash"))
        if self.current_state not in LIFECYCLE_STATES or (self.previous_state is not None and self.previous_state not in LIFECYCLE_STATES):
            raise ValueError("lifecycle_state: unsupported")
        if self.order_id is not None:
            object.__setattr__(self, "order_id", _number(self.order_id, "order_id", maximum=_MAX_UINT128))
        if self.authoritative:
            raise ValueError("lifecycle record cannot be authoritative")
        if isinstance(self.event_evidence, DreamDexTransactionEventEvidence):
            object.__setattr__(self, "event_evidence", (self.event_evidence,))
        else:
            object.__setattr__(self, "event_evidence", tuple(self.event_evidence))
        object.__setattr__(self, "blockers", _unique(self.blockers))

    @property
    def receipt_fingerprint(self) -> str | None:
        return self.receipt_evidence.fingerprint if self.receipt_evidence else None

    @property
    def event_fingerprint(self) -> str | None:
        if not self.event_evidence:
            return None
        return sha256(_canonical({"events": [event.fingerprint for event in self.event_evidence]}).encode("utf-8")).hexdigest()

    def safe_dict(self) -> dict[str, Any]:
        return serialize_transaction_lifecycle_diagnostics(self)

    def __repr__(self) -> str:
        return f"DreamDexTransactionLifecycleRecord(operation={self.operation!r}, current_state={self.current_state!r}, transaction_hash={_masked_hash(self.transaction_hash)!r}, authoritative=False)"


@dataclass(frozen=True, repr=False)
class DreamDexTransactionLifecyclePreview:
    operation: str
    transaction_hash_masked: str
    current_state: str
    previous_state: str | None
    request_fingerprint: str | None
    envelope_fingerprint: str | None
    receipt_status: str
    event_status: str
    order_id_status: str
    replacement_status: str
    lifecycle_fingerprint: str
    authoritative: bool
    reconciliation_status: str
    blockers: tuple[str, ...]

    def safe_dict(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}

    def __repr__(self) -> str:
        return f"DreamDexTransactionLifecyclePreview(operation={self.operation!r}, current_state={self.current_state!r}, transaction_hash={self.transaction_hash_masked!r}, authoritative=False)"


@dataclass(frozen=True)
class LifecycleValidationResult:
    valid: bool
    status: str
    errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class LifecycleTransitionResult:
    allowed: bool
    status: str
    errors: tuple[str, ...] = ()


def _lifecycle_fingerprint(record_fields: Mapping[str, Any]) -> str:
    return sha256(_canonical(record_fields).encode("utf-8")).hexdigest()


def compute_receipt_fingerprint(receipt: DreamDexTransactionReceiptEvidence) -> str:
    return receipt.fingerprint


def compute_event_fingerprint(event: DreamDexTransactionEventEvidence) -> str:
    return event.fingerprint


def compute_lifecycle_fingerprint(*, schema_version: str, operation: str, request_fingerprint: str | None, envelope_fingerprint: str | None, transaction_hash: str | None, current_state: str, previous_state: str | None, receipt_fingerprint: str | None, event_fingerprint: str | None, order_id: int | None, replacement_transaction_hash: str | None, evidence_source: str, evidence_status: str) -> str:
    return _lifecycle_fingerprint({
        "schema_version": schema_version, "operation": operation, "request_fingerprint": request_fingerprint,
        "envelope_fingerprint": envelope_fingerprint, "transaction_hash": transaction_hash or "<unavailable>",
        "current_state": current_state, "previous_state": previous_state, "receipt_fingerprint": receipt_fingerprint,
        "event_fingerprint": event_fingerprint, "order_id": order_id,
        "replacement_transaction_hash": replacement_transaction_hash, "evidence_source": evidence_source,
        "evidence_status": evidence_status,
    })


def _make_record(*, envelope: DreamDexUnsignedTransactionEnvelope | None, lifecycle_id: str, operation: str, transaction_hash: str | None, current_state: str, previous_state: str | None, reason: str, receipt: DreamDexTransactionReceiptEvidence | None = None, events: Sequence[DreamDexTransactionEventEvidence] = (), evidence: DreamDexTransactionLifecycleEvidence | None = None, order_id: int | None = None, replacement_hash: str | None = None, blockers: Sequence[str] = (), submission: DreamDexExternalSubmissionEvidence | None = None, replacement: DreamDexTransactionReplacementEvidence | None = None, request_fingerprint: str | None = None, envelope_fingerprint: str | None = None, add_envelope_blocker: bool = True) -> DreamDexTransactionLifecycleRecord:
    evidence = evidence or DreamDexTransactionLifecycleEvidence(source_type="unavailable", unresolved_reasons=("transaction_lifecycle_evidence_unavailable",))
    request_fp = getattr(envelope, "request_fingerprint", request_fingerprint)
    envelope_fp = getattr(envelope, "envelope_fingerprint", envelope_fingerprint)
    all_blockers = list(blockers)
    all_blockers.extend(evidence.conflicts)
    all_blockers.extend(evidence.unresolved_reasons)
    if envelope is None and add_envelope_blocker:
        all_blockers.append("transaction_envelope_unavailable")
    if current_state in {"externally_signed", "externally_submitted", "pending_external_confirmation", "unknown_external_state"}:
        all_blockers.extend(("transaction_lifecycle_non_authoritative", "transaction_receipt_evidence_unavailable"))
    lifecycle_fp = compute_lifecycle_fingerprint(schema_version=SCHEMA_VERSION, operation=operation, request_fingerprint=request_fp, envelope_fingerprint=envelope_fp, transaction_hash=transaction_hash, current_state=current_state, previous_state=previous_state, receipt_fingerprint=receipt.fingerprint if receipt else None, event_fingerprint=sha256(_canonical({"events": [event.fingerprint for event in events]}).encode("utf-8")).hexdigest() if events else None, order_id=order_id, replacement_transaction_hash=replacement_hash, evidence_source=evidence.source_type, evidence_status=evidence.source_status)
    return DreamDexTransactionLifecycleRecord(SCHEMA_VERSION, lifecycle_id, operation, request_fp, envelope_fp, transaction_hash, current_state, previous_state, reason, receipt, tuple(events), evidence, order_id, replacement_hash, lifecycle_fp, False, "incomplete", tuple(all_blockers), submission, replacement)


def create_prepared_lifecycle(envelope: DreamDexUnsignedTransactionEnvelope | None, *, lifecycle_id: str = "offline-lifecycle", operation: str | None = None) -> DreamDexTransactionLifecycleRecord:
    resolved_operation = operation or getattr(envelope, "operation", "unavailable")
    if envelope is None:
        return _make_record(envelope=None, lifecycle_id=lifecycle_id, operation=resolved_operation, transaction_hash=None, current_state="unavailable", previous_state=None, reason="envelope_unavailable")
    return _make_record(envelope=envelope, lifecycle_id=lifecycle_id, operation=resolved_operation, transaction_hash=None, current_state="prepared", previous_state=None, reason="prepared_from_unsigned_envelope", evidence=DreamDexTransactionLifecycleEvidence(source_type="unavailable", source_status="unavailable", unresolved_reasons=("transaction_submission_evidence_unavailable",)))


def create_transaction_lifecycle(envelope: DreamDexUnsignedTransactionEnvelope | None, *, lifecycle_id: str = "offline-lifecycle", operation: str | None = None) -> DreamDexTransactionLifecycleRecord:
    return create_prepared_lifecycle(envelope, lifecycle_id=lifecycle_id, operation=operation)


def validate_external_submission_evidence(submission: DreamDexExternalSubmissionEvidence, *, envelope: DreamDexUnsignedTransactionEnvelope) -> LifecycleValidationResult:
    errors: list[str] = []
    if submission.transaction_hash is None:
        errors.append("transaction_hash_unavailable")
    if submission.signer_address != envelope.from_address:
        errors.append("submission_signer_mismatch")
    if submission.chain_id != envelope.chain_id:
        errors.append("submission_chain_mismatch")
    if submission.target_address != envelope.to_address:
        errors.append("submission_target_mismatch")
    if submission.request_fingerprint != envelope.request_fingerprint:
        errors.append("submission_request_fingerprint_mismatch")
    if submission.envelope_fingerprint != envelope.envelope_fingerprint:
        errors.append("submission_envelope_fingerprint_mismatch")
    errors.extend(submission.conflicts)
    errors.extend(submission.unresolved_reasons)
    unique = _unique(errors)
    return LifecycleValidationResult(not unique, "valid" if not unique else "blocked", unique)


def import_external_submission(envelope: DreamDexUnsignedTransactionEnvelope, submission: DreamDexExternalSubmissionEvidence, *, lifecycle_id: str = "offline-lifecycle") -> DreamDexTransactionLifecycleRecord:
    result = validate_external_submission_evidence(submission, envelope=envelope)
    if not result.valid:
        raise ValueError("external submission evidence blocked: " + ";".join(result.errors))
    evidence = DreamDexTransactionLifecycleEvidence(source_type=submission.source_type, source_status=submission.source_status, transaction_hash_status="externally_supplied", submission_status="externally_supplied", chain_id_status="externally_supplied", from_address_status="externally_supplied", to_address_status="externally_supplied")
    return _make_record(envelope=envelope, lifecycle_id=lifecycle_id, operation=envelope.operation, transaction_hash=submission.transaction_hash, current_state="externally_submitted", previous_state="prepared", reason="external_submission_evidence", evidence=evidence, submission=submission, blockers=("transaction_lifecycle_non_authoritative",))


def validate_receipt_evidence(receipt: DreamDexTransactionReceiptEvidence, *, transaction_hash: str | None = None, envelope: DreamDexUnsignedTransactionEnvelope | None = None, expected_pool: str | None = None, expected_from: str | None = None) -> LifecycleValidationResult:
    errors: list[str] = list(receipt.validation_errors)
    if receipt.transaction_hash is None:
        errors.append("receipt_transaction_hash_unavailable")
    if transaction_hash is not None and receipt.transaction_hash != _hash(transaction_hash, "transaction_hash", allow_none=False):
        errors.append("receipt_transaction_hash_mismatch")
    target = envelope.to_address if envelope is not None else _address(expected_pool, "expected_pool")
    sender = envelope.from_address if envelope is not None else _address(expected_from, "expected_from")
    if sender is not None and receipt.from_address != sender:
        errors.append("receipt_from_mismatch")
    if target is not None and receipt.to_address != target:
        errors.append("receipt_to_mismatch")
    if receipt.contract_address is not None:
        errors.append("receipt_contract_address_for_pool_forbidden")
    if receipt.status not in {"success", "reverted"}:
        errors.append("receipt_status_unavailable")
    unique = _unique(errors)
    return LifecycleValidationResult(not unique, "valid" if not unique else "blocked", unique)


def validate_event_evidence(event: DreamDexTransactionEventEvidence, *, operation: str, transaction_hash: str | None = None, expected_pool: str | None = None, expected_order_id: int | None = None, expected_owner: str | None = None) -> LifecycleValidationResult:
    errors: list[str] = list(event.validation_errors)
    expected_event = "OrderPlaced" if operation == "place_order" else ("OrderCancelled" if operation == "cancel_order" else None)
    expected_topic = ORDER_PLACED_TOPIC if operation == "place_order" else (ORDER_CANCELLED_TOPIC if operation == "cancel_order" else None)
    if expected_event is None:
        errors.append("reduce_event_semantics_unavailable")
    elif event.event_name != expected_event or event.topic0 is None or event.topic0.lower() != expected_topic.lower():
        errors.append("event_signature_or_topic_mismatch")
    if transaction_hash is not None and event.transaction_hash != _hash(transaction_hash, "transaction_hash", allow_none=False):
        errors.append("event_transaction_hash_mismatch")
    if expected_pool is not None and event.contract_address != _address(expected_pool, "expected_pool", allow_none=False):
        errors.append("event_pool_mismatch")
    if expected_order_id is not None and event.order_id != expected_order_id:
        errors.append("event_order_id_mismatch")
    if expected_owner is not None and event.owner_address is not None and event.owner_address != _address(expected_owner, "expected_owner", allow_none=False):
        errors.append("event_owner_mismatch")
    if event.order_id is None and expected_event is not None:
        errors.append("event_order_id_unavailable")
    unique = _unique(errors)
    return LifecycleValidationResult(not unique, "valid" if not unique else "blocked", unique)


def validate_state_transition(current_state: str, target_state: str, *, external_signing_confirmed: bool = False, submission_evidence: DreamDexExternalSubmissionEvidence | None = None, receipt_evidence: DreamDexTransactionReceiptEvidence | None = None, event_evidence: Sequence[DreamDexTransactionEventEvidence] = (), incomplete_external_evidence: bool = False) -> LifecycleTransitionResult:
    allowed = {
        "prepared": {"externally_signed", "externally_submitted", "unknown_external_state"},
        "externally_signed": {"externally_submitted", "unknown_external_state"},
        "externally_submitted": {"pending_external_confirmation", "confirmed_reverted", "confirmed_missing_required_event", "replaced_external", "dropped_external", "unknown_external_state"},
        "pending_external_confirmation": {"confirmed_success", "confirmed_reverted", "confirmed_missing_required_event", "replaced_external", "dropped_external", "unknown_external_state"},
        "confirmed_missing_required_event": {"confirmed_success", "unknown_external_state"},
    }
    if current_state not in LIFECYCLE_STATES or target_state not in LIFECYCLE_STATES:
        return LifecycleTransitionResult(False, "blocked", ("state_unavailable",))
    if target_state == "unknown_external_state":
        if current_state in {"confirmed_success", "confirmed_reverted", "confirmed_missing_required_event", "replaced_external", "dropped_external"}:
            return LifecycleTransitionResult(False, "blocked", ("invalid_or_regressive_transition",))
        if incomplete_external_evidence or (submission_evidence is not None and (submission_evidence.conflicts or submission_evidence.unresolved_reasons)) or (receipt_evidence is not None and receipt_evidence.validation_errors):
            return LifecycleTransitionResult(True, "allowed", ())
        return LifecycleTransitionResult(False, "blocked", ("unknown_state_requires_incomplete_or_conflicting_evidence",))
    if target_state not in allowed.get(current_state, set()):
        return LifecycleTransitionResult(False, "blocked", ("invalid_or_regressive_transition",))
    if current_state == "prepared" and target_state == "externally_signed" and not external_signing_confirmed:
        return LifecycleTransitionResult(False, "blocked", ("external_signing_evidence_required",))
    if target_state == "externally_submitted" and submission_evidence is None:
        return LifecycleTransitionResult(False, "blocked", ("external_submission_evidence_required",))
    if target_state == "externally_submitted" and submission_evidence is not None and submission_evidence.transaction_hash is None:
        return LifecycleTransitionResult(False, "blocked", ("transaction_hash_required_for_submission",))
    if target_state == "externally_submitted" and submission_evidence is not None and (submission_evidence.source_type == "unavailable" or submission_evidence.submission_channel == "unavailable"):
        return LifecycleTransitionResult(False, "blocked", ("external_submission_source_unavailable",))
    if target_state in {"confirmed_success", "confirmed_reverted", "confirmed_missing_required_event"} and receipt_evidence is None:
        return LifecycleTransitionResult(False, "blocked", ("receipt_evidence_required",))
    if current_state == "confirmed_missing_required_event" and target_state == "confirmed_success" and not event_evidence:
        return LifecycleTransitionResult(False, "blocked", ("new_required_event_evidence_required",))
    return LifecycleTransitionResult(True, "allowed", ())


def transition_transaction_lifecycle(record: DreamDexTransactionLifecycleRecord, target_state: str, *, reason: str = "external_evidence", external_signing_confirmed: bool = False, submission_evidence: DreamDexExternalSubmissionEvidence | None = None, receipt_evidence: DreamDexTransactionReceiptEvidence | None = None, event_evidence: Sequence[DreamDexTransactionEventEvidence] = (), replacement_evidence: DreamDexTransactionReplacementEvidence | None = None, order_id: int | None = None, incomplete_external_evidence: bool = False) -> DreamDexTransactionLifecycleRecord:
    check = validate_state_transition(record.current_state, target_state, external_signing_confirmed=external_signing_confirmed, submission_evidence=submission_evidence, receipt_evidence=receipt_evidence, event_evidence=event_evidence, incomplete_external_evidence=incomplete_external_evidence)
    if not check.allowed:
        raise ValueError("lifecycle transition blocked: " + ";".join(check.errors))
    if target_state in {"pending_external_confirmation", "confirmed_success", "confirmed_reverted", "confirmed_missing_required_event", "replaced_external", "dropped_external"} and record.transaction_hash is None and submission_evidence is None:
        raise ValueError("lifecycle transition blocked: transaction_hash_required")
    tx_hash = record.transaction_hash
    if submission_evidence is not None:
        tx_hash = submission_evidence.transaction_hash
    replacement_hash = replacement_evidence.replacement_transaction_hash if replacement_evidence is not None else record.replacement_transaction_hash
    next_order_id = order_id if order_id is not None else (None if replacement_evidence is not None else record.order_id)
    blockers = list(record.blockers)
    if submission_evidence is not None or target_state in {"externally_submitted", "pending_external_confirmation", "confirmed_success", "confirmed_reverted", "confirmed_missing_required_event", "replaced_external", "dropped_external"}:
        blockers = [item for item in blockers if item != "transaction_submission_evidence_unavailable"]
    if receipt_evidence is not None or target_state in {"pending_external_confirmation", "confirmed_success", "confirmed_reverted", "confirmed_missing_required_event"}:
        blockers = [item for item in blockers if item != "transaction_receipt_evidence_unavailable"]
    if target_state == "confirmed_success":
        blockers = [item for item in blockers if item != "transaction_event_evidence_unavailable"]
    if target_state == "unknown_external_state":
        blockers.append("transaction_state_unknown_external")
    if target_state in {"confirmed_success", "confirmed_reverted", "confirmed_missing_required_event", "replaced_external", "dropped_external"}:
        blockers.append("transaction_lifecycle_non_authoritative")
    if target_state == "confirmed_missing_required_event":
        blockers.append("transaction_event_evidence_unavailable")
    if record.operation == "reduce_order" and target_state == "confirmed_success":
        blockers.append("reduce_event_semantics_unavailable")
    evidence = DreamDexTransactionLifecycleEvidence(source_type=(submission_evidence.source_type if submission_evidence else (receipt_evidence.evidence_source if receipt_evidence else record.evidence.source_type)), source_status="observed", transaction_hash_status="externally_supplied" if tx_hash else "unavailable", submission_status="externally_supplied" if submission_evidence else record.evidence.submission_status, receipt_status=receipt_evidence.status if receipt_evidence else record.evidence.receipt_status, receipt_success_status=receipt_evidence.status if receipt_evidence else record.evidence.receipt_success_status, block_number_status="externally_supplied" if receipt_evidence and receipt_evidence.block_number is not None else record.evidence.block_number_status, block_hash_status="externally_supplied" if receipt_evidence and receipt_evidence.block_hash is not None else record.evidence.block_hash_status, transaction_index_status="externally_supplied" if receipt_evidence and receipt_evidence.transaction_index is not None else record.evidence.transaction_index_status, from_address_status="externally_supplied" if receipt_evidence and receipt_evidence.from_address else record.evidence.from_address_status, to_address_status="externally_supplied" if receipt_evidence and receipt_evidence.to_address else record.evidence.to_address_status, chain_id_status="externally_supplied" if submission_evidence and submission_evidence.chain_id is not None else record.evidence.chain_id_status, event_status="observed" if event_evidence else record.evidence.event_status, replacement_status="observed" if replacement_evidence else record.evidence.replacement_status)
    return _make_record(envelope=None, lifecycle_id=record.lifecycle_id, operation=record.operation, transaction_hash=tx_hash, current_state=target_state, previous_state=record.current_state, reason=reason, receipt=receipt_evidence if receipt_evidence is not None else (None if replacement_evidence is not None else record.receipt_evidence), events=event_evidence if event_evidence else (() if replacement_evidence is not None else record.event_evidence), evidence=evidence, order_id=next_order_id, replacement_hash=replacement_hash, blockers=blockers, submission=submission_evidence or record.submission_evidence, replacement=replacement_evidence or record.replacement_evidence, request_fingerprint=record.request_fingerprint, envelope_fingerprint=record.envelope_fingerprint, add_envelope_blocker=False)


def apply_receipt_evidence(record: DreamDexTransactionLifecycleRecord, receipt: DreamDexTransactionReceiptEvidence, event_evidence: Sequence[DreamDexTransactionEventEvidence] = ()) -> DreamDexTransactionLifecycleRecord:
    expected_pool = record.submission_evidence.target_address if record.submission_evidence else None
    expected_from = record.submission_evidence.signer_address if record.submission_evidence else None
    receipt_check = validate_receipt_evidence(receipt, transaction_hash=record.transaction_hash, expected_pool=expected_pool, expected_from=expected_from)
    if not receipt_check.valid:
        return transition_transaction_lifecycle(record, "unknown_external_state", reason="receipt_evidence_conflict", receipt_evidence=receipt, incomplete_external_evidence=True)
    valid_events: list[DreamDexTransactionEventEvidence] = []
    event_errors: list[str] = []
    expected_owner = record.submission_evidence.signer_address if record.submission_evidence else None
    for event in event_evidence:
        check = validate_event_evidence(event, operation=record.operation, transaction_hash=record.transaction_hash, expected_pool=expected_pool, expected_owner=expected_owner)
        if check.valid:
            valid_events.append(event)
        else:
            event_errors.extend(check.errors)
    if receipt.status == "reverted":
        return transition_transaction_lifecycle(record, "confirmed_reverted", reason="receipt_reverted", receipt_evidence=receipt, event_evidence=valid_events)
    required_event = record.operation in {"place_order", "cancel_order"}
    if required_event and not valid_events:
        next_record = transition_transaction_lifecycle(record, "confirmed_missing_required_event", reason="receipt_success_without_required_event", receipt_evidence=receipt)
        if event_errors:
            return _make_record(envelope=None, lifecycle_id=next_record.lifecycle_id, operation=next_record.operation, transaction_hash=next_record.transaction_hash, current_state=next_record.current_state, previous_state=next_record.previous_state, reason=next_record.transition_reason, receipt=next_record.receipt_evidence, evidence=next_record.evidence, blockers=(*next_record.blockers, *event_errors), request_fingerprint=next_record.request_fingerprint, envelope_fingerprint=next_record.envelope_fingerprint, add_envelope_blocker=False, submission=next_record.submission_evidence)
        return next_record
    order_id = valid_events[0].order_id if valid_events else None
    confirmation_base = record
    if record.current_state == "externally_submitted":
        confirmation_base = transition_transaction_lifecycle(record, "pending_external_confirmation", reason="receipt_observed", receipt_evidence=receipt, event_evidence=valid_events)
    return transition_transaction_lifecycle(confirmation_base, "confirmed_success", reason="receipt_and_required_evidence", receipt_evidence=receipt, event_evidence=valid_events, order_id=order_id)


def apply_replacement_evidence(record: DreamDexTransactionLifecycleRecord, replacement: DreamDexTransactionReplacementEvidence) -> DreamDexTransactionLifecycleRecord:
    if record.transaction_hash != replacement.original_transaction_hash:
        raise ValueError("replacement_original_hash_mismatch")
    return transition_transaction_lifecycle(record, "replaced_external", reason="external_replacement_evidence", replacement_evidence=replacement)


def apply_dropped_state(record: DreamDexTransactionLifecycleRecord) -> DreamDexTransactionLifecycleRecord:
    return transition_transaction_lifecycle(record, "dropped_external", reason="external_drop_evidence")


def build_transaction_lifecycle_preview(record: DreamDexTransactionLifecycleRecord) -> DreamDexTransactionLifecyclePreview:
    event_status = "unavailable" if not record.event_evidence else ("confirmed" if all(not event.validation_errors for event in record.event_evidence) else "blocked")
    return DreamDexTransactionLifecyclePreview(record.operation, _masked_hash(record.transaction_hash), record.current_state, record.previous_state, record.request_fingerprint, record.envelope_fingerprint, record.receipt_evidence.status if record.receipt_evidence else "unavailable", event_status, "confirmed" if record.order_id is not None else "unavailable", record.evidence.replacement_status, record.lifecycle_fingerprint, False, record.reconciliation_status, record.blockers)


def serialize_transaction_lifecycle_diagnostics(record: DreamDexTransactionLifecycleRecord) -> dict[str, Any]:
    preview = build_transaction_lifecycle_preview(record)
    result = preview.safe_dict()
    result.update({
        "receipt_fingerprint": record.receipt_fingerprint,
        "event_fingerprint": record.event_fingerprint,
        "receipt_evidence": record.receipt_evidence.safe_dict() if record.receipt_evidence else None,
        "event_evidence": tuple(event.safe_dict() for event in record.event_evidence),
        "evidence": record.evidence.safe_dict(),
        "replacement_transaction_hash": _masked_hash(record.replacement_transaction_hash),
        "submission_evidence": record.submission_evidence.safe_dict() if record.submission_evidence else None,
    })
    return result


def describe_transaction_lifecycle_capabilities() -> Mapping[str, str]:
    return {
        "create_prepared_lifecycle": "available_offline",
        "import_external_submission": "available_offline",
        "validate_receipt_evidence": "available_offline",
        "validate_event_evidence": "available_offline",
        "validate_state_transition": "available_offline",
        "build_lifecycle_preview": "available_offline",
        "serialize_safe_diagnostics": "available_offline",
        "submit_transaction": "unavailable",
        "poll_transaction": "unavailable",
        "fetch_receipt": "unavailable",
        "fetch_logs": "unavailable",
        "detect_replacement_live": "unavailable",
        "wait_for_confirmations": "unavailable",
    }


__all__ = [
    "SCHEMA_VERSION", "LIFECYCLE_STATES", "SUBMISSION_CHANNELS", "REPLACEMENT_REASONS", "RECEIPT_STATUSES",
    "DreamDexTransactionLifecycleEvidence", "DreamDexTransactionReceiptEvidence", "DreamDexTransactionEventEvidence", "DreamDexTransactionLifecycleRecord", "DreamDexTransactionLifecyclePreview", "DreamDexExternalSubmissionEvidence", "DreamDexTransactionReplacementEvidence", "LifecycleValidationResult", "LifecycleTransitionResult",
    "compute_receipt_fingerprint", "compute_event_fingerprint", "compute_lifecycle_fingerprint", "create_prepared_lifecycle", "create_transaction_lifecycle", "validate_external_submission_evidence", "import_external_submission", "validate_receipt_evidence", "validate_event_evidence", "validate_state_transition", "transition_transaction_lifecycle", "apply_receipt_evidence", "apply_replacement_evidence", "apply_dropped_state", "build_transaction_lifecycle_preview", "serialize_transaction_lifecycle_diagnostics", "describe_transaction_lifecycle_capabilities",
]
