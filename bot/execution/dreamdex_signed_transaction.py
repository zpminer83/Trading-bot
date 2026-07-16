"""In-memory transaction-signing handoff and independent verification.

The production boundary in this module intentionally stops before any network
submission.  A signer may return an ephemeral byte payload, which is decoded
and verified in memory and then discarded by the signing session.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from typing import Any, Protocol, runtime_checkable

from eth_account import Account
from eth_account.typed_transactions import TypedTransaction
from eth_utils import keccak
from hexbytes import HexBytes
import rlp

from bot.execution.dreamdex_execution_journal import (
    DreamDexExecutionIntent,
    DreamDexExecutionIntentResult,
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
from bot.execution.dreamdex_signing_lease import DreamDexSigningLease
from bot.execution.dreamdex_transaction_envelope import DreamDexUnsignedTransactionEnvelope, validate_unsigned_transaction_envelope
from bot.execution.dreamdex_transaction_signer import (
    DreamDexTransactionSignerCapabilities,
    DreamDexTransactionSigningPolicy,
    DreamDexTransactionSigningRequest,
    validate_transaction_signing_policy,
    validate_transaction_signing_request,
)

SCHEMA_VERSION = "1"
MAX_SIGNED_PAYLOAD_BYTES = 1_048_576
SUPPORTED_SIGNED_TYPES = frozenset({"legacy", "eip1559"})
DECODE_STATUSES = frozenset({"decoded", "invalid", "unavailable"})
SIGNATURE_STATUSES = frozenset({"verified", "unavailable"})


def _tuple(values: Any) -> tuple[str, ...]:
    return tuple(dict.fromkeys(str(value) for value in (values or ()) if str(value)))


def _address(value: Any, field: str) -> str:
    return validate_evm_address(value, field=field)  # type: ignore[return-value]


def _mask(value: Any) -> str:
    if not value:
        return "<missing>"
    return str(value) if str(value).startswith("<") else mask_hex_hash(value)


def _selector(data: bytes) -> str | None:
    return "0x" + data[:4].hex() if len(data) >= 4 else None


def _int_from_rlp(value: bytes, field: str) -> int:
    if not isinstance(value, bytes):
        raise ValueError("signed_transaction_malformed")
    if len(value) > 1 and value[0] == 0:
        raise ValueError("signed_transaction_malformed")
    result = int.from_bytes(value, "big") if value else 0
    validate_uint(result, field=field)
    return result


def _bytes_from_value(value: Any) -> bytes:
    if isinstance(value, bytearray):
        value = bytes(value)
    if isinstance(value, memoryview):
        value = value.tobytes()
    if not isinstance(value, bytes):
        raise ValueError("signed_transaction_payload_type_invalid")
    if not value:
        raise ValueError("signed_transaction_empty")
    if len(value) > MAX_SIGNED_PAYLOAD_BYTES:
        raise ValueError("signed_transaction_oversized")
    return bytes(value)


class DreamDexSignedTransactionDecodeError(ValueError):
    """Safe decode failure; it intentionally contains no payload details."""

    def __init__(self, category: str):
        self.category = str(category)
        super().__init__(self.category)


@dataclass(frozen=True, repr=False)
class DreamDexTransactionSigningMaterial:
    schema_version: str
    intent_id: str
    reservation_id: str
    lease_id: str
    operation: str
    finalized_envelope: DreamDexUnsignedTransactionEnvelope
    signing_request_fingerprint: str
    lease_fingerprint: str
    material_fingerprint: str
    policy_approved: bool
    lease_active: bool
    authoritative: bool
    blockers: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError("signed_transaction_schema_version_invalid")
        if not isinstance(self.finalized_envelope, DreamDexUnsignedTransactionEnvelope):
            raise TypeError("finalized_envelope_type_invalid")
        object.__setattr__(self, "blockers", _tuple(self.blockers))
        object.__setattr__(self, "authoritative", False)

    def safe_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "intent_id": _mask(self.intent_id),
            "reservation_id": _mask(self.reservation_id),
            "lease_id": _mask(self.lease_id),
            "operation": self.operation,
            "signing_request_fingerprint": _mask(self.signing_request_fingerprint),
            "lease_fingerprint": _mask(self.lease_fingerprint),
            "material_fingerprint": _mask(self.material_fingerprint),
            "policy_approved": self.policy_approved,
            "lease_active": self.lease_active,
            "authoritative": False,
            "raw_envelope_output_allowed": False,
            "blockers": self.blockers,
        }

    def __repr__(self) -> str:
        return f"DreamDexTransactionSigningMaterial(intent_id={_mask(self.intent_id)!r}, lease_id={_mask(self.lease_id)!r}, operation={self.operation!r}, lease_active={self.lease_active!r}, authoritative=False)"


@dataclass(frozen=True, repr=False)
class DreamDexEphemeralSignedTransaction:
    raw_signed_transaction: bytes
    signer_reported_address: str
    signing_request_fingerprint: str
    lease_fingerprint: str
    source_type: str = "test_fixture"

    def __post_init__(self) -> None:
        object.__setattr__(self, "raw_signed_transaction", _bytes_from_value(self.raw_signed_transaction))
        object.__setattr__(self, "signer_reported_address", _address(self.signer_reported_address, "signer_reported_address"))
        if not isinstance(self.signing_request_fingerprint, str) or not self.signing_request_fingerprint:
            raise ValueError("signed_transaction_request_fingerprint_invalid")
        if not isinstance(self.lease_fingerprint, str) or not self.lease_fingerprint:
            raise ValueError("signed_transaction_lease_fingerprint_invalid")
        if not isinstance(self.source_type, str) or not self.source_type:
            raise ValueError("signed_transaction_source_type_invalid")

    def __repr__(self) -> str:
        return f"DreamDexEphemeralSignedTransaction(payload_present=True, payload_length={len(self.raw_signed_transaction)}, signer={mask_evm_address(self.signer_reported_address)!r})"

    def __str__(self) -> str:
        return "DreamDexEphemeralSignedTransaction(payload=redacted)"

    def __copy__(self):
        raise TypeError("ephemeral_signed_transaction_copy_forbidden")

    def __deepcopy__(self, memo):
        raise TypeError("ephemeral_signed_transaction_copy_forbidden")

    def __reduce__(self):
        raise TypeError("ephemeral_signed_transaction_serialization_forbidden")

    def __reduce_ex__(self, protocol):
        raise TypeError("ephemeral_signed_transaction_serialization_forbidden")


@dataclass(frozen=True, repr=False)
class DreamDexDecodedSignedTransaction:
    transaction_type: str
    chain_id: int | None
    recovered_sender: str | None
    nonce: int | None
    target_address: str | None
    value_wei: int | None
    gas_limit: int | None
    gas_price_wei: int | None
    max_fee_per_gas_wei: int | None
    max_priority_fee_per_gas_wei: int | None
    calldata_sha256: str | None
    calldata_length: int | None
    selector: str | None
    signed_transaction_hash: str | None
    signed_payload_length: int
    decoding_status: str
    authoritative: bool
    blockers: tuple[str, ...] = ()
    validation_errors: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.transaction_type not in SUPPORTED_SIGNED_TYPES:
            raise ValueError("signed_transaction_type_invalid")
        if self.chain_id is not None:
            validate_uint(self.chain_id, field="chain_id")
        if self.recovered_sender is not None:
            object.__setattr__(self, "recovered_sender", _address(self.recovered_sender, "recovered_sender"))
        if self.target_address is not None:
            object.__setattr__(self, "target_address", _address(self.target_address, "target_address"))
        if self.decoding_status not in DECODE_STATUSES:
            raise ValueError("signed_transaction_decoding_status_invalid")
        object.__setattr__(self, "blockers", _tuple(self.blockers))
        object.__setattr__(self, "validation_errors", _tuple(self.validation_errors))

    def safe_dict(self) -> dict[str, Any]:
        return {
            "transaction_type": self.transaction_type,
            "chain_id": self.chain_id,
            "recovered_sender_masked": mask_evm_address(self.recovered_sender),
            "nonce": self.nonce,
            "target_address_masked": mask_evm_address(self.target_address),
            "value_wei": self.value_wei,
            "gas_limit": self.gas_limit,
            "gas_price_wei": self.gas_price_wei,
            "max_fee_per_gas_wei": self.max_fee_per_gas_wei,
            "max_priority_fee_per_gas_wei": self.max_priority_fee_per_gas_wei,
            "calldata_sha256": mask_hex_hash(self.calldata_sha256),
            "calldata_length": self.calldata_length,
            "selector": self.selector,
            "signed_transaction_hash": mask_hex_hash(self.signed_transaction_hash),
            "signed_payload_length": self.signed_payload_length,
            "decoding_status": self.decoding_status,
            "authoritative": self.authoritative,
            "raw_calldata_output_allowed": False,
            "blockers": self.blockers,
            "validation_errors": self.validation_errors,
        }

    def __repr__(self) -> str:
        return f"DreamDexDecodedSignedTransaction(type={self.transaction_type!r}, chain_id={self.chain_id!r}, sender={mask_evm_address(self.recovered_sender)!r}, hash={mask_hex_hash(self.signed_transaction_hash)!r}, status={self.decoding_status!r}, authoritative={self.authoritative!r})"


@dataclass(frozen=True, repr=False)
class DreamDexSignedTransactionVerificationResult:
    schema_version: str
    signing_request_fingerprint_match: bool
    lease_fingerprint_match: bool
    signer_report_match: bool
    recovered_sender_match: bool
    chain_match: bool
    nonce_match: bool
    target_match: bool
    value_match: bool
    gas_match: bool
    fee_model_match: bool
    fee_fields_match: bool
    calldata_hash_match: bool
    calldata_length_match: bool
    selector_match: bool
    operation_selector_match: bool
    transaction_hash_available: bool
    verified: bool
    ready_for_journal_signed_transition: bool
    ready_for_submission: bool
    verification_fingerprint: str
    authoritative: bool
    blockers: tuple[str, ...] = ()
    validation_errors: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError("signed_transaction_schema_version_invalid")
        object.__setattr__(self, "ready_for_submission", False)
        object.__setattr__(self, "ready_for_journal_signed_transition", bool(self.ready_for_journal_signed_transition and self.verified))
        object.__setattr__(self, "authoritative", bool(self.authoritative and self.verified))
        object.__setattr__(self, "blockers", _tuple(self.blockers))
        object.__setattr__(self, "validation_errors", _tuple(self.validation_errors))

    def safe_dict(self) -> dict[str, Any]:
        return {name: (mask_hex_hash(getattr(self, name)) if name == "verification_fingerprint" else getattr(self, name)) for name in self.__dataclass_fields__ if name not in {"schema_version"}}

    def __repr__(self) -> str:
        return f"DreamDexSignedTransactionVerificationResult(verified={self.verified!r}, ready_for_submission=False, blockers={self.blockers!r})"


@dataclass(frozen=True, repr=False)
class DreamDexVerifiedSignedTransactionArtifact:
    schema_version: str
    intent_id: str
    lease_id: str
    signer_address: str
    transaction_type: str
    chain_id: int
    nonce: int
    target_address: str
    selector: str
    signed_transaction_hash: str
    signed_payload_length: int
    signing_request_fingerprint: str
    lease_fingerprint: str
    verification_fingerprint: str
    signature_status: str
    source_type: str
    authoritative: bool
    ready_for_submission: bool
    blockers: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION or self.transaction_type not in SUPPORTED_SIGNED_TYPES:
            raise ValueError("signed_artifact_schema_or_type_invalid")
        object.__setattr__(self, "signer_address", _address(self.signer_address, "signer_address"))
        object.__setattr__(self, "target_address", _address(self.target_address, "target_address"))
        validate_uint(self.chain_id, field="chain_id")
        validate_uint(self.nonce, field="nonce")
        if self.signature_status not in SIGNATURE_STATUSES:
            raise ValueError("signed_artifact_signature_status_invalid")
        object.__setattr__(self, "ready_for_submission", False)
        object.__setattr__(self, "blockers", _tuple(self.blockers))

    def safe_dict(self) -> dict[str, Any]:
        return {"schema_version": self.schema_version, "intent_id": _mask(self.intent_id), "lease_id": _mask(self.lease_id), "signer_address_masked": mask_evm_address(self.signer_address), "transaction_type": self.transaction_type, "chain_id": self.chain_id, "nonce": self.nonce, "target_address_masked": mask_evm_address(self.target_address), "selector": self.selector, "signed_transaction_hash": mask_hex_hash(self.signed_transaction_hash), "signed_payload_length": self.signed_payload_length, "signing_request_fingerprint": _mask(self.signing_request_fingerprint), "lease_fingerprint": _mask(self.lease_fingerprint), "verification_fingerprint": _mask(self.verification_fingerprint), "signature_status": self.signature_status, "source_type": self.source_type, "authoritative": self.authoritative, "ready_for_submission": False, "raw_signed_transaction_output_allowed": False, "raw_signature_output_allowed": False, "blockers": self.blockers}

    def __repr__(self) -> str:
        return f"DreamDexVerifiedSignedTransactionArtifact(intent_id={_mask(self.intent_id)!r}, hash={mask_hex_hash(self.signed_transaction_hash)!r}, signature_status={self.signature_status!r}, ready_for_submission=False)"


@dataclass(frozen=True, repr=False)
class DreamDexSignedTransactionPreview:
    signing_session_status: str
    signer_invocation_performed: bool
    signed_payload_received: bool
    signed_payload_persisted: bool
    transaction_decoded: bool
    sender_recovered: bool
    sender_match: bool
    chain_match: bool
    nonce_match: bool
    target_match: bool
    selector_match: bool
    value_match: bool
    gas_match: bool
    fee_match: bool
    calldata_match: bool
    transaction_hash_status: str
    journal_state: str
    signed_artifact_available: bool
    raw_signed_transaction_output_allowed: bool
    ready_for_submission: bool
    blockers: tuple[str, ...] = ()

    def safe_dict(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}

    def __repr__(self) -> str:
        return f"DreamDexSignedTransactionPreview(status={self.signing_session_status!r}, decoded={self.transaction_decoded!r}, ready_for_submission=False)"


@dataclass(frozen=True, repr=False)
class DreamDexSigningMaterialValidationResult:
    valid: bool
    blockers: tuple[str, ...] = ()
    validation_errors: tuple[str, ...] = ()

    @property
    def approved(self) -> bool:
        return self.valid

    def __post_init__(self) -> None:
        object.__setattr__(self, "blockers", _tuple(self.blockers))
        object.__setattr__(self, "validation_errors", _tuple(self.validation_errors))

    def safe_dict(self) -> dict[str, Any]:
        return {"valid": self.valid, "approved": self.valid, "blockers": self.blockers, "validation_errors": self.validation_errors}

    def __repr__(self) -> str:
        return f"DreamDexSigningMaterialValidationResult(valid={self.valid!r}, blockers={self.blockers!r})"


@dataclass(frozen=True, repr=False)
class DreamDexTransactionSigningSessionResult:
    status: str
    intent_id: str | None
    lease_id: str | None
    signer_invocation_performed: bool
    verification: DreamDexSignedTransactionVerificationResult | None
    artifact: DreamDexVerifiedSignedTransactionArtifact | None
    journal_state: str
    blockers: tuple[str, ...] = ()
    validation_errors: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "blockers", _tuple(self.blockers))
        object.__setattr__(self, "validation_errors", _tuple(self.validation_errors))

    def safe_dict(self) -> dict[str, Any]:
        return {"status": self.status, "intent_id": _mask(self.intent_id), "lease_id": _mask(self.lease_id), "signer_invocation_performed": self.signer_invocation_performed, "verification": self.verification.safe_dict() if self.verification else None, "artifact": self.artifact.safe_dict() if self.artifact else None, "journal_state": self.journal_state, "blockers": self.blockers, "validation_errors": self.validation_errors, "raw_signed_transaction_output_allowed": False}

    def __repr__(self) -> str:
        return f"DreamDexTransactionSigningSessionResult(status={self.status!r}, signer_invocation_performed={self.signer_invocation_performed!r}, journal_state={self.journal_state!r})"


@runtime_checkable
class DreamDexBoundTransactionSigner(Protocol):
    def get_address(self) -> str: ...
    def describe_capabilities(self) -> DreamDexTransactionSignerCapabilities: ...
    def sign_finalized_transaction(self, material: DreamDexTransactionSigningMaterial) -> DreamDexEphemeralSignedTransaction: ...


class UnavailableDreamDexBoundTransactionSigner:
    def get_address(self) -> str:
        return "<unavailable>"

    def describe_capabilities(self) -> DreamDexTransactionSignerCapabilities:
        return DreamDexTransactionSignerCapabilities()

    def sign_finalized_transaction(self, material: DreamDexTransactionSigningMaterial) -> DreamDexEphemeralSignedTransaction:
        raise RuntimeError("bound_transaction_signer_unavailable")


def build_transaction_signing_material(*, journal: DreamDexExecutionJournal, intent: DreamDexExecutionIntent, reservation: DreamDexNonceReservation, finalized_envelope: DreamDexUnsignedTransactionEnvelope, signing_request: DreamDexTransactionSigningRequest, lease: DreamDexSigningLease, signing_policy: DreamDexTransactionSigningPolicy | None = None) -> DreamDexTransactionSigningMaterial:
    values = (journal, intent, reservation, finalized_envelope, signing_request, lease)
    if any(isinstance(value, dict) for value in values) or not all(isinstance(value, expected) for value, expected in zip(values, (DreamDexExecutionJournal, DreamDexExecutionIntent, DreamDexNonceReservation, DreamDexUnsignedTransactionEnvelope, DreamDexTransactionSigningRequest, DreamDexSigningLease))):
        raise TypeError("signing_material_typed_inputs_required")
    blockers: list[str] = []
    if intent.state != JournalState.SIGNING_LEASE_ACQUIRED.value or lease.lease_status != "acquired" or not lease.nonce_match:
        blockers.append("signing_lease_intent_invalid")
    if reservation.reservation_status != "reserved" or reservation.intent_id != intent.intent_id:
        blockers.append("signing_lease_reservation_invalid")
    if lease.intent_id != intent.intent_id or lease.reservation_id != reservation.reservation_id or lease.lease_id != lease.journal_event_id:
        blockers.append("signing_lease_binding_mismatch")
    if finalized_envelope.envelope_fingerprint != intent.finalized_envelope_fingerprint or signing_request.signing_request_fingerprint != intent.signing_request_fingerprint:
        blockers.append("signing_transaction_fingerprint_mismatch")
    structural = validate_unsigned_transaction_envelope(finalized_envelope)
    if structural.errors:
        blockers.extend(structural.errors)
    if signing_policy is not None:
        policy_result = validate_transaction_signing_policy(finalized_envelope, signing_policy, signer_address=intent.signer_address)
        request_result = validate_transaction_signing_request(signing_request, envelope=finalized_envelope, policy=signing_policy)
        if not signing_request.policy_approved or not policy_result.approved or not request_result.approved or not signing_request.ready_for_signer_invocation:
            blockers.append("transaction_signing_policy_rejected")
    if blockers:
        raise ValueError("signing_material_invalid:" + ",".join(dict.fromkeys(blockers)))
    material_fp = deterministic_fingerprint({"intent_id": intent.intent_id, "reservation_id": reservation.reservation_id, "lease_id": lease.lease_id, "operation": intent.operation, "signing_request_fingerprint": signing_request.signing_request_fingerprint, "lease_fingerprint": lease.lease_fingerprint}, domain="dreamdex_signing_material")
    return DreamDexTransactionSigningMaterial(SCHEMA_VERSION, intent.intent_id, reservation.reservation_id, lease.lease_id, intent.operation, finalized_envelope, signing_request.signing_request_fingerprint, lease.lease_fingerprint, material_fp, True, True, False, ())


def validate_transaction_signing_material(material: Any, *, journal: DreamDexExecutionJournal | None = None, intent: DreamDexExecutionIntent | None = None, reservation: DreamDexNonceReservation | None = None, signing_request: DreamDexTransactionSigningRequest | None = None) -> DreamDexSigningMaterialValidationResult:
    if not isinstance(material, DreamDexTransactionSigningMaterial):
        return DreamDexSigningMaterialValidationResult(False, ("signing_material_input_type_invalid",), ("typed_inputs_required",))
    blockers = list(material.blockers)
    if not material.policy_approved or not material.lease_active:
        blockers.append("signing_material_not_approved")
    if intent is not None and (not isinstance(intent, DreamDexExecutionIntent) or intent.intent_id != material.intent_id or intent.state != JournalState.SIGNING_LEASE_ACQUIRED.value):
        blockers.append("signing_lease_intent_invalid")
    if reservation is not None and (not isinstance(reservation, DreamDexNonceReservation) or reservation.reservation_id != material.reservation_id or reservation.reservation_status != "reserved"):
        blockers.append("signing_lease_reservation_invalid")
    if signing_request is not None and (not isinstance(signing_request, DreamDexTransactionSigningRequest) or signing_request.signing_request_fingerprint != material.signing_request_fingerprint):
        blockers.append("signing_transaction_fingerprint_mismatch")
    return DreamDexSigningMaterialValidationResult(not blockers, tuple(dict.fromkeys(blockers)))


def _decode_legacy(raw: bytes) -> dict[str, Any]:
    try:
        fields = rlp.decode(raw)
    except Exception as exc:
        raise DreamDexSignedTransactionDecodeError("signed_transaction_malformed") from None
    if not isinstance(fields, list) or len(fields) != 9 or any(not isinstance(item, bytes) for item in fields):
        raise DreamDexSignedTransactionDecodeError("signed_transaction_malformed")
    nonce, gas_price, gas, to, value, data, v, r, s = fields
    if len(to) not in {0, 20} or not data:
        # Empty calldata is valid for a generic transaction; the envelope
        # verifier will reject it for the DreamDEX operation selector.
        if len(to) not in {0, 20}:
            raise DreamDexSignedTransactionDecodeError("signed_transaction_malformed")
    v_int = _int_from_rlp(v, "v")
    if v_int in {27, 28}:
        chain_id = None
    elif v_int >= 35:
        chain_id = (v_int - 35) // 2
    else:
        raise DreamDexSignedTransactionDecodeError("signed_transaction_malformed")
    if len(r) == 0 or len(s) == 0:
        raise DreamDexSignedTransactionDecodeError("signed_transaction_malformed")
    return {"transaction_type": "legacy", "chain_id": chain_id, "nonce": _int_from_rlp(nonce, "nonce"), "target_address": "0x" + to.hex() if to else None, "value_wei": _int_from_rlp(value, "value"), "gas_limit": _int_from_rlp(gas, "gas_limit"), "gas_price_wei": _int_from_rlp(gas_price, "gas_price"), "max_fee_per_gas_wei": None, "max_priority_fee_per_gas_wei": None, "data": data}


def _decode_typed(raw: bytes) -> dict[str, Any]:
    try:
        typed = TypedTransaction.from_bytes(HexBytes(raw))
        values = typed.as_dict()
    except Exception:
        raise DreamDexSignedTransactionDecodeError("signed_transaction_malformed") from None
    if values.get("type") != 2:
        raise DreamDexSignedTransactionDecodeError("signed_transaction_unsupported_type")
    to = bytes(values.get("to") or b"")
    data = bytes(values.get("data") or b"")
    max_fee = values.get("maxFeePerGas")
    max_priority = values.get("maxPriorityFeePerGas")
    if not isinstance(max_fee, int) or not isinstance(max_priority, int) or max_fee < max_priority:
        raise DreamDexSignedTransactionDecodeError("signed_transaction_fee_invalid")
    return {"transaction_type": "eip1559", "chain_id": values.get("chainId"), "nonce": values.get("nonce"), "target_address": "0x" + to.hex() if to else None, "value_wei": values.get("value"), "gas_limit": values.get("gas"), "gas_price_wei": None, "max_fee_per_gas_wei": max_fee, "max_priority_fee_per_gas_wei": max_priority, "data": data}


def decode_signed_transaction(raw_signed_transaction: bytes | bytearray | memoryview) -> DreamDexDecodedSignedTransaction:
    raw = _bytes_from_value(raw_signed_transaction)
    try:
        if raw[0] < 0x80 and raw[0] != 2:
            raise DreamDexSignedTransactionDecodeError("signed_transaction_unsupported_type")
        fields = _decode_typed(raw) if raw[0] < 0x80 else _decode_legacy(raw)
        recovered = _address(Account.recover_transaction(HexBytes(raw)), "recovered_sender")
        data = bytes(fields.pop("data"))
        digest = sha256(data).hexdigest()
        return DreamDexDecodedSignedTransaction(fields["transaction_type"], fields["chain_id"], recovered, fields["nonce"], fields["target_address"], fields["value_wei"], fields["gas_limit"], fields["gas_price_wei"], fields["max_fee_per_gas_wei"], fields["max_priority_fee_per_gas_wei"], digest, len(data), _selector(data), "0x" + keccak(raw).hex(), len(raw), "decoded", True)
    except DreamDexSignedTransactionDecodeError:
        raise
    except Exception:
        raise DreamDexSignedTransactionDecodeError("signed_transaction_sender_recovery_failed") from None


def _result_fingerprint(values: dict[str, Any]) -> str:
    return deterministic_fingerprint(values, domain="dreamdex_signed_transaction_verification")


def verify_signed_transaction(material: DreamDexTransactionSigningMaterial, signed_transaction: DreamDexEphemeralSignedTransaction, decoded: DreamDexDecodedSignedTransaction | None = None) -> DreamDexSignedTransactionVerificationResult:
    if not isinstance(material, DreamDexTransactionSigningMaterial) or not isinstance(signed_transaction, DreamDexEphemeralSignedTransaction):
        return DreamDexSignedTransactionVerificationResult(SCHEMA_VERSION, *([False] * 17), False, False, _result_fingerprint({"status": "typed_input_invalid"}), False, ("signed_transaction_input_type_invalid",), ("typed_inputs_required",))
    try:
        decoded = decoded or decode_signed_transaction(signed_transaction.raw_signed_transaction)
    except DreamDexSignedTransactionDecodeError as exc:
        return _verification_failure(material, signed_transaction, str(exc))
    envelope = material.finalized_envelope
    signer_report = signed_transaction.signer_reported_address.lower() == (envelope.from_address or "").lower()
    recovered = decoded.recovered_sender is not None and decoded.recovered_sender.lower() == (envelope.from_address or "").lower()
    chain = decoded.chain_id == envelope.chain_id
    nonce = decoded.nonce == envelope.nonce
    target = decoded.target_address is not None and decoded.target_address.lower() == (envelope.to_address or "").lower()
    value = decoded.value_wei == envelope.value_wei
    gas = decoded.gas_limit == envelope.gas_limit
    fee_model = decoded.transaction_type == envelope.transaction_type
    if envelope.transaction_type == "legacy":
        fee_fields = decoded.gas_price_wei == envelope.gas_price_wei and decoded.max_fee_per_gas_wei is None and decoded.max_priority_fee_per_gas_wei is None
    else:
        fee_fields = decoded.gas_price_wei is None and decoded.max_fee_per_gas_wei == envelope.max_fee_per_gas_wei and decoded.max_priority_fee_per_gas_wei == envelope.max_priority_fee_per_gas_wei and decoded.max_fee_per_gas_wei is not None and decoded.max_priority_fee_per_gas_wei is not None and decoded.max_fee_per_gas_wei >= decoded.max_priority_fee_per_gas_wei
    cal_hash = decoded.calldata_sha256 == envelope.calldata_sha256
    cal_len = decoded.calldata_length == envelope.calldata_length
    selector = decoded.selector == _selector(bytes(envelope.calldata or b""))
    expected_selector = {"place_order": "0x4e978373", "cancel_order": "0xdbc91396", "reduce_order": "0x33407b60"}.get(envelope.operation)
    operation_selector = decoded.selector == expected_selector
    request_fp = signed_transaction.signing_request_fingerprint == material.signing_request_fingerprint
    lease_fp = signed_transaction.lease_fingerprint == material.lease_fingerprint
    hash_available = bool(decoded.signed_transaction_hash)
    flags = (request_fp, lease_fp, signer_report, recovered, chain, nonce, target, value, gas, fee_model, fee_fields, cal_hash, cal_len, selector, operation_selector, hash_available)
    blockers: list[str] = []
    names = ("signed_transaction_fingerprint_mismatch", "signed_transaction_fingerprint_mismatch", "signed_transaction_sender_mismatch", "signed_transaction_sender_mismatch", "signed_transaction_chain_mismatch", "signed_transaction_nonce_mismatch", "signed_transaction_target_mismatch", "signed_transaction_value_mismatch", "signed_transaction_gas_mismatch", "signed_transaction_fee_mismatch", "signed_transaction_fee_mismatch", "signed_transaction_calldata_mismatch", "signed_transaction_calldata_mismatch", "signed_transaction_selector_mismatch", "signed_transaction_selector_mismatch", "signed_transaction_decoder_unavailable")
    blockers.extend(name for flag, name in zip(flags, names) if not flag)
    verified = bool(all(flags) and material.policy_approved and material.lease_active)
    if not material.policy_approved or not material.lease_active:
        blockers.append("signing_material_not_approved")
    blockers = list(dict.fromkeys(blockers))
    fp = _result_fingerprint({"flags": flags, "verified": verified, "hash": decoded.signed_transaction_hash if hash_available else None, "blockers": blockers})
    return DreamDexSignedTransactionVerificationResult(SCHEMA_VERSION, request_fp, lease_fp, signer_report, recovered, chain, nonce, target, value, gas, fee_model, fee_fields, cal_hash, cal_len, selector, operation_selector, hash_available, verified, verified, False, fp, verified, tuple(blockers), ())


def _verification_failure(material: DreamDexTransactionSigningMaterial, signed: DreamDexEphemeralSignedTransaction, category: str) -> DreamDexSignedTransactionVerificationResult:
    fp = _result_fingerprint({"category": category, "intent_id": material.intent_id})
    return DreamDexSignedTransactionVerificationResult(SCHEMA_VERSION, False, False, False, False, False, False, False, False, False, False, False, False, False, False, False, False, False, False, False, fp, False, ("signed_transaction_verification_failed",), (category,))


def begin_transaction_signing(*, journal: DreamDexExecutionJournal, material: DreamDexTransactionSigningMaterial) -> DreamDexExecutionIntentResult:
    validation = validate_transaction_signing_material(material)
    if not validation.valid:
        return DreamDexExecutionIntentResult(None, False, False, validation.blockers, validation.validation_errors)
    return journal.begin_transaction_signing(intent_id=material.intent_id, lease_id=material.lease_id, chain_id=material.finalized_envelope.chain_id, signer_address=material.finalized_envelope.from_address, finalized_envelope_fingerprint=material.finalized_envelope.envelope_fingerprint, signing_request_fingerprint=material.signing_request_fingerprint)


def finalize_verified_signing(*, journal: DreamDexExecutionJournal, material: DreamDexTransactionSigningMaterial, verification: DreamDexSignedTransactionVerificationResult) -> DreamDexExecutionIntentResult:
    if not verification.verified:
        return DreamDexExecutionIntentResult(None, False, False, verification.blockers, verification.validation_errors)
    return journal.finalize_signed_transaction(intent_id=material.intent_id, lease_id=material.lease_id, chain_id=material.finalized_envelope.chain_id, signer_address=material.finalized_envelope.from_address, finalized_envelope_fingerprint=material.finalized_envelope.envelope_fingerprint, signing_request_fingerprint=material.signing_request_fingerprint)


def _artifact(material: DreamDexTransactionSigningMaterial, decoded: DreamDexDecodedSignedTransaction, verification: DreamDexSignedTransactionVerificationResult, source_type: str) -> DreamDexVerifiedSignedTransactionArtifact:
    return DreamDexVerifiedSignedTransactionArtifact(SCHEMA_VERSION, material.intent_id, material.lease_id, decoded.recovered_sender or material.finalized_envelope.from_address, decoded.transaction_type, decoded.chain_id, decoded.nonce, decoded.target_address, decoded.selector, decoded.signed_transaction_hash, decoded.signed_payload_length, material.signing_request_fingerprint, material.lease_fingerprint, verification.verification_fingerprint, "verified", source_type, True, False, ("signed_payload_not_durably_available", "signed_transaction_submission_unavailable"))


def run_transaction_signing_session(*, journal: DreamDexExecutionJournal, material: DreamDexTransactionSigningMaterial, signer: DreamDexBoundTransactionSigner) -> DreamDexTransactionSigningSessionResult:
    validation = validate_transaction_signing_material(material)
    if not validation.valid:
        return DreamDexTransactionSigningSessionResult("blocked", material.intent_id if isinstance(material, DreamDexTransactionSigningMaterial) else None, material.lease_id if isinstance(material, DreamDexTransactionSigningMaterial) else None, False, None, None, JournalState.SIGNING_LEASE_ACQUIRED.value, validation.blockers, validation.validation_errors)
    if not isinstance(signer, DreamDexBoundTransactionSigner):
        return DreamDexTransactionSigningSessionResult("blocked", material.intent_id, material.lease_id, False, None, None, JournalState.SIGNING_LEASE_ACQUIRED.value, ("bound_transaction_signer_unavailable",), ("signer_type_invalid",))
    begun = begin_transaction_signing(journal=journal, material=material)
    if not begun.intent or begun.intent.state != JournalState.SIGNING_STARTED.value or begun.blockers or begun.validation_errors:
        return DreamDexTransactionSigningSessionResult("blocked", material.intent_id, material.lease_id, False, None, None, JournalState.SIGNING_LEASE_ACQUIRED.value, begun.blockers, begun.validation_errors)
    signed: DreamDexEphemeralSignedTransaction | None = None
    try:
        try:
            signed = signer.sign_finalized_transaction(material)
            if not isinstance(signed, DreamDexEphemeralSignedTransaction):
                raise ValueError("signed_transaction_payload_type_invalid")
        except Exception:
            journal.mark_signing_recovery_required(intent_id=material.intent_id, reason="bound_signer_invocation_failed")
            return DreamDexTransactionSigningSessionResult("recovery_required", material.intent_id, material.lease_id, True, None, None, JournalState.RECOVERY_REQUIRED.value, ("bound_transaction_signer_invocation_failed",), ())
        try:
            decoded = decode_signed_transaction(signed.raw_signed_transaction)
            verification = verify_signed_transaction(material, signed, decoded)
        except DreamDexSignedTransactionDecodeError as exc:
            journal.mark_signing_recovery_required(intent_id=material.intent_id, reason="signed_transaction_verification_failed")
            verification = _verification_failure(material, signed, str(exc))
            return DreamDexTransactionSigningSessionResult("recovery_required", material.intent_id, material.lease_id, True, verification, None, JournalState.RECOVERY_REQUIRED.value, verification.blockers, verification.validation_errors)
        except Exception:
            journal.mark_signing_recovery_required(intent_id=material.intent_id, reason="signed_transaction_verification_failed")
            return DreamDexTransactionSigningSessionResult("recovery_required", material.intent_id, material.lease_id, True, None, None, JournalState.RECOVERY_REQUIRED.value, ("signed_transaction_verification_failed",), ())
        if not verification.verified:
            journal.mark_signing_recovery_required(intent_id=material.intent_id, reason="signed_transaction_verification_failed")
            return DreamDexTransactionSigningSessionResult("recovery_required", material.intent_id, material.lease_id, True, verification, None, JournalState.RECOVERY_REQUIRED.value, verification.blockers, verification.validation_errors)
        artifact = _artifact(material, decoded, verification, signed.source_type)
        finalized = finalize_verified_signing(journal=journal, material=material, verification=verification)
        if not finalized.intent:
            journal.mark_signing_recovery_required(intent_id=material.intent_id, reason="signed_transition_failed")
            return DreamDexTransactionSigningSessionResult("recovery_required", material.intent_id, material.lease_id, True, verification, artifact, JournalState.RECOVERY_REQUIRED.value, ("signed_transition_failed",), finalized.validation_errors)
        return DreamDexTransactionSigningSessionResult("signed", material.intent_id, material.lease_id, True, verification, artifact, JournalState.SIGNED.value, artifact.blockers, ())
    finally:
        # The only owner of the raw payload in orchestration scope is dropped.
        signed = None


def build_signed_transaction_preview(result: DreamDexTransactionSigningSessionResult | None = None) -> DreamDexSignedTransactionPreview:
    verification = result.verification if result else None
    return DreamDexSignedTransactionPreview(result.status if result else "unavailable", bool(result and result.signer_invocation_performed), bool(result and result.signer_invocation_performed and result.verification is not None), False, bool(verification and verification.transaction_hash_available), bool(verification and verification.recovered_sender_match), bool(verification and verification.signer_report_match and verification.recovered_sender_match), bool(verification and verification.chain_match), bool(verification and verification.nonce_match), bool(verification and verification.target_match), bool(verification and verification.selector_match), bool(verification and verification.value_match), bool(verification and verification.gas_match), bool(verification and verification.fee_fields_match and verification.fee_model_match), bool(verification and verification.calldata_hash_match and verification.calldata_length_match), "available" if verification and verification.transaction_hash_available else "unavailable", result.journal_state if result else "unavailable", bool(result and result.artifact), False, False, result.blockers if result else ())


def serialize_signed_transaction_diagnostics(value: Any = None) -> dict[str, Any]:
    if isinstance(value, DreamDexEphemeralSignedTransaction):
        raise TypeError("ephemeral_signed_transaction_diagnostics_forbidden")
    if isinstance(value, (DreamDexTransactionSigningMaterial, DreamDexDecodedSignedTransaction, DreamDexSignedTransactionVerificationResult, DreamDexVerifiedSignedTransactionArtifact, DreamDexSigningMaterialValidationResult)):
        return value.safe_dict()
    if isinstance(value, DreamDexSignedTransactionPreview):
        return value.safe_dict()
    if isinstance(value, DreamDexTransactionSigningSessionResult):
        return build_signed_transaction_preview(value).safe_dict()
    if value is None:
        return build_signed_transaction_preview().safe_dict()
    raise TypeError("signed_transaction_diagnostics_type_invalid")


__all__ = [name for name in globals() if name.startswith("DreamDex") or name.startswith("build_") or name.startswith("validate_") or name.startswith("decode_") or name.startswith("verify_") or name.startswith("begin_") or name.startswith("finalize_") or name.startswith("run_") or name.startswith("serialize_")]
