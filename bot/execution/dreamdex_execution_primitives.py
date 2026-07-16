"""Small, pure execution-layer primitives shared by offline diagnostics.

The existing request, envelope, lifecycle and reconciliation domains retain
their public APIs and fingerprint algorithms.  This module provides common
validation/status vocabulary for new integrations without changing those
domain fingerprints.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from hashlib import sha256
import json
import re
from typing import Any, Mapping, Sequence


class _StringEnum(str, Enum):
    def __str__(self) -> str:
        return self.value


class ExecutionAvailability(_StringEnum):
    AVAILABLE_OFFLINE = "available_offline"
    UNAVAILABLE = "unavailable"
    PARTIAL = "partial"
    SOURCE_CONFIRMED = "source_confirmed"
    available_offline = AVAILABLE_OFFLINE
    unavailable = UNAVAILABLE
    partial = PARTIAL
    source_confirmed = SOURCE_CONFIRMED


class EvidenceAuthority(_StringEnum):
    AUTHORITATIVE = "authoritative"
    NON_AUTHORITATIVE = "non_authoritative"
    authoritative = AUTHORITATIVE
    non_authoritative = NON_AUTHORITATIVE


class ReadinessStatus(_StringEnum):
    READY = "ready"
    BLOCKED = "blocked"
    UNAVAILABLE = "unavailable"
    ready = READY
    blocked = BLOCKED
    unavailable = UNAVAILABLE


class MatchStatus(_StringEnum):
    CONFIRMED = "confirmed"
    PARTIAL = "partial"
    MISMATCH = "mismatch"
    UNAVAILABLE = "unavailable"
    NOT_APPLICABLE = "not_applicable"
    confirmed = CONFIRMED
    partial = PARTIAL
    mismatch = MISMATCH
    unavailable = UNAVAILABLE
    not_applicable = NOT_APPLICABLE


class SourceType(_StringEnum):
    EXTERNAL_MANUAL = "external_manual"
    TEST_FIXTURE = "test_fixture"
    UNAVAILABLE = "unavailable"
    external_manual = EXTERNAL_MANUAL
    test_fixture = TEST_FIXTURE
    unavailable = UNAVAILABLE


CAPABILITY_STATUS_VALUES = frozenset(item.value for item in ExecutionAvailability)
AUTHORITY_STATUS_VALUES = frozenset(item.value for item in EvidenceAuthority)
READINESS_STATUS_VALUES = frozenset(item.value for item in ReadinessStatus)
MATCH_STATUS_VALUES = frozenset(item.value for item in MatchStatus)
SOURCE_TYPE_VALUES = frozenset(item.value for item in SourceType)


class DreamDexExecutionBlockers:
    """Canonical blocker names; informational statuses are not included."""

    INCOMPLETE_ACCOUNT_STATE = "incomplete_account_state"
    BALANCE_SOURCE_UNAVAILABLE = "balance_source_unavailable"
    AUTHENTICATED_ACCOUNT_STATE_UNAVAILABLE = "authenticated_account_state_unavailable"
    INCOMPLETE_OPEN_ORDERS_SOURCE = "incomplete_open_orders_source"
    INCOMPLETE_FILLS_SOURCE = "incomplete_fills_source"
    FAIR_PLAY_UNAVAILABLE = "fair_play_unavailable"
    RISK_UNAVAILABLE = "risk_unavailable"
    MARKET_STATUS_UNAVAILABLE = "market_status_unavailable"
    AUTHORITATIVE_ACCOUNT_ADDRESS_UNRESOLVED = "authoritative_account_address_unresolved"
    SMART_WALLET_OWNER_MAPPING_UNRESOLVED = "smart_wallet_owner_mapping_unresolved"
    DIRECT_SIGNER_KEY_UNAVAILABLE = "direct_signer_key_unavailable"
    DIRECT_SIGNER_BINDING_NON_AUTHORITATIVE = "direct_signer_binding_non_authoritative"
    TRANSACTION_SIGNER_UNAVAILABLE = "transaction_signer_unavailable"
    DIRECT_TRANSACTION_TRANSPORT_UNIMPLEMENTED = "direct_transaction_transport_unimplemented"
    TRANSACTION_SUBMISSION_EVIDENCE_UNAVAILABLE = "transaction_submission_evidence_unavailable"
    TRANSACTION_RECEIPT_EVIDENCE_UNAVAILABLE = "transaction_receipt_evidence_unavailable"
    TRANSACTION_EVENT_EVIDENCE_UNAVAILABLE = "transaction_event_evidence_unavailable"
    TRANSACTION_LIFECYCLE_NON_AUTHORITATIVE = "transaction_lifecycle_non_authoritative"
    TRANSACTION_REPLACEMENT_STATUS_UNAVAILABLE = "transaction_replacement_status_unavailable"
    ORDER_ID_LIFECYCLE_UNCONFIRMED = "order_id_lifecycle_unconfirmed"
    DIRECT_ORDER_RECONCILIATION_UNAVAILABLE = "direct_order_reconciliation_unavailable"
    RECONCILIATION_GRAPH_UNAVAILABLE = "reconciliation_graph_unavailable"
    RECONCILIATION_GRAPH_NON_AUTHORITATIVE = "reconciliation_graph_non_authoritative"
    ORDER_METADATA_UNAVAILABLE = "order_metadata_unavailable"
    AUTHENTICATED_ORDER_STATE_UNAVAILABLE = "authenticated_order_state_unavailable"
    AUTHENTICATED_PAGINATION_INCOMPLETE = "authenticated_pagination_incomplete"
    FILL_COVERAGE_UNAVAILABLE = "fill_coverage_unavailable"
    FILL_COVERAGE_INCOMPLETE = "fill_coverage_incomplete"
    REPLACEMENT_LINEAGE_UNRESOLVED = "replacement_lineage_unresolved"
    REORG_STATUS_UNRESOLVED = "reorg_status_unresolved"
    ACCOUNT_IDENTITY_CONFLICT = "account_identity_conflict"
    MARKET_IDENTITY_CONFLICT = "market_identity_conflict"
    REDUCE_EVENT_SEMANTICS_UNAVAILABLE = "reduce_event_semantics_unavailable"
    RECEIPT_LOOKUP_UNAVAILABLE = "receipt_lookup_unavailable"
    TRANSACTION_RECEIPT_NOT_FOUND = "transaction_receipt_not_found"
    TRANSACTION_RECEIPT_MALFORMED = "transaction_receipt_malformed"
    TRANSACTION_RECEIPT_HASH_MISMATCH = "transaction_receipt_hash_mismatch"
    TRANSACTION_RECEIPT_REVERTED = "transaction_receipt_reverted"
    CANONICAL_BLOCK_UNAVAILABLE = "canonical_block_unavailable"
    CANONICAL_BLOCK_HASH_MISMATCH = "canonical_block_hash_mismatch"
    CONFIRMATION_DEPTH_INSUFFICIENT = "confirmation_depth_insufficient"
    CONFIRMATION_POLICY_UNAVAILABLE = "confirmation_policy_unavailable"
    EXPECTED_CONTRACT_EVENT_MISSING = "expected_contract_event_missing"
    EXPECTED_CONTRACT_EVENT_CONFLICT = "expected_contract_event_conflict"
    ORDER_PLACED_EVENT_UNCONFIRMED = "order_placed_event_unconfirmed"
    ORDER_CANCELLED_EVENT_UNCONFIRMED = "order_cancelled_event_unconfirmed"
    ORDER_ID_CONFIRMATION_UNAVAILABLE = "order_id_confirmation_unavailable"
    ORDER_ID_MISMATCH = "order_id_mismatch"
    TRANSACTION_REORG_DETECTED = "transaction_reorg_detected"
    RECEIPT_OBSERVATION_UNSTABLE = "receipt_observation_unstable"
    CONFIRMATION_MONITOR_TIMEOUT = "confirmation_monitor_timeout"
    CONFIRMATION_PERSISTENCE_UNAVAILABLE = "confirmation_persistence_unavailable"
    TRANSACTION_ENVELOPE_UNAVAILABLE = "transaction_envelope_unavailable"
    TRANSACTION_TYPE_POLICY_UNRESOLVED = "transaction_type_policy_unresolved"
    TRANSACTION_NONCE_UNRESOLVED = "transaction_nonce_unresolved"
    TRANSACTION_GAS_UNRESOLVED = "transaction_gas_unresolved"
    TRANSACTION_FEES_UNRESOLVED = "transaction_fees_unresolved"
    TRANSACTION_SUBMISSION_UNAVAILABLE = "transaction_submission_unavailable"
    TRANSACTION_SIGNING_POLICY_UNAVAILABLE = "transaction_signing_policy_unavailable"
    TRANSACTION_SIGNER_IMPLEMENTATION_UNAVAILABLE = "transaction_signer_implementation_unavailable"
    TRANSACTION_SIGNING_REQUEST_UNAVAILABLE = "transaction_signing_request_unavailable"
    TRANSACTION_SIGNING_POLICY_REJECTED = "transaction_signing_policy_rejected"
    TRANSACTION_FEE_LIMIT_UNRESOLVED = "transaction_fee_limit_unresolved"
    TRANSACTION_VALUE_LIMIT_UNRESOLVED = "transaction_value_limit_unresolved"
    LIVE_TRANSACTION_PREFLIGHT_UNAVAILABLE = "live_transaction_preflight_unavailable"
    RPC_CONFIGURATION_UNAVAILABLE = "rpc_configuration_unavailable"
    RPC_CHAIN_MISMATCH = "rpc_chain_mismatch"
    TARGET_CONTRACT_CODE_UNAVAILABLE = "target_contract_code_unavailable"
    TARGET_CONTRACT_CODE_MISSING = "target_contract_code_missing"
    TARGET_CONTRACT_CODE_MALFORMED = "target_contract_code_malformed"
    PENDING_NONCE_UNAVAILABLE = "pending_nonce_unavailable"
    PENDING_NONCE_SNAPSHOT_NOT_RESERVED = "pending_nonce_snapshot_not_reserved"
    GAS_ESTIMATE_UNAVAILABLE = "gas_estimate_unavailable"
    GAS_ESTIMATE_REVERTED = "gas_estimate_reverted"
    GAS_LIMIT_POLICY_UNRESOLVED = "gas_limit_policy_unresolved"
    GAS_LIMIT_POLICY_EXCEEDED = "gas_limit_policy_exceeded"
    FEE_MODEL_UNRESOLVED = "fee_model_unresolved"
    FEE_EVIDENCE_UNAVAILABLE = "fee_evidence_unavailable"
    TRANSACTION_FEE_LIMIT_EXCEEDED = "transaction_fee_limit_exceeded"
    NATIVE_FEE_BALANCE_UNAVAILABLE = "native_fee_balance_unavailable"
    NATIVE_FEE_BALANCE_INSUFFICIENT = "native_fee_balance_insufficient"
    FINALIZED_ENVELOPE_UNAVAILABLE = "finalized_envelope_unavailable"
    PREFLIGHT_REQUIRES_NONCE_REVALIDATION = "preflight_requires_nonce_revalidation"
    EXECUTION_JOURNAL_UNAVAILABLE = "execution_journal_unavailable"
    EXECUTION_JOURNAL_PATH_UNAVAILABLE = "execution_journal_path_unavailable"
    EXECUTION_JOURNAL_SCHEMA_INCOMPATIBLE = "execution_journal_schema_incompatible"
    EXECUTION_JOURNAL_INTEGRITY_FAILED = "execution_journal_integrity_failed"
    EXECUTION_JOURNAL_RECOVERY_REQUIRED = "execution_journal_recovery_required"
    EXECUTION_INTENT_CONFLICT = "execution_intent_conflict"
    EXECUTION_INTENT_LIMIT_EXCEEDED = "execution_intent_limit_exceeded"
    NONCE_RESERVATION_UNAVAILABLE = "nonce_reservation_unavailable"
    NONCE_RESERVATION_CONFLICT = "nonce_reservation_conflict"
    NONCE_RESERVATION_LIMIT_EXCEEDED = "nonce_reservation_limit_exceeded"
    PENDING_NONCE_SNAPSHOT_REQUIRES_REVALIDATION = "pending_nonce_snapshot_requires_revalidation"
    EXTERNAL_NONCE_EXCLUSIVITY_UNAVAILABLE = "external_nonce_exclusivity_unavailable"
    UNKNOWN_EXECUTION_STATE_BLOCKS_PROGRESS = "unknown_execution_state_blocks_progress"
    EXECUTION_JOURNAL_LIMITS_UNRESOLVED = "execution_journal_limits_unresolved"
    LIVE_NONCE_REVALIDATION_UNAVAILABLE = "live_nonce_revalidation_unavailable"
    LIVE_NONCE_CHAIN_MISMATCH = "live_nonce_chain_mismatch"
    LIVE_NONCE_MISMATCH = "live_nonce_mismatch"
    LIVE_NONCE_OBSERVATION_STALE = "live_nonce_observation_stale"
    LIVE_NONCE_SOURCE_UNAVAILABLE = "live_nonce_source_unavailable"
    SIGNING_LEASE_UNAVAILABLE = "signing_lease_unavailable"
    SIGNING_LEASE_CONFLICT = "signing_lease_conflict"
    SIGNING_LEASE_POLICY_UNRESOLVED = "signing_lease_policy_unresolved"
    SIGNING_LEASE_INPUT_TYPE_INVALID = "signing_lease_input_type_invalid"
    SIGNING_LEASE_INTENT_INVALID = "signing_lease_intent_invalid"
    SIGNING_LEASE_RESERVATION_INVALID = "signing_lease_reservation_invalid"
    SIGNING_LEASE_ENVELOPE_MISMATCH = "signing_lease_envelope_mismatch"
    SIGNING_LEASE_REQUEST_MISMATCH = "signing_lease_request_mismatch"
    SIGNING_LEASE_REQUEST_NOT_READY = "signing_lease_request_not_ready"
    BOUND_TRANSACTION_SIGNER_UNAVAILABLE = "bound_transaction_signer_unavailable"
    SIGNED_TRANSACTION_DECODER_UNAVAILABLE = "signed_transaction_decoder_unavailable"
    SIGNED_TRANSACTION_MALFORMED = "signed_transaction_malformed"
    SIGNED_TRANSACTION_SENDER_MISMATCH = "signed_transaction_sender_mismatch"
    SIGNED_TRANSACTION_CHAIN_MISMATCH = "signed_transaction_chain_mismatch"
    SIGNED_TRANSACTION_NONCE_MISMATCH = "signed_transaction_nonce_mismatch"
    SIGNED_TRANSACTION_TARGET_MISMATCH = "signed_transaction_target_mismatch"
    SIGNED_TRANSACTION_VALUE_MISMATCH = "signed_transaction_value_mismatch"
    SIGNED_TRANSACTION_GAS_MISMATCH = "signed_transaction_gas_mismatch"
    SIGNED_TRANSACTION_FEE_MISMATCH = "signed_transaction_fee_mismatch"
    SIGNED_TRANSACTION_CALLDATA_MISMATCH = "signed_transaction_calldata_mismatch"
    SIGNED_TRANSACTION_SELECTOR_MISMATCH = "signed_transaction_selector_mismatch"
    SIGNED_TRANSACTION_FINGERPRINT_MISMATCH = "signed_transaction_fingerprint_mismatch"
    SIGNED_TRANSACTION_VERIFICATION_FAILED = "signed_transaction_verification_failed"
    SIGNED_PAYLOAD_NOT_DURABLY_AVAILABLE = "signed_payload_not_durably_available"
    SIGNED_TRANSACTION_SUBMISSION_UNAVAILABLE = "signed_transaction_submission_unavailable"
    RAW_TRANSACTION_SUBMISSION_UNAVAILABLE = "raw_transaction_submission_unavailable"
    SUBMISSION_POLICY_UNAVAILABLE = "submission_policy_unavailable"
    SUBMISSION_RECORD_UNAVAILABLE = "submission_record_unavailable"
    SUBMISSION_PRECONDITION_FAILED = "submission_precondition_failed"
    SUBMISSION_ATTEMPT_ALREADY_STARTED = "submission_attempt_already_started"
    SUBMISSION_TRANSPORT_UNAVAILABLE = "submission_transport_unavailable"
    SUBMISSION_RESPONSE_UNAVAILABLE = "submission_response_unavailable"
    SUBMISSION_HASH_MISMATCH = "submission_hash_mismatch"
    SUBMISSION_OUTCOME_UNKNOWN = "submission_outcome_unknown"
    SUBMISSION_RECOVERY_REQUIRED = "submission_recovery_required"
    SUBMISSION_TRANSACTION_NOT_FOUND = "submission_transaction_not_found"
    SUBMISSION_TRANSACTION_FIELD_MISMATCH = "submission_transaction_field_mismatch"
    AUTOMATIC_SUBMISSION_RETRY_DISABLED = "automatic_submission_retry_disabled"
    TRANSACTION_REPLACEMENT_DISABLED = "transaction_replacement_disabled"
    RECEIPT_LOOKUP_UNAVAILABLE = "receipt_lookup_unavailable"
    SUBMISSION_DETERMINISTIC_REJECTION = "submission_deterministic_rejection"

    ACCOUNT = (
        INCOMPLETE_ACCOUNT_STATE, BALANCE_SOURCE_UNAVAILABLE,
        AUTHENTICATED_ACCOUNT_STATE_UNAVAILABLE,
        AUTHORITATIVE_ACCOUNT_ADDRESS_UNRESOLVED,
        SMART_WALLET_OWNER_MAPPING_UNRESOLVED, DIRECT_SIGNER_KEY_UNAVAILABLE,
        DIRECT_SIGNER_BINDING_NON_AUTHORITATIVE,
    )
    TRANSACTION = (
        TRANSACTION_SIGNER_UNAVAILABLE, DIRECT_TRANSACTION_TRANSPORT_UNIMPLEMENTED,
        TRANSACTION_SUBMISSION_EVIDENCE_UNAVAILABLE, TRANSACTION_RECEIPT_EVIDENCE_UNAVAILABLE,
        TRANSACTION_EVENT_EVIDENCE_UNAVAILABLE, TRANSACTION_LIFECYCLE_NON_AUTHORITATIVE,
        TRANSACTION_REPLACEMENT_STATUS_UNAVAILABLE, TRANSACTION_ENVELOPE_UNAVAILABLE,
        TRANSACTION_TYPE_POLICY_UNRESOLVED, TRANSACTION_NONCE_UNRESOLVED,
        TRANSACTION_GAS_UNRESOLVED, TRANSACTION_FEES_UNRESOLVED,
        TRANSACTION_SUBMISSION_UNAVAILABLE, TRANSACTION_SIGNING_POLICY_UNAVAILABLE,
        TRANSACTION_SIGNER_IMPLEMENTATION_UNAVAILABLE, TRANSACTION_SIGNING_REQUEST_UNAVAILABLE,
        TRANSACTION_SIGNING_POLICY_REJECTED, TRANSACTION_FEE_LIMIT_UNRESOLVED,
        TRANSACTION_VALUE_LIMIT_UNRESOLVED, LIVE_TRANSACTION_PREFLIGHT_UNAVAILABLE,
        RPC_CONFIGURATION_UNAVAILABLE, RPC_CHAIN_MISMATCH, TARGET_CONTRACT_CODE_UNAVAILABLE,
        TARGET_CONTRACT_CODE_MISSING, TARGET_CONTRACT_CODE_MALFORMED, PENDING_NONCE_UNAVAILABLE, PENDING_NONCE_SNAPSHOT_NOT_RESERVED,
        GAS_ESTIMATE_UNAVAILABLE, GAS_ESTIMATE_REVERTED, GAS_LIMIT_POLICY_UNRESOLVED,
        GAS_LIMIT_POLICY_EXCEEDED, FEE_MODEL_UNRESOLVED, FEE_EVIDENCE_UNAVAILABLE,
        TRANSACTION_FEE_LIMIT_EXCEEDED, NATIVE_FEE_BALANCE_UNAVAILABLE,
        NATIVE_FEE_BALANCE_INSUFFICIENT, FINALIZED_ENVELOPE_UNAVAILABLE,
        PREFLIGHT_REQUIRES_NONCE_REVALIDATION,
        EXECUTION_JOURNAL_UNAVAILABLE, EXECUTION_JOURNAL_PATH_UNAVAILABLE,
        EXECUTION_JOURNAL_SCHEMA_INCOMPATIBLE, EXECUTION_JOURNAL_INTEGRITY_FAILED,
        EXECUTION_JOURNAL_RECOVERY_REQUIRED, EXECUTION_INTENT_CONFLICT,
        EXECUTION_INTENT_LIMIT_EXCEEDED, NONCE_RESERVATION_UNAVAILABLE,
        NONCE_RESERVATION_CONFLICT, NONCE_RESERVATION_LIMIT_EXCEEDED,
        PENDING_NONCE_SNAPSHOT_REQUIRES_REVALIDATION,
        EXTERNAL_NONCE_EXCLUSIVITY_UNAVAILABLE,
        UNKNOWN_EXECUTION_STATE_BLOCKS_PROGRESS,
        EXECUTION_JOURNAL_LIMITS_UNRESOLVED,
        LIVE_NONCE_REVALIDATION_UNAVAILABLE, LIVE_NONCE_CHAIN_MISMATCH,
        LIVE_NONCE_MISMATCH, LIVE_NONCE_OBSERVATION_STALE,
        LIVE_NONCE_SOURCE_UNAVAILABLE, SIGNING_LEASE_UNAVAILABLE,
        SIGNING_LEASE_CONFLICT, SIGNING_LEASE_POLICY_UNRESOLVED,
        SIGNING_LEASE_INPUT_TYPE_INVALID, SIGNING_LEASE_INTENT_INVALID,
        SIGNING_LEASE_RESERVATION_INVALID, SIGNING_LEASE_ENVELOPE_MISMATCH,
        SIGNING_LEASE_REQUEST_MISMATCH, SIGNING_LEASE_REQUEST_NOT_READY,
        BOUND_TRANSACTION_SIGNER_UNAVAILABLE, SIGNED_TRANSACTION_DECODER_UNAVAILABLE,
        SIGNED_TRANSACTION_MALFORMED, SIGNED_TRANSACTION_SENDER_MISMATCH,
        SIGNED_TRANSACTION_CHAIN_MISMATCH, SIGNED_TRANSACTION_NONCE_MISMATCH,
        SIGNED_TRANSACTION_TARGET_MISMATCH, SIGNED_TRANSACTION_VALUE_MISMATCH,
        SIGNED_TRANSACTION_GAS_MISMATCH, SIGNED_TRANSACTION_FEE_MISMATCH,
        SIGNED_TRANSACTION_CALLDATA_MISMATCH, SIGNED_TRANSACTION_SELECTOR_MISMATCH,
        SIGNED_TRANSACTION_FINGERPRINT_MISMATCH, SIGNED_TRANSACTION_VERIFICATION_FAILED,
        SIGNED_PAYLOAD_NOT_DURABLY_AVAILABLE, SIGNED_TRANSACTION_SUBMISSION_UNAVAILABLE,
        RAW_TRANSACTION_SUBMISSION_UNAVAILABLE, SUBMISSION_POLICY_UNAVAILABLE,
        SUBMISSION_RECORD_UNAVAILABLE, SUBMISSION_PRECONDITION_FAILED,
        SUBMISSION_ATTEMPT_ALREADY_STARTED, SUBMISSION_TRANSPORT_UNAVAILABLE,
        SUBMISSION_RESPONSE_UNAVAILABLE, SUBMISSION_HASH_MISMATCH,
        SUBMISSION_OUTCOME_UNKNOWN, SUBMISSION_RECOVERY_REQUIRED,
        SUBMISSION_TRANSACTION_NOT_FOUND, SUBMISSION_TRANSACTION_FIELD_MISMATCH,
        AUTOMATIC_SUBMISSION_RETRY_DISABLED, TRANSACTION_REPLACEMENT_DISABLED,
        RECEIPT_LOOKUP_UNAVAILABLE, TRANSACTION_RECEIPT_NOT_FOUND,
        TRANSACTION_RECEIPT_MALFORMED, TRANSACTION_RECEIPT_HASH_MISMATCH,
        TRANSACTION_RECEIPT_REVERTED, CANONICAL_BLOCK_UNAVAILABLE,
        CANONICAL_BLOCK_HASH_MISMATCH, CONFIRMATION_DEPTH_INSUFFICIENT,
        CONFIRMATION_POLICY_UNAVAILABLE, EXPECTED_CONTRACT_EVENT_MISSING,
        EXPECTED_CONTRACT_EVENT_CONFLICT, ORDER_PLACED_EVENT_UNCONFIRMED,
        ORDER_CANCELLED_EVENT_UNCONFIRMED, ORDER_ID_CONFIRMATION_UNAVAILABLE,
        ORDER_ID_MISMATCH, TRANSACTION_REORG_DETECTED,
        RECEIPT_OBSERVATION_UNSTABLE, CONFIRMATION_MONITOR_TIMEOUT,
        CONFIRMATION_PERSISTENCE_UNAVAILABLE,
        SUBMISSION_DETERMINISTIC_REJECTION,
    )
    ORDER_LIFECYCLE = (ORDER_ID_LIFECYCLE_UNCONFIRMED, DIRECT_ORDER_RECONCILIATION_UNAVAILABLE)
    RECONCILIATION = (
        RECONCILIATION_GRAPH_UNAVAILABLE, RECONCILIATION_GRAPH_NON_AUTHORITATIVE,
        ORDER_METADATA_UNAVAILABLE, AUTHENTICATED_ORDER_STATE_UNAVAILABLE,
        AUTHENTICATED_PAGINATION_INCOMPLETE, FILL_COVERAGE_UNAVAILABLE,
        FILL_COVERAGE_INCOMPLETE, REPLACEMENT_LINEAGE_UNRESOLVED,
        REORG_STATUS_UNRESOLVED, ACCOUNT_IDENTITY_CONFLICT, MARKET_IDENTITY_CONFLICT,
        REDUCE_EVENT_SEMANTICS_UNAVAILABLE,
        INCOMPLETE_OPEN_ORDERS_SOURCE, INCOMPLETE_FILLS_SOURCE,
        FAIR_PLAY_UNAVAILABLE, RISK_UNAVAILABLE, MARKET_STATUS_UNAVAILABLE,
    )
    ALL = tuple(dict.fromkeys((*ACCOUNT, *TRANSACTION, *ORDER_LIFECYCLE, *RECONCILIATION)))
    PRODUCTION_DEFAULT_ACTIVE = (
        INCOMPLETE_ACCOUNT_STATE, AUTHORITATIVE_ACCOUNT_ADDRESS_UNRESOLVED,
        DIRECT_SIGNER_KEY_UNAVAILABLE, DIRECT_SIGNER_BINDING_NON_AUTHORITATIVE,
        TRANSACTION_SIGNER_UNAVAILABLE, DIRECT_TRANSACTION_TRANSPORT_UNIMPLEMENTED,
        RAW_TRANSACTION_SUBMISSION_UNAVAILABLE,
        DIRECT_ORDER_RECONCILIATION_UNAVAILABLE, ORDER_ID_LIFECYCLE_UNCONFIRMED,
        RECONCILIATION_GRAPH_UNAVAILABLE, RECONCILIATION_GRAPH_NON_AUTHORITATIVE,
        ORDER_METADATA_UNAVAILABLE, AUTHENTICATED_ORDER_STATE_UNAVAILABLE,
        AUTHENTICATED_PAGINATION_INCOMPLETE, FILL_COVERAGE_UNAVAILABLE,
        FILL_COVERAGE_INCOMPLETE, REPLACEMENT_LINEAGE_UNRESOLVED,
        REORG_STATUS_UNRESOLVED,
    )

    @classmethod
    def normalize(cls, values: Sequence[str], *, strict: bool = True) -> tuple[str, ...]:
        unknown = sorted({str(value) for value in values if str(value) not in cls.ALL})
        if unknown and strict:
            raise ValueError("unknown execution blocker: " + ",".join(unknown))
        return tuple(value for value in cls.ALL if value in values)


_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_HASH_RE = re.compile(r"^0x[0-9a-fA-F]{64}$", re.IGNORECASE)


def mask_evm_address(value: Any) -> str:
    if not value:
        return "<missing>"
    text = str(value)
    return text if text.startswith("<") else (text[:4] + "..." + text[-4:] if len(text) > 8 else "***")


def mask_hex_hash(value: Any) -> str:
    if not value:
        return "<missing>"
    text = str(value)
    return text if text.startswith("<") else (text[:6] + "..." + text[-4:] if len(text) > 10 else "***")


def validate_evm_address(value: Any, *, field: str = "address", allow_none: bool = False) -> str | None:
    if value is None and allow_none:
        return None
    if not isinstance(value, str) or not _ADDRESS_RE.fullmatch(value):
        raise ValueError(f"{field}: invalid_address")
    return value.lower()


def validate_tx_hash(value: Any, *, field: str = "transaction_hash", allow_none: bool = False) -> str | None:
    if value is None and allow_none:
        return None
    if not isinstance(value, str) or not _HASH_RE.fullmatch(value) or value.lower() == "0x" + "0" * 64:
        raise ValueError(f"{field}: invalid_hash")
    return value.lower()


def validate_uint(value: Any, *, field: str = "value", maximum: int = (1 << 256) - 1, allow_none: bool = False) -> int | None:
    if value is None and allow_none:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > maximum:
        raise ValueError(f"{field}: invalid_nonnegative_integer")
    return value


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str).encode("utf-8")


def sha256_hex(value: bytes | str) -> str:
    raw = value.encode("utf-8") if isinstance(value, str) else bytes(value)
    return sha256(raw).hexdigest()


def deterministic_fingerprint(value: Any, *, domain: str | None = None, schema_version: str = "1") -> str:
    payload = {"schema_version": schema_version, "domain": domain, "payload": value} if domain else value
    return sha256_hex(canonical_json_bytes(payload))


def safe_sorted_tuple(values: Sequence[Any]) -> tuple[Any, ...]:
    return tuple(sorted(dict.fromkeys(values), key=lambda item: str(item)))


_SENSITIVE_KEYS = (
    "calldata", "receipt_json", "raw_topics", "raw_data", "private_key", "seed", "mnemonic",
    "signature", "signed_transaction", "bearer", "cookie", "authorization", "auth_header",
    "token", "nonce", "order_id", "tx_hash", "balance", "price", "quantity", "timestamp",
    "stdout", "stderr", "message",
)


def _is_raw_sensitive_key(name: str) -> bool:
    if name.endswith(("_status", "_count", "_complete", "_present", "_fingerprint", "_masked", "_integrity")):
        return False
    return name in _SENSITIVE_KEYS or any(name.endswith("_" + token) for token in _SENSITIVE_KEYS)


def ensure_no_raw_sensitive_fields(value: Any) -> Any:
    """Reject raw sensitive fields in a diagnostics payload and return it."""
    def walk(item: Any, path: str = "") -> None:
        if isinstance(item, Mapping):
            for key, child in item.items():
                name = str(key).lower()
                if _is_raw_sensitive_key(name):
                    raise ValueError("raw_sensitive_field:" + (path + "." if path else "") + str(key))
                if name in {"address", "owner_address", "from_address", "to_address", "account_address", "market_address", "pool_address", "transaction_hash", "block_hash"} and isinstance(child, str):
                    if name.endswith("hash") and _HASH_RE.fullmatch(child):
                        raise ValueError("full_hash_in_diagnostics")
                    if name.endswith("address") and _ADDRESS_RE.fullmatch(child):
                        raise ValueError("full_address_in_diagnostics")
                walk(child, path + "." + str(key) if path else str(key))
        elif isinstance(item, (tuple, list)):
            for child in item:
                walk(child, path)
    walk(value)
    return value


@dataclass(frozen=True)
class DreamDexExecutionCapability:
    name: str
    status: str
    layer: str
    source_status: str = "unavailable"
    authoritative: bool = False
    blocking: bool = False
    unresolved_reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        status = str(self.status)
        if status not in CAPABILITY_STATUS_VALUES:
            raise ValueError("capability_status: unsupported")
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "unresolved_reasons", safe_sorted_tuple(tuple(str(item) for item in self.unresolved_reasons)))

    def safe_dict(self) -> dict[str, Any]:
        return {"name": self.name, "status": self.status, "layer": self.layer, "source_status": self.source_status, "authoritative": bool(self.authoritative), "blocking": bool(self.blocking), "unresolved_reasons": self.unresolved_reasons}


@dataclass(frozen=True)
class DreamDexExecutionCapabilityMatrix:
    capabilities: tuple[DreamDexExecutionCapability, ...]
    blockers: tuple[str, ...] = ()
    fingerprint: str = ""

    def __post_init__(self) -> None:
        ordered = tuple(sorted(self.capabilities, key=lambda item: item.name))
        if len({item.name for item in ordered}) != len(ordered):
            raise ValueError("duplicate_capability_name")
        object.__setattr__(self, "capabilities", ordered)
        object.__setattr__(self, "blockers", DreamDexExecutionBlockers.normalize(self.blockers))
        if not self.fingerprint:
            object.__setattr__(self, "fingerprint", deterministic_fingerprint({"capabilities": [item.safe_dict() for item in ordered], "blockers": self.blockers}, domain="dreamdex/capabilities/v1"))

    def by_name(self, name: str) -> DreamDexExecutionCapability:
        for item in self.capabilities:
            if item.name == name:
                return item
        return DreamDexExecutionCapability(name, "unavailable", "unknown")

    def safe_dict(self) -> dict[str, Any]:
        return {"capabilities": tuple(item.safe_dict() for item in self.capabilities), "blockers": self.blockers, "fingerprint": self.fingerprint}


def build_execution_capability_matrix(*, blockers: Sequence[str] = ()) -> DreamDexExecutionCapabilityMatrix:
    available = {
        "build_unsigned_place": "unsigned_request", "build_unsigned_cancel": "unsigned_request", "build_unsigned_reduce": "unsigned_request",
        "validate_unsigned_request": "unsigned_request", "build_unsigned_envelope": "envelope", "validate_unsigned_envelope": "envelope",
        "create_prepared_lifecycle": "lifecycle", "import_external_submission": "lifecycle", "validate_receipt_evidence": "receipt",
        "validate_event_evidence": "receipt", "build_reconciliation_graph": "reconciliation", "validate_reconciliation_graph": "reconciliation",
        "serialize_safe_diagnostics": "reconciliation", "build_evidence_inventory": "reconciliation",
        "adapt_authenticated_orders": "reconciliation", "adapt_order_metadata": "reconciliation",
        "adapt_onchain_fills": "reconciliation", "build_evidence_bundle": "reconciliation",
        "build_graphs_from_bundle": "reconciliation", "build_bridge_preview": "reconciliation",
        "serialize_bridge_diagnostics": "reconciliation",
        "validate_signing_policy": "signing", "build_signing_request": "signing",
        "validate_signing_request": "signing", "build_signing_preview": "signing",
        "transaction_signer_protocol": "signing",
        "readonly_rpc_protocol": "rpc", "validate_rpc_response": "rpc",
        "finalize_transaction_envelope": "preflight", "build_transaction_preflight_preview": "preflight",
        "serialize_transaction_preflight_diagnostics": "preflight",
        "execution_journal_model": "journal", "initialize_execution_journal": "journal",
        "validate_execution_journal_schema": "journal", "verify_execution_journal_integrity": "journal",
        "create_execution_intent": "journal", "idempotent_intent_lookup": "journal",
        "reserve_nonce_locally": "journal", "enforce_nonce_uniqueness": "journal",
        "validate_state_transition": "journal",
        "signing_lease_model": "signing_lease", "validate_live_nonce_evidence": "signing_lease",
        "validate_signing_lease": "signing_lease", "signing_material_model": "signed_transaction",
        "bound_transaction_signer_protocol": "signed_transaction", "verify_signed_transaction_fields": "signed_transaction",
        "verify_signed_transaction_calldata": "signed_transaction", "calculate_signed_transaction_hash": "signed_transaction",
        "journal_signing_started_transition": "journal", "journal_signed_transition": "journal",
        "raw_transaction_submission_model": "submission", "raw_transaction_submitter_protocol": "submission",
        "validate_submission_preconditions": "submission", "persist_submission_started": "submission",
        "enforce_single_submission_attempt": "submission", "verify_rpc_transaction_hash": "submission",
        "classify_submission_outcome": "submission",
        "transaction_receipt_model": "receipt", "transaction_receipt_reader_protocol": "receipt",
        "normalize_transaction_receipt": "receipt", "validate_canonical_block": "receipt", "calculate_confirmation_depth": "receipt",
        "validate_order_placed_event": "receipt", "validate_order_cancelled_event": "receipt", "detect_transaction_reorg": "receipt",
        "persist_transaction_confirmation": "journal",
    }
    unavailable = {
        "resolve_nonce": "envelope", "estimate_gas": "envelope", "resolve_fees": "envelope", "sign_transaction": "signing",
        "serialize_signed_transaction": "signing", "submit_transaction": "submission", "fetch_receipt": "receipt", "fetch_logs": "receipt",
        "wait_for_confirmations": "receipt", "fetch_authenticated_orders": "authentication", "fetch_fills_live": "authentication",
        "fetch_order_metadata_live": "authentication", "fetch_onchain_fills_live": "authentication",
        "fetch_lifecycle_live": "authentication", "resolve_identity_live": "authentication",
        "signer_address_discovery": "signing",
        "production_bound_signer": "signed_transaction", "persist_raw_signed_transaction": "signed_transaction",
        "production_raw_transaction_submitter": "submission", "automatic_submission_retry": "submission",
        "transaction_replacement": "submission", "receipt_lookup": "receipt", "validate_reduce_event": "receipt",
        "revalidate_nonce_live": "journal", "externally_lock_nonce": "journal",
    }
    partial = {
        "resolve_pending_nonce": "preflight", "estimate_transaction_gas": "preflight",
        "detect_fee_model": "preflight", "resolve_transaction_fees": "preflight",
        "check_native_fee_balance": "preflight",
        "recover_execution_state": "journal",
        "revalidate_pending_nonce_live": "signing_lease", "acquire_signing_lease": "signing_lease",
        "decode_signed_transaction": "signed_transaction", "recover_signed_transaction_sender": "signed_transaction",
        "recover_submission_by_hash": "submission",
        "observe_transaction_confirmation_once": "receipt", "monitor_transaction_confirmation": "receipt", "receipt_polling": "receipt",
    }
    values = [DreamDexExecutionCapability(name, ExecutionAvailability.AVAILABLE_OFFLINE.value, layer, source_status="offline", blocking=False) for name, layer in available.items()]
    values.extend(DreamDexExecutionCapability(name, ExecutionAvailability.PARTIAL.value, layer, source_status="opt_in_runtime", blocking=False, unresolved_reasons=("runtime_evidence_required",)) for name, layer in partial.items())
    values.extend(DreamDexExecutionCapability(name, ExecutionAvailability.UNAVAILABLE.value, layer, source_status="unavailable", blocking=True, unresolved_reasons=("capability_unavailable",)) for name, layer in unavailable.items())
    return DreamDexExecutionCapabilityMatrix(tuple(values), tuple(blockers))


@dataclass(frozen=True)
class DreamDexExecutionArchitectureAuditFinding:
    duplicate: str
    source_files: tuple[str, ...]
    semantic_equivalence: str
    consolidation_candidate: str
    risk: str
    action: str


AUDIT_FINDINGS = (
    DreamDexExecutionArchitectureAuditFinding("address/hash validators and masking", ("dreamdex_unsigned_transaction.py", "dreamdex_transaction_envelope.py", "dreamdex_transaction_lifecycle.py", "dreamdex_order_reconciliation.py"), "same safety intent; domain error wording differs", "shared helpers for new code only", "changing existing exceptions or masks would break compatibility", "keep separate; re-export shared primitives"),
    DreamDexExecutionArchitectureAuditFinding("canonical JSON/SHA-256 helpers", ("dreamdex_unsigned_transaction.py", "dreamdex_transaction_envelope.py", "dreamdex_transaction_lifecycle.py", "dreamdex_order_reconciliation.py"), "same low-level algorithm, different domain payloads", "common canonicalization only", "changing domain payloads changes regression fingerprints", "keep domain fingerprints separate"),
    DreamDexExecutionArchitectureAuditFinding("capability status strings", ("dreamdex_unsigned_transaction.py", "dreamdex_transaction_envelope.py", "dreamdex_transaction_lifecycle.py", "dreamdex_order_reconciliation.py"), "external string values are equivalent", "typed vocabulary/matrix", "enum coercion must preserve serialized strings", "consolidate new matrix; preserve old APIs"),
    DreamDexExecutionArchitectureAuditFinding("blocker literals", ("dreamdex_transaction_envelope.py", "scripts/check_live_read_only_state.py", "dreamdex_order_reconciliation.py"), "overlapping names plus domain-specific blockers", "canonical registry for diagnostics", "removing legacy literals would change CLI gates", "consolidate registry without rewriting old paths"),
)


def build_execution_architecture_audit_report() -> tuple[DreamDexExecutionArchitectureAuditFinding, ...]:
    return AUDIT_FINDINGS


# Short aliases make the vocabulary convenient for offline callers while the
# descriptive names remain the canonical public API.
ExecutionCapability = DreamDexExecutionCapability
CapabilityMatrix = DreamDexExecutionCapabilityMatrix
build_capability_matrix = build_execution_capability_matrix
run_execution_architecture_audit = build_execution_architecture_audit_report


__all__ = [
    "ExecutionAvailability", "EvidenceAuthority", "ReadinessStatus", "MatchStatus", "SourceType",
    "CAPABILITY_STATUS_VALUES", "DreamDexExecutionBlockers", "mask_evm_address", "mask_hex_hash",
    "validate_evm_address", "validate_tx_hash", "validate_uint", "canonical_json_bytes", "sha256_hex",
    "deterministic_fingerprint", "safe_sorted_tuple", "ensure_no_raw_sensitive_fields",
    "DreamDexExecutionCapability", "DreamDexExecutionCapabilityMatrix", "build_execution_capability_matrix",
    "DreamDexExecutionArchitectureAuditFinding", "AUDIT_FINDINGS", "build_execution_architecture_audit_report",
    "ExecutionCapability", "CapabilityMatrix", "build_capability_matrix", "run_execution_architecture_audit",
]
