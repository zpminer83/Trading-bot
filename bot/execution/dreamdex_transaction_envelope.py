"""Offline, non-authoritative finalized transaction envelopes.

This module deliberately ends at deterministic construction, validation and a
redacted diagnostic preview.  It has no provider, RPC, HTTP, signer, key,
subprocess or receipt implementation.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import re
from typing import Any, Mapping

from bot.execution.dreamdex_unsigned_transaction import (
    CHAIN_ID,
    MAX_UINT256,
    SUPPORTED_OPERATIONS,
    DreamDexUnsignedTransactionRequest,
    _calldata_bytes,
    _mask,
)


ENVELOPE_SCHEMA_VERSION = "1"
SOURCE_TYPES = frozenset({"external_manual", "test_fixture", "unavailable"})
TRANSACTION_TYPES = frozenset({"legacy", "eip1559", "unresolved"})
EVIDENCE_STATUSES = frozenset({"unavailable", "externally_supplied", "test_confirmed", "source_confirmed", "invalid"})
VENDOR_FEE_POLICY_SOURCE_PATHS = (
    "packages/core/src/execute.ts",
    "packages/core-py/dreamdex_core/nonce.py",
    "packages/core-py/dreamdex_core/execute.py",
    "packages/core/src/config/networks.ts",
)
VENDOR_FEE_POLICY_SUMMARY = "EIP-1559 when base fee/priority fee are available; legacy gasPrice fallback; exact production policy unresolved offline"
VENDOR_GAS_POLICY_SUMMARY = "13/10 estimate headroom; native BUY 5,000,000; native SELL 2,000,000; ERC20 700,000"

_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_SELECTOR_BY_OPERATION = {
    "place_order": "0x4e978373",
    "cancel_order": "0xdbc91396",
    "reduce_order": "0x33407b60",
}


def _address(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _ADDRESS_RE.fullmatch(value):
        raise ValueError(f"{field}: invalid_address")
    return value.lower()


def _int_status(value: Any, *, field: str, allow_none: bool = True, positive: bool = False) -> tuple[int | None, str, tuple[str, ...]]:
    if value is None and allow_none:
        return None, "unavailable", ()
    if isinstance(value, bool) or not isinstance(value, int):
        return None, "invalid", (f"{field}_invalid",)
    if value < (1 if positive else 0) or value > MAX_UINT256:
        return None, "invalid", (f"{field}_invalid",)
    return value, "externally_supplied", ()


def _canonical(value: Mapping[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _request_fingerprint(request: DreamDexUnsignedTransactionRequest) -> str:
    body = {
        "schema_version": request.schema_version,
        "operation": request.operation,
        "chain_id": request.chain_id,
        "from": request.from_address.lower() if request.from_address else None,
        "to": request.to_address.lower() if request.to_address else None,
        "value_wei": request.value_wei,
        "calldata_sha256": request.calldata_sha256,
        "calldata_length": request.calldata_length,
    }
    return sha256(_canonical(body).encode("utf-8")).hexdigest()


def _envelope_fingerprint(*, request_fingerprint: str, nonce: int | None, gas_limit: int | None, transaction_type: str, gas_price_wei: int | None, max_fee_per_gas_wei: int | None, max_priority_fee_per_gas_wei: int | None) -> str:
    body = {
        "request_fingerprint": request_fingerprint,
        "nonce": nonce,
        "gas_limit": gas_limit,
        "transaction_type": transaction_type,
        "gas_price_wei": gas_price_wei,
        "max_fee_per_gas_wei": max_fee_per_gas_wei,
        "max_priority_fee_per_gas_wei": max_priority_fee_per_gas_wei,
    }
    return sha256(_canonical(body).encode("utf-8")).hexdigest()


@dataclass(frozen=True, repr=False)
class DreamDexTransactionEnvelopeEvidence:
    source_type: str = "unavailable"
    source_status: str = "unavailable"
    chain_id_status: str = "unavailable"
    nonce_status: str = "unavailable"
    gas_limit_status: str = "unavailable"
    transaction_type_status: str = "unavailable"
    fee_status: str = "unavailable"
    base_fee_status: str = "unavailable"
    priority_fee_status: str = "unavailable"
    max_fee_status: str = "unavailable"
    block_reference_status: str = "unavailable"
    authoritative: bool = False
    conflicts: tuple[str, ...] = ()
    unresolved_reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.source_type not in SOURCE_TYPES:
            raise ValueError("source_type: unsupported")
        if self.authoritative:
            raise ValueError("envelope evidence cannot be authoritative")
        for name in ("chain_id_status", "nonce_status", "gas_limit_status", "transaction_type_status", "fee_status", "base_fee_status", "priority_fee_status", "max_fee_status", "block_reference_status"):
            if getattr(self, name) not in EVIDENCE_STATUSES:
                raise ValueError(f"{name}: unsupported")
        object.__setattr__(self, "conflicts", tuple(dict.fromkeys(self.conflicts)))
        object.__setattr__(self, "unresolved_reasons", tuple(dict.fromkeys(self.unresolved_reasons)))

    def safe_dict(self) -> dict[str, Any]:
        return {
            "source_type": self.source_type,
            "source_status": self.source_status,
            "chain_id_status": self.chain_id_status,
            "nonce_status": self.nonce_status,
            "gas_limit_status": self.gas_limit_status,
            "transaction_type_status": self.transaction_type_status,
            "fee_status": self.fee_status,
            "base_fee_status": self.base_fee_status,
            "priority_fee_status": self.priority_fee_status,
            "max_fee_status": self.max_fee_status,
            "block_reference_status": self.block_reference_status,
            "authoritative": False,
            "conflicts": self.conflicts,
            "unresolved_reasons": self.unresolved_reasons,
        }

    def __repr__(self) -> str:
        return f"DreamDexTransactionEnvelopeEvidence(source_type={self.source_type!r}, source_status={self.source_status!r}, authoritative=False)"


@dataclass(frozen=True, repr=False)
class DreamDexTransactionTypePolicyEvidence:
    """Immutable source-only summary; it never resolves a production fee mode."""

    source_paths: tuple[str, ...] = VENDOR_FEE_POLICY_SOURCE_PATHS
    source_fingerprints: tuple[tuple[str, str], ...] = ()
    fee_semantics: str = VENDOR_FEE_POLICY_SUMMARY
    transaction_type_status: str = "unavailable"
    conflicts: tuple[str, ...] = ()
    source_status: str = "unavailable"

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_paths", tuple(dict.fromkeys(self.source_paths)))
        object.__setattr__(self, "source_fingerprints", tuple(sorted(self.source_fingerprints)))
        object.__setattr__(self, "conflicts", tuple(dict.fromkeys(self.conflicts)))

    def safe_dict(self) -> dict[str, Any]:
        return {
            "source_paths": self.source_paths,
            "source_fingerprints": self.source_fingerprints,
            "fee_semantics": self.fee_semantics,
            "transaction_type_status": self.transaction_type_status,
            "conflicts": self.conflicts,
            "source_status": self.source_status,
        }

    def __repr__(self) -> str:
        return f"DreamDexTransactionTypePolicyEvidence(source_status={self.source_status!r}, transaction_type_status={self.transaction_type_status!r})"


def build_transaction_type_policy_evidence(source_fingerprints: Mapping[str, str] | None = None, *, source_status: str | None = None, conflicts: tuple[str, ...] = ()) -> DreamDexTransactionTypePolicyEvidence:
    fingerprints = tuple((path, source_fingerprints[path]) for path in VENDOR_FEE_POLICY_SOURCE_PATHS if source_fingerprints and path in source_fingerprints)
    observed = source_status or ("observed" if fingerprints else "unavailable")
    return DreamDexTransactionTypePolicyEvidence(source_fingerprints=fingerprints, source_status=observed, conflicts=conflicts)


@dataclass(frozen=True, repr=False)
class DreamDexUnsignedTransactionEnvelope:
    schema_version: str
    operation: str
    chain_id: int | None
    from_address: str | None
    to_address: str | None
    value_wei: int | None
    calldata: bytes | str | None
    calldata_sha256: str | None
    calldata_length: int | None
    nonce: int | None
    gas_limit: int | None
    transaction_type: str
    gas_price_wei: int | None
    max_fee_per_gas_wei: int | None
    max_priority_fee_per_gas_wei: int | None
    evidence: DreamDexTransactionEnvelopeEvidence
    validation_status: str
    authoritative: bool
    ready_for_signing: bool
    ready_for_submission: bool
    blockers: tuple[str, ...]
    request_fingerprint: str
    envelope_fingerprint: str

    def __post_init__(self) -> None:
        if self.from_address is not None:
            object.__setattr__(self, "from_address", _address(self.from_address, "from_address"))
        if self.to_address is not None:
            object.__setattr__(self, "to_address", _address(self.to_address, "to_address"))
        raw = _calldata_bytes(self.calldata)
        if self.calldata is not None and raw is None:
            raise ValueError("calldata: invalid_hex")
        if raw is not None:
            object.__setattr__(self, "calldata", bytes(raw))
        if self.authoritative or self.ready_for_signing or self.ready_for_submission:
            raise ValueError("unsigned envelope cannot be authoritative or ready")
        if not isinstance(self.evidence, DreamDexTransactionEnvelopeEvidence):
            raise ValueError("evidence_type_invalid")
        object.__setattr__(self, "blockers", tuple(dict.fromkeys(self.blockers)))

    def safe_dict(self) -> dict[str, Any]:
        return serialize_transaction_envelope_diagnostics(self)

    def __repr__(self) -> str:
        return f"DreamDexUnsignedTransactionEnvelope(operation={self.operation!r}, chain_id={self.chain_id!r}, from_address={_mask(self.from_address)!r}, to_address={_mask(self.to_address)!r}, request_fingerprint={self.request_fingerprint!r}, envelope_fingerprint={self.envelope_fingerprint!r}, ready_for_signing=False, ready_for_submission=False)"


@dataclass(frozen=True, repr=False)
class DreamDexTransactionEnvelopePreview:
    operation: str
    chain_id: int | None
    from_address_masked: str
    to_address_masked: str
    value_wei: int | None
    selector: str | None
    calldata_length: int | None
    calldata_sha256: str | None
    request_fingerprint: str | None
    envelope_fingerprint: str | None
    nonce: int | None
    nonce_status: str
    gas_limit: int | None
    gas_limit_status: str
    transaction_type: str
    fee_mode: str
    gas_price_status: str
    max_fee_status: str
    priority_fee_status: str
    evidence_source: str
    authoritative: bool
    ready_for_signing: bool
    ready_for_submission: bool
    blockers: tuple[str, ...]

    def safe_dict(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__ if name != "calldata"}

    def __repr__(self) -> str:
        return f"DreamDexTransactionEnvelopePreview(operation={self.operation!r}, chain_id={self.chain_id!r}, selector={self.selector!r}, request_fingerprint={self.request_fingerprint!r}, envelope_fingerprint={self.envelope_fingerprint!r}, ready_for_signing=False, ready_for_submission=False)"


@dataclass(frozen=True)
class EnvelopeValidationResult:
    valid: bool
    status: str
    errors: tuple[str, ...] = ()


def _fee_validation(*, transaction_type: str, gas_price_wei: Any, max_fee_per_gas_wei: Any, max_priority_fee_per_gas_wei: Any) -> tuple[dict[str, int | None], tuple[str, ...]]:
    errors: list[str] = []
    fields: dict[str, int | None] = {}
    for name, value in (("gas_price_wei", gas_price_wei), ("max_fee_per_gas_wei", max_fee_per_gas_wei), ("max_priority_fee_per_gas_wei", max_priority_fee_per_gas_wei)):
        parsed, _, field_errors = _int_status(value, field=name)
        fields[name] = parsed
        errors.extend(field_errors)
    if transaction_type == "legacy":
        if fields["gas_price_wei"] is None:
            errors.append("gas_price_required")
        if fields["max_fee_per_gas_wei"] is not None or fields["max_priority_fee_per_gas_wei"] is not None:
            errors.append("legacy_mixed_fee_fields")
    elif transaction_type == "eip1559":
        if fields["gas_price_wei"] is not None:
            errors.append("eip1559_mixed_gas_price")
        if fields["max_fee_per_gas_wei"] is None or fields["max_priority_fee_per_gas_wei"] is None:
            errors.append("eip1559_fee_fields_required")
        elif fields["max_fee_per_gas_wei"] < fields["max_priority_fee_per_gas_wei"]:
            errors.append("max_fee_below_priority_fee")
    elif transaction_type == "unresolved":
        if any(value is not None for value in fields.values()):
            errors.append("unresolved_mixed_fee_fields")
    else:
        errors.append("transaction_type_invalid")
    return fields, tuple(dict.fromkeys(errors))


def validate_unsigned_transaction_envelope(envelope: DreamDexUnsignedTransactionEnvelope, request: DreamDexUnsignedTransactionRequest | None = None) -> EnvelopeValidationResult:
    errors: list[str] = []
    if not isinstance(envelope, DreamDexUnsignedTransactionEnvelope):
        return EnvelopeValidationResult(False, "blocked", ("envelope_type_invalid",))
    if envelope.operation not in SUPPORTED_OPERATIONS:
        errors.append("operation_invalid")
    if envelope.chain_id != CHAIN_ID:
        errors.append("chain_id_invalid")
    if envelope.transaction_type not in TRANSACTION_TYPES:
        errors.append("transaction_type_invalid")
    if envelope.authoritative:
        errors.append("authoritative_forbidden")
    if envelope.ready_for_signing or envelope.ready_for_submission:
        errors.append("readiness_forbidden")
    raw = _calldata_bytes(envelope.calldata)
    if envelope.from_address is None:
        errors.append("from_address_unavailable")
    if envelope.to_address is None:
        errors.append("to_address_unavailable")
    digest = sha256(raw).hexdigest() if raw is not None else None
    if raw is None or len(raw) < 4:
        errors.append("calldata_unavailable")
    else:
        if "0x" + raw[:4].hex() != _SELECTOR_BY_OPERATION.get(envelope.operation):
            errors.append("selector_operation_mismatch")
    if digest != envelope.calldata_sha256:
        errors.append("calldata_hash_mismatch")
    if raw is not None and len(raw) != envelope.calldata_length:
        errors.append("calldata_length_mismatch")
    if envelope.value_wei is None or isinstance(envelope.value_wei, bool) or not isinstance(envelope.value_wei, int) or not 0 <= envelope.value_wei <= MAX_UINT256:
        errors.append("value_invalid")
    if envelope.nonce is not None and (isinstance(envelope.nonce, bool) or not isinstance(envelope.nonce, int) or not 0 <= envelope.nonce <= MAX_UINT256):
        errors.append("nonce_invalid")
    if envelope.nonce is None:
        errors.append("transaction_nonce_unresolved")
    if envelope.gas_limit is not None and (isinstance(envelope.gas_limit, bool) or not isinstance(envelope.gas_limit, int) or not 0 < envelope.gas_limit <= MAX_UINT256):
        errors.append("gas_limit_invalid")
    if envelope.gas_limit is None:
        errors.append("transaction_gas_unresolved")
    _, fee_errors = _fee_validation(transaction_type=envelope.transaction_type, gas_price_wei=envelope.gas_price_wei, max_fee_per_gas_wei=envelope.max_fee_per_gas_wei, max_priority_fee_per_gas_wei=envelope.max_priority_fee_per_gas_wei)
    errors.extend(fee_errors)
    if envelope.transaction_type == "unresolved":
        errors.append("transaction_fees_unresolved")
    if request is not None:
        if not isinstance(request, DreamDexUnsignedTransactionRequest):
            errors.append("request_type_invalid")
        else:
            if envelope.operation != request.operation:
                errors.append("operation_request_mismatch")
            if envelope.chain_id != request.chain_id:
                errors.append("chain_id_request_mismatch")
            if envelope.from_address != request.from_address or envelope.to_address != request.to_address:
                errors.append("address_request_mismatch")
            if envelope.value_wei != request.value_wei:
                errors.append("value_request_mismatch")
            if digest != request.calldata_sha256 or envelope.calldata_length != request.calldata_length:
                errors.append("calldata_request_mismatch")
            expected_request = _request_fingerprint(request)
            if envelope.request_fingerprint != expected_request:
                errors.append("request_fingerprint_mismatch")
    expected_envelope = _envelope_fingerprint(request_fingerprint=envelope.request_fingerprint, nonce=envelope.nonce, gas_limit=envelope.gas_limit, transaction_type=envelope.transaction_type, gas_price_wei=envelope.gas_price_wei, max_fee_per_gas_wei=envelope.max_fee_per_gas_wei, max_priority_fee_per_gas_wei=envelope.max_priority_fee_per_gas_wei)
    if envelope.envelope_fingerprint != expected_envelope:
        errors.append("envelope_fingerprint_mismatch")
    unique = tuple(dict.fromkeys(errors))
    return EnvelopeValidationResult(not unique, "structurally_complete" if not unique else "blocked", unique)


def build_unsigned_transaction_envelope(request: DreamDexUnsignedTransactionRequest, *, nonce: int | None, gas_limit: int | None, transaction_type: str, gas_price_wei: int | None = None, max_fee_per_gas_wei: int | None = None, max_priority_fee_per_gas_wei: int | None = None, evidence: DreamDexTransactionEnvelopeEvidence | None = None) -> DreamDexUnsignedTransactionEnvelope:
    if not isinstance(request, DreamDexUnsignedTransactionRequest):
        raise ValueError("request_type_invalid")
    evidence = evidence or DreamDexTransactionEnvelopeEvidence(source_type="unavailable")
    request_fp = _request_fingerprint(request)
    blockers: list[str] = list(request.validation_errors)
    if request.authoritative:
        blockers.append("request_authoritative_forbidden")
    nonce_value, nonce_status, nonce_errors = _int_status(nonce, field="nonce")
    gas_value, gas_status, gas_errors = _int_status(gas_limit, field="gas_limit", positive=True)
    fee_values, fee_errors = _fee_validation(transaction_type=transaction_type, gas_price_wei=gas_price_wei, max_fee_per_gas_wei=max_fee_per_gas_wei, max_priority_fee_per_gas_wei=max_priority_fee_per_gas_wei)
    blockers.extend(nonce_errors)
    blockers.extend(gas_errors)
    blockers.extend(fee_errors)
    if nonce_value is None:
        blockers.append("transaction_nonce_unresolved")
    if gas_value is None:
        blockers.append("transaction_gas_unresolved")
    if transaction_type == "unresolved":
        blockers.append("transaction_type_policy_unresolved")
    if any(value == 0 for value in fee_values.values() if value is not None):
        blockers.append("zero_fee_not_production_ready")
    if transaction_type == "unresolved":
        blockers.append("transaction_fees_unresolved")
    blockers.extend(evidence.conflicts)
    blockers.extend(evidence.unresolved_reasons)
    blockers.extend(("transaction_signer_unavailable", "direct_signer_key_unavailable", "transaction_submission_unavailable"))
    raw = _calldata_bytes(request.calldata)
    envelope_fp = _envelope_fingerprint(request_fingerprint=request_fp, nonce=nonce_value, gas_limit=gas_value, transaction_type=transaction_type, gas_price_wei=fee_values["gas_price_wei"], max_fee_per_gas_wei=fee_values["max_fee_per_gas_wei"], max_priority_fee_per_gas_wei=fee_values["max_priority_fee_per_gas_wei"])
    structural_blockers = [*request.validation_errors, *nonce_errors, *gas_errors, *fee_errors, *evidence.conflicts]
    if transaction_type == "unresolved":
        structural_blockers.extend(("transaction_type_policy_unresolved", "transaction_fees_unresolved"))
    status = "structurally_complete" if not tuple(dict.fromkeys(structural_blockers)) else "incomplete"
    return DreamDexUnsignedTransactionEnvelope(
        schema_version=ENVELOPE_SCHEMA_VERSION,
        operation=request.operation,
        chain_id=request.chain_id,
        from_address=request.from_address,
        to_address=request.to_address,
        value_wei=request.value_wei,
        calldata=raw,
        calldata_sha256=request.calldata_sha256,
        calldata_length=request.calldata_length,
        nonce=nonce_value,
        gas_limit=gas_value,
        transaction_type=transaction_type,
        gas_price_wei=fee_values["gas_price_wei"],
        max_fee_per_gas_wei=fee_values["max_fee_per_gas_wei"],
        max_priority_fee_per_gas_wei=fee_values["max_priority_fee_per_gas_wei"],
        evidence=evidence,
        validation_status=status,
        authoritative=False,
        ready_for_signing=False,
        ready_for_submission=False,
        blockers=tuple(dict.fromkeys(blockers)),
        request_fingerprint=request_fp,
        envelope_fingerprint=envelope_fp,
    )


def build_transaction_envelope_preview(envelope: DreamDexUnsignedTransactionEnvelope) -> DreamDexTransactionEnvelopePreview:
    validation = validate_unsigned_transaction_envelope(envelope)
    raw = _calldata_bytes(envelope.calldata)
    selector = "0x" + raw[:4].hex() if raw is not None and len(raw) >= 4 else None
    fee_mode = envelope.transaction_type
    if envelope.transaction_type == "legacy":
        gas_price_status = "externally_supplied" if envelope.gas_price_wei is not None else "unavailable"
        max_fee_status = priority_status = "unavailable"
    elif envelope.transaction_type == "eip1559":
        gas_price_status = "unavailable"
        max_fee_status = "externally_supplied" if envelope.max_fee_per_gas_wei is not None else "unavailable"
        priority_status = "externally_supplied" if envelope.max_priority_fee_per_gas_wei is not None else "unavailable"
    else:
        gas_price_status = max_fee_status = priority_status = "unavailable"
    return DreamDexTransactionEnvelopePreview(
        operation=envelope.operation,
        chain_id=envelope.chain_id,
        from_address_masked=_mask(envelope.from_address),
        to_address_masked=_mask(envelope.to_address),
        value_wei=envelope.value_wei,
        selector=selector,
        calldata_length=envelope.calldata_length,
        calldata_sha256=envelope.calldata_sha256,
        request_fingerprint=envelope.request_fingerprint,
        envelope_fingerprint=envelope.envelope_fingerprint,
        nonce=envelope.nonce,
        nonce_status=envelope.evidence.nonce_status if envelope.nonce is not None else "unavailable",
        gas_limit=envelope.gas_limit,
        gas_limit_status=envelope.evidence.gas_limit_status if envelope.gas_limit is not None else "unavailable",
        transaction_type=envelope.transaction_type,
        fee_mode=fee_mode,
        gas_price_status=gas_price_status,
        max_fee_status=max_fee_status,
        priority_fee_status=priority_status,
        evidence_source=envelope.evidence.source_type,
        authoritative=False,
        ready_for_signing=False,
        ready_for_submission=False,
        blockers=tuple(dict.fromkeys((*envelope.blockers, *validation.errors))),
    )


def serialize_transaction_envelope_diagnostics(envelope: DreamDexUnsignedTransactionEnvelope) -> dict[str, Any]:
    preview = build_transaction_envelope_preview(envelope)
    return {
        "operation": preview.operation,
        "chain_id": preview.chain_id,
        "from_address_masked": preview.from_address_masked,
        "to_address_masked": preview.to_address_masked,
        "value_wei": preview.value_wei,
        "selector": preview.selector,
        "calldata_length": preview.calldata_length,
        "calldata_sha256": preview.calldata_sha256,
        "request_fingerprint": preview.request_fingerprint,
        "envelope_fingerprint": preview.envelope_fingerprint,
        "nonce": preview.nonce,
        "nonce_status": preview.nonce_status,
        "gas_limit": preview.gas_limit,
        "gas_limit_status": preview.gas_limit_status,
        "transaction_type": preview.transaction_type,
        "fee_mode": preview.fee_mode,
        "gas_price_status": preview.gas_price_status,
        "max_fee_status": preview.max_fee_status,
        "priority_fee_status": preview.priority_fee_status,
        "evidence_source": preview.evidence_source,
        "authoritative": False,
        "ready_for_signing": False,
        "ready_for_submission": False,
        "blockers": preview.blockers,
    }


def describe_transaction_envelope_capabilities() -> Mapping[str, str]:
    return {
        "build_unsigned_request": "available_offline",
        "validate_unsigned_request": "available_offline",
        "build_unsigned_envelope": "available_offline",
        "validate_unsigned_envelope": "available_offline",
        "preview_unsigned_envelope": "available_offline",
        "resolve_nonce": "unavailable",
        "estimate_gas": "unavailable",
        "resolve_fees": "unavailable",
        "sign_transaction": "unavailable",
        "serialize_signed_transaction": "unavailable",
        "submit_transaction": "unavailable",
        "wait_for_receipt": "unavailable",
    }


__all__ = [
    "ENVELOPE_SCHEMA_VERSION", "SOURCE_TYPES", "TRANSACTION_TYPES", "EVIDENCE_STATUSES", "VENDOR_FEE_POLICY_SOURCE_PATHS", "VENDOR_FEE_POLICY_SUMMARY", "VENDOR_GAS_POLICY_SUMMARY",
    "DreamDexTransactionEnvelopeEvidence", "DreamDexTransactionTypePolicyEvidence", "DreamDexUnsignedTransactionEnvelope", "DreamDexTransactionEnvelopePreview", "EnvelopeValidationResult",
    "build_transaction_type_policy_evidence", "build_unsigned_transaction_envelope", "validate_unsigned_transaction_envelope", "build_transaction_envelope_preview", "serialize_transaction_envelope_diagnostics", "describe_transaction_envelope_capabilities",
]
