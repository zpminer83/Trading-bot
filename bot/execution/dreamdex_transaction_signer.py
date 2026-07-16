"""Offline policy boundary for DreamDEX transaction signing.

This module intentionally stops before key handling, cryptography, providers,
processes, and submission.  It validates a finalized unsigned envelope and
produces redacted, deterministic diagnostics only.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import re
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

from bot.execution.dreamdex_direct_order_encoding import CANCEL_SELECTOR, PLACE_SELECTOR, REDUCE_SELECTOR
from bot.execution.dreamdex_transaction_envelope import (
    DreamDexUnsignedTransactionEnvelope,
    validate_unsigned_transaction_envelope,
)
from bot.execution.dreamdex_unsigned_transaction import CHAIN_ID, MAX_UINT256
from bot.execution.dreamdex_execution_primitives import deterministic_fingerprint, mask_evm_address, mask_hex_hash, ensure_no_raw_sensitive_fields

POOL_ADDRESS = "0x035de7403eac6872787779cca7ccf1b4cdb61379"
SIGNER_STATUS_VALUES = frozenset({"unavailable", "available_offline", "test_fixture", "source_confirmed"})
SIGNATURE_STATUS_VALUES = frozenset({"unavailable", "test_fixture", "valid", "invalid"})
_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_HASH_RE = re.compile(r"^(?:0x)?[0-9a-fA-F]{64}$")
_SELECTORS = {"place_order": PLACE_SELECTOR.lower(), "cancel_order": CANCEL_SELECTOR.lower(), "reduce_order": REDUCE_SELECTOR.lower()}
ALLOWED_OPERATIONS = tuple(_SELECTORS)
ALLOWED_SELECTORS = tuple(_SELECTORS.items())
ALLOWED_TARGET_ADDRESSES = (POOL_ADDRESS,)


def _addr(value: str | None, field: str, *, allow_none: bool = True) -> str | None:
    if value is None and allow_none:
        return None
    if not isinstance(value, str) or not _ADDRESS_RE.fullmatch(value):
        raise ValueError(f"{field}: invalid_address")
    return value.lower()


def _uint(value: Any, field: str, *, allow_none: bool = True, positive: bool = False) -> int | None:
    if value is None and allow_none:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < (1 if positive else 0) or value > MAX_UINT256:
        raise ValueError(f"{field}: invalid_uint256")
    return value


def _selector(envelope: DreamDexUnsignedTransactionEnvelope) -> str | None:
    raw = envelope.calldata
    if isinstance(raw, str) and raw.startswith("0x"):
        try:
            raw = bytes.fromhex(raw[2:])
        except ValueError:
            return None
    if isinstance(raw, (bytes, bytearray)) and len(raw) >= 4:
        return "0x" + bytes(raw[:4]).hex()
    return None


def _max_fee(envelope: DreamDexUnsignedTransactionEnvelope) -> int | None:
    if envelope.transaction_type == "legacy":
        if envelope.gas_limit is None or envelope.gas_price_wei is None:
            return None
        return envelope.gas_limit * envelope.gas_price_wei
    if envelope.transaction_type == "eip1559":
        if envelope.gas_limit is None or envelope.max_fee_per_gas_wei is None:
            return None
        return envelope.gas_limit * envelope.max_fee_per_gas_wei
    return None


def _request_fp(values: Mapping[str, Any]) -> str:
    return deterministic_fingerprint(dict(values), domain="dreamdex/transaction-signing-request", schema_version="1")


@dataclass(frozen=True, repr=False)
class DreamDexTransactionSigningPolicy:
    schema_version: str = "1"
    required_chain_id: int = CHAIN_ID
    required_signer_address: str | None = None
    allowed_target_addresses: tuple[str, ...] = ALLOWED_TARGET_ADDRESSES
    allowed_operations: tuple[str, ...] = ALLOWED_OPERATIONS
    allowed_selectors: tuple[tuple[str, str], ...] = ALLOWED_SELECTORS
    allow_native_value: bool = False
    maximum_native_value_wei: int | None = None
    maximum_gas_limit: int | None = None
    maximum_total_fee_wei: int | None = None
    require_request_fingerprint: bool = True
    require_envelope_fingerprint: bool = True
    require_structurally_complete_envelope: bool = True
    require_authoritative_transaction_type: bool = True
    require_exact_sender_match: bool = True
    require_exact_target_match: bool = True
    require_exact_selector_match: bool = True
    production_status: str = "unavailable"
    authoritative: bool = False
    unresolved_reasons: tuple[str, ...] = ("signer_address_unresolved", "signer_unavailable")

    @classmethod
    def production_default(cls) -> "DreamDexTransactionSigningPolicy":
        return cls()

    def __post_init__(self) -> None:
        object.__setattr__(self, "required_signer_address", _addr(self.required_signer_address, "required_signer_address"))
        object.__setattr__(self, "allowed_target_addresses", tuple(dict.fromkeys(_addr(x, "allowed_target_address", allow_none=False) for x in self.allowed_target_addresses)))
        object.__setattr__(self, "allowed_operations", tuple(dict.fromkeys(self.allowed_operations)))
        object.__setattr__(self, "allowed_selectors", tuple((str(op), str(sel).lower()) for op, sel in self.allowed_selectors))
        if self.required_chain_id != CHAIN_ID:
            raise ValueError("required_chain_id: unsupported")
        if not self.allowed_target_addresses or any(x is None for x in self.allowed_target_addresses):
            raise ValueError("allowed_target_addresses: required")
        if any(target != POOL_ADDRESS for target in self.allowed_target_addresses):
            raise ValueError("allowed_target_addresses: source_confirmed_pool_required")
        if not set(self.allowed_operations).issubset(set(_SELECTORS)):
            raise ValueError("allowed_operations: unsupported")
        for _, sel in self.allowed_selectors:
            if not re.fullmatch(r"0x[0-9a-f]{8}", sel):
                raise ValueError("allowed_selectors: invalid")
        selector_map = dict(self.allowed_selectors)
        for operation, selector in selector_map.items():
            if operation not in _SELECTORS or selector != _SELECTORS[operation]:
                raise ValueError("allowed_selectors: exact_allowlist_required")
        for name in ("maximum_native_value_wei", "maximum_gas_limit", "maximum_total_fee_wei"):
            value = getattr(self, name)
            if value is not None:
                _uint(value, name)
                if name != "maximum_native_value_wei" and value <= 0:
                    raise ValueError(f"{name}: must_be_positive")
        if self.maximum_native_value_wei is not None and self.maximum_native_value_wei < 0:
            raise ValueError("maximum_native_value_wei: invalid")
        if self.production_status not in SIGNER_STATUS_VALUES:
            raise ValueError("production_status: unsupported")
        object.__setattr__(self, "unresolved_reasons", tuple(dict.fromkeys(self.unresolved_reasons)))

    def safe_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version, "required_chain_id": self.required_chain_id,
            "required_signer_address_masked": mask_evm_address(self.required_signer_address),
            "allowed_target_addresses_masked": tuple(mask_evm_address(x) for x in self.allowed_target_addresses),
            "allowed_operations": self.allowed_operations, "allowed_selectors": self.allowed_selectors,
            "allow_native_value": self.allow_native_value, "maximum_native_value_wei": self.maximum_native_value_wei,
            "maximum_gas_limit": self.maximum_gas_limit, "maximum_total_fee_wei": self.maximum_total_fee_wei,
            "production_status": self.production_status, "authoritative": False, "unresolved_reasons": self.unresolved_reasons,
        }

    def __repr__(self) -> str:
        return f"DreamDexTransactionSigningPolicy(chain_id={self.required_chain_id}, signer={mask_evm_address(self.required_signer_address)!r}, production_status={self.production_status!r}, authoritative=False)"


@dataclass(frozen=True, repr=False)
class DreamDexTransactionSigningRequest:
    schema_version: str
    operation: str
    chain_id: int | None
    signer_address: str | None
    target_address: str | None
    selector: str | None
    value_wei: int | None
    nonce: int | None
    gas_limit: int | None
    transaction_type: str | None
    gas_price_wei: int | None
    max_fee_per_gas_wei: int | None
    max_priority_fee_per_gas_wei: int | None
    request_fingerprint: str | None
    envelope_fingerprint: str | None
    calldata_sha256: str | None
    calldata_length: int | None
    signing_request_fingerprint: str
    policy_validation_status: str
    policy_approved: bool
    ready_for_signer_invocation: bool
    blockers: tuple[str, ...] = ()
    validation_errors: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "signer_address", _addr(self.signer_address, "signer_address"))
        object.__setattr__(self, "target_address", _addr(self.target_address, "target_address"))
        if self.selector is not None and not re.fullmatch(r"0x[0-9a-fA-F]{8}", self.selector):
            raise ValueError("selector: invalid")
        for field in ("value_wei", "nonce", "gas_limit", "gas_price_wei", "max_fee_per_gas_wei", "max_priority_fee_per_gas_wei"):
            _uint(getattr(self, field), field)
        object.__setattr__(self, "blockers", tuple(dict.fromkeys(self.blockers)))
        object.__setattr__(self, "validation_errors", tuple(dict.fromkeys(self.validation_errors)))
        if self.ready_for_signer_invocation and not self.policy_approved:
            raise ValueError("ready_for_signer_invocation_requires_approval")

    def safe_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version, "operation": self.operation, "chain_id": self.chain_id,
            "signer_address_masked": mask_evm_address(self.signer_address), "target_address_masked": mask_evm_address(self.target_address),
            "selector": self.selector, "value_wei": self.value_wei, "nonce_status": "present" if self.nonce is not None else "unavailable",
            "gas_limit": self.gas_limit, "transaction_type": self.transaction_type, "fee_fields_present": tuple(x for x, v in (("gas_price_wei", self.gas_price_wei), ("max_fee_per_gas_wei", self.max_fee_per_gas_wei), ("max_priority_fee_per_gas_wei", self.max_priority_fee_per_gas_wei)) if v is not None),
            "request_fingerprint": mask_hex_hash(self.request_fingerprint), "envelope_fingerprint": mask_hex_hash(self.envelope_fingerprint),
            "calldata_sha256": mask_hex_hash(self.calldata_sha256), "calldata_length": self.calldata_length,
            "signing_request_fingerprint": mask_hex_hash(self.signing_request_fingerprint), "policy_validation_status": self.policy_validation_status,
            "policy_approved": self.policy_approved, "ready_for_signer_invocation": self.ready_for_signer_invocation,
            "blockers": self.blockers, "validation_errors": self.validation_errors,
        }

    def __repr__(self) -> str:
        return f"DreamDexTransactionSigningRequest(operation={self.operation!r}, chain_id={self.chain_id!r}, signer={mask_evm_address(self.signer_address)!r}, selector={self.selector!r}, signing_request_fingerprint={mask_hex_hash(self.signing_request_fingerprint)!r}, policy_approved={self.policy_approved!r})"


@dataclass(frozen=True, repr=False)
class DreamDexSigningPolicyValidationResult:
    chain_match: bool = False
    signer_match: bool = False
    target_match: bool = False
    operation_match: bool = False
    selector_match: bool = False
    value_policy_match: bool = False
    gas_policy_match: bool = False
    fee_policy_match: bool = False
    fingerprint_match: bool = False
    transaction_type_match: bool = False
    structurally_complete: bool = False
    approved: bool = False
    blockers: tuple[str, ...] = ()
    validation_errors: tuple[str, ...] = ()

    def safe_dict(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}

    def __repr__(self) -> str:
        return f"DreamDexSigningPolicyValidationResult(approved={self.approved!r}, blockers={self.blockers!r})"


@dataclass(frozen=True, repr=False)
class DreamDexTransactionSignerCapabilities:
    signer_type: str = "unavailable"
    address_discovery: str = "unavailable"
    transaction_signing: str = "unavailable"
    supported_chain_ids: tuple[int, ...] = ()
    supported_transaction_types: tuple[str, ...] = ()
    supported_operations: tuple[str, ...] = ()
    arbitrary_contract_calls: bool = False
    arbitrary_calldata: bool = False
    native_value_support: bool = False
    production_status: str = "unavailable"
    authoritative: bool = False
    unresolved_reasons: tuple[str, ...] = ("signer_implementation_unavailable",)

    def safe_dict(self) -> dict[str, Any]:
        return {"signer_type": self.signer_type, "address_discovery": self.address_discovery, "transaction_signing": self.transaction_signing, "supported_chain_ids": self.supported_chain_ids, "supported_transaction_types": self.supported_transaction_types, "supported_operations": self.supported_operations, "arbitrary_contract_calls": False, "arbitrary_calldata": False, "native_value_support": self.native_value_support, "production_status": self.production_status, "authoritative": False, "unresolved_reasons": self.unresolved_reasons}

    def __repr__(self) -> str:
        return f"DreamDexTransactionSignerCapabilities(signer_type={self.signer_type!r}, transaction_signing={self.transaction_signing!r}, authoritative=False)"


@dataclass(frozen=True, repr=False)
class DreamDexTransactionSigningPreview:
    operation: str
    chain_id: int | None
    signer_address_masked: str
    target_address_masked: str
    selector: str | None
    value_wei: int | None
    nonce_status: str
    gas_limit: int | None
    fee_mode: str
    maximum_possible_fee_wei: int | None
    request_fingerprint: str | None
    envelope_fingerprint: str | None
    signing_request_fingerprint: str | None
    signer_status: str
    policy_approved: bool
    signer_invocation_allowed: bool
    raw_calldata_output_allowed: bool
    blockers: tuple[str, ...] = ()

    def safe_dict(self) -> dict[str, Any]:
        return {"operation": self.operation, "chain_id": self.chain_id, "signer_address_masked": self.signer_address_masked, "target_address_masked": self.target_address_masked, "selector": self.selector, "value_wei": self.value_wei, "nonce_status": self.nonce_status, "gas_limit": self.gas_limit, "fee_mode": self.fee_mode, "maximum_possible_fee_wei": self.maximum_possible_fee_wei, "request_fingerprint": mask_hex_hash(self.request_fingerprint), "envelope_fingerprint": mask_hex_hash(self.envelope_fingerprint), "signing_request_fingerprint": mask_hex_hash(self.signing_request_fingerprint), "signer_status": self.signer_status, "policy_approved": self.policy_approved, "signer_invocation_allowed": self.signer_invocation_allowed, "raw_calldata_output_allowed": False, "blockers": self.blockers}

    def __repr__(self) -> str:
        return f"DreamDexTransactionSigningPreview(operation={self.operation!r}, chain_id={self.chain_id!r}, selector={self.selector!r}, policy_approved={self.policy_approved!r}, signer_invocation_allowed=False)"


@dataclass(frozen=True, repr=False)
class DreamDexSignedTransactionArtifact:
    schema_version: str
    signer_address: str
    signing_request_fingerprint: str
    signed_transaction_hash: str
    signed_payload_length: int
    signature_status: str
    source_type: str
    authoritative: bool
    ready_for_submission: bool
    blockers: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "signer_address", _addr(self.signer_address, "signer_address", allow_none=False))
        _uint(self.signed_payload_length, "signed_payload_length")
        if self.signature_status not in SIGNATURE_STATUS_VALUES:
            raise ValueError("signature_status: unsupported")
        if self.authoritative or self.ready_for_submission:
            raise ValueError("signed artifact cannot be authoritative or ready_for_submission")

    def safe_dict(self) -> dict[str, Any]:
        return {"schema_version": self.schema_version, "signer_address_masked": mask_evm_address(self.signer_address), "signing_request_fingerprint": mask_hex_hash(self.signing_request_fingerprint), "signed_transaction_hash": mask_hex_hash(self.signed_transaction_hash), "signed_payload_length": self.signed_payload_length, "signature_status": self.signature_status, "source_type": self.source_type, "authoritative": False, "ready_for_submission": False, "blockers": self.blockers}

    def __repr__(self) -> str:
        return f"DreamDexSignedTransactionArtifact(signer={mask_evm_address(self.signer_address)!r}, signature_status={self.signature_status!r}, ready_for_submission=False)"


@runtime_checkable
class DreamDexTransactionSigner(Protocol):
    def get_address(self) -> str: ...
    def describe_capabilities(self) -> DreamDexTransactionSignerCapabilities: ...
    def sign_transaction(self, request: DreamDexTransactionSigningRequest) -> DreamDexSignedTransactionArtifact: ...


class UnavailableDreamDexTransactionSigner:
    def get_address(self) -> str:
        return "<unavailable>"

    def describe_capabilities(self) -> DreamDexTransactionSignerCapabilities:
        return DreamDexTransactionSignerCapabilities()

    def sign_transaction(self, request: DreamDexTransactionSigningRequest) -> DreamDexSignedTransactionArtifact:
        raise RuntimeError("transaction_signer_unavailable")


def build_production_transaction_signing_policy(**overrides: Any) -> DreamDexTransactionSigningPolicy:
    return DreamDexTransactionSigningPolicy(**overrides)


def validate_transaction_signing_policy(envelope: Any, policy: DreamDexTransactionSigningPolicy, *, signer_address: str | None = None, request: Any = None) -> DreamDexSigningPolicyValidationResult:
    blockers: list[str] = []
    errors: list[str] = []
    if not isinstance(policy, DreamDexTransactionSigningPolicy):
        raise ValueError("policy_type_invalid")
    if not isinstance(envelope, DreamDexUnsignedTransactionEnvelope):
        return DreamDexSigningPolicyValidationResult(blockers=("envelope_type_invalid",), validation_errors=("envelope_type_invalid",))
    structural = validate_unsigned_transaction_envelope(envelope, request=request)
    errors.extend(structural.errors)
    chain = envelope.chain_id == policy.required_chain_id
    if not chain: blockers.append("chain_id_mismatch")
    signer = _addr(signer_address, "signer_address") if signer_address is not None else policy.required_signer_address
    signer_match = bool(signer and envelope.from_address and signer.lower() == envelope.from_address.lower())
    if policy.required_signer_address is None or signer is None:
        blockers.append("transaction_signer_address_unresolved")
    else:
        if signer.lower() != policy.required_signer_address.lower():
            signer_match = False
            blockers.append("policy_signer_address_mismatch")
        if not signer_match:
            blockers.append("signer_address_mismatch")
    target = bool(envelope.to_address and envelope.to_address.lower() in policy.allowed_target_addresses)
    if not target: blockers.append("target_address_not_allowlisted")
    operation = envelope.operation in policy.allowed_operations
    if not operation: blockers.append("operation_not_allowlisted")
    selector = _selector(envelope)
    expected_selector = dict(policy.allowed_selectors).get(envelope.operation)
    selector_match = bool(selector and expected_selector and selector == expected_selector and _SELECTORS.get(envelope.operation) == selector)
    if not selector_match: blockers.append("selector_operation_mismatch")
    value = envelope.value_wei
    value_ok = isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= MAX_UINT256
    if envelope.operation in {"cancel_order", "reduce_order"} and value != 0: value_ok = False
    if envelope.operation == "place_order" and value != 0:
        value_ok = bool(policy.allow_native_value and policy.maximum_native_value_wei is not None and value_ok and value <= policy.maximum_native_value_wei)
        if policy.maximum_native_value_wei is None: blockers.append("transaction_value_limit_unresolved")
    if not value_ok: blockers.append("value_policy_rejected")
    gas_ok = isinstance(envelope.gas_limit, int) and not isinstance(envelope.gas_limit, bool) and envelope.gas_limit > 0 and envelope.gas_limit <= MAX_UINT256
    if policy.maximum_gas_limit is None: blockers.append("transaction_fee_limit_unresolved")
    elif gas_ok and envelope.gas_limit > policy.maximum_gas_limit: gas_ok = False; blockers.append("gas_limit_exceeded")
    if not gas_ok: blockers.append("gas_policy_rejected")
    fee = _max_fee(envelope)
    fee_components = (envelope.gas_price_wei,) if envelope.transaction_type == "legacy" else (envelope.max_fee_per_gas_wei, envelope.max_priority_fee_per_gas_wei)
    fee_ok = fee is not None and all(isinstance(item, int) and not isinstance(item, bool) and item > 0 for item in fee_components) and fee <= MAX_UINT256
    if policy.maximum_total_fee_wei is None: blockers.append("transaction_fee_limit_unresolved")
    elif fee_ok and fee > policy.maximum_total_fee_wei: fee_ok = False; blockers.append("total_fee_exceeded")
    if not fee_ok: blockers.append("fee_policy_rejected")
    tx_type = envelope.transaction_type in {"legacy", "eip1559"}
    if not tx_type: blockers.append("transaction_type_unresolved")
    fp_ok = bool(envelope.request_fingerprint and envelope.envelope_fingerprint and envelope.calldata_sha256 is not None and envelope.calldata_length is not None)
    fp_ok = fp_ok and bool(_HASH_RE.fullmatch(str(envelope.request_fingerprint))) and bool(_HASH_RE.fullmatch(str(envelope.envelope_fingerprint))) and bool(_HASH_RE.fullmatch(str(envelope.calldata_sha256)))
    if not fp_ok: blockers.append("transaction_fingerprint_unresolved")
    if policy.require_authoritative_transaction_type and envelope.evidence.transaction_type_status not in {"source_confirmed", "test_confirmed"}: blockers.append("transaction_type_unresolved")
    structural_ok = structural.valid and envelope.validation_status == "structurally_complete"
    if not structural_ok: blockers.extend(structural.errors or ("envelope_not_structurally_complete",))
    all_blockers = tuple(dict.fromkeys((*blockers, *errors)))
    approved = not all_blockers and structural_ok and chain and signer_match and target and operation and selector_match and value_ok and gas_ok and fee_ok and tx_type and fp_ok
    return DreamDexSigningPolicyValidationResult(chain, signer_match, target, operation, selector_match, value_ok, gas_ok, fee_ok, fp_ok, tx_type, structural_ok, approved, all_blockers, tuple(dict.fromkeys(errors)))


def build_transaction_signing_request(envelope: Any, policy: DreamDexTransactionSigningPolicy, *, signer_address: str | None = None) -> DreamDexTransactionSigningRequest:
    if not isinstance(envelope, DreamDexUnsignedTransactionEnvelope):
        raise ValueError("envelope_type_invalid")
    result = validate_transaction_signing_policy(envelope, policy, signer_address=signer_address)
    values = {"operation": envelope.operation, "chain_id": envelope.chain_id, "signer_address": envelope.from_address, "target_address": envelope.to_address, "selector": _selector(envelope), "value_wei": envelope.value_wei, "nonce": envelope.nonce, "gas_limit": envelope.gas_limit, "transaction_type": envelope.transaction_type, "gas_price_wei": envelope.gas_price_wei, "max_fee_per_gas_wei": envelope.max_fee_per_gas_wei, "max_priority_fee_per_gas_wei": envelope.max_priority_fee_per_gas_wei, "request_fingerprint": envelope.request_fingerprint, "envelope_fingerprint": envelope.envelope_fingerprint, "calldata_sha256": envelope.calldata_sha256, "calldata_length": envelope.calldata_length}
    fp = _request_fp(values)
    return DreamDexTransactionSigningRequest(schema_version="1", signing_request_fingerprint=fp, policy_validation_status="approved" if result.approved else "rejected", policy_approved=result.approved, ready_for_signer_invocation=result.approved and policy.production_status in {"test_fixture", "source_confirmed"}, blockers=result.blockers, validation_errors=result.validation_errors, **values)


def validate_transaction_signing_request(request: Any, *, envelope: Any = None, policy: DreamDexTransactionSigningPolicy | None = None) -> DreamDexSigningPolicyValidationResult:
    if not isinstance(request, DreamDexTransactionSigningRequest):
        return DreamDexSigningPolicyValidationResult(blockers=("signing_request_type_invalid",), validation_errors=("signing_request_type_invalid",))
    values = {name: getattr(request, name) for name in ("operation", "chain_id", "signer_address", "target_address", "selector", "value_wei", "nonce", "gas_limit", "transaction_type", "gas_price_wei", "max_fee_per_gas_wei", "max_priority_fee_per_gas_wei", "request_fingerprint", "envelope_fingerprint", "calldata_sha256", "calldata_length")}
    expected = _request_fp(values)
    errors = [] if expected == request.signing_request_fingerprint else ["signing_request_fingerprint_mismatch"]
    if envelope is not None and isinstance(envelope, DreamDexUnsignedTransactionEnvelope) and (request.request_fingerprint != envelope.request_fingerprint or request.envelope_fingerprint != envelope.envelope_fingerprint): errors.append("envelope_request_binding_mismatch")
    return DreamDexSigningPolicyValidationResult(approved=False, blockers=tuple(errors), validation_errors=tuple(errors)) if errors else DreamDexSigningPolicyValidationResult(approved=request.policy_approved, structurally_complete=request.policy_approved, blockers=request.blockers, validation_errors=request.validation_errors)


def build_transaction_signing_preview(envelope: Any = None, *, policy: DreamDexTransactionSigningPolicy | None = None, request: DreamDexTransactionSigningRequest | None = None, validation: DreamDexSigningPolicyValidationResult | None = None, signer: DreamDexTransactionSigner | None = None) -> DreamDexTransactionSigningPreview:
    policy = policy or DreamDexTransactionSigningPolicy()
    if request is None and isinstance(envelope, DreamDexUnsignedTransactionEnvelope):
        request = build_transaction_signing_request(envelope, policy, signer_address=signer.get_address() if signer else None)
    if isinstance(envelope, DreamDexUnsignedTransactionEnvelope):
        validation = validation or validate_transaction_signing_policy(envelope, policy, signer_address=signer.get_address() if signer else None)
        return DreamDexTransactionSigningPreview(envelope.operation, envelope.chain_id, mask_evm_address(envelope.from_address), mask_evm_address(envelope.to_address), _selector(envelope), envelope.value_wei, "present" if envelope.nonce is not None else "unavailable", envelope.gas_limit, envelope.transaction_type, _max_fee(envelope), envelope.request_fingerprint, envelope.envelope_fingerprint, request.signing_request_fingerprint if request else None, signer.describe_capabilities().transaction_signing if signer else "unavailable", validation.approved, bool(request and request.ready_for_signer_invocation), False, validation.blockers)
    return DreamDexTransactionSigningPreview("unavailable", CHAIN_ID, "<missing>", mask_evm_address(POOL_ADDRESS), None, None, "unavailable", None, "unresolved", None, None, None, None, "unavailable", False, False, False, ("transaction_signing_request_unavailable", "transaction_signer_implementation_unavailable"))


def serialize_transaction_signing_diagnostics(value: Any) -> dict[str, Any]:
    if hasattr(value, "safe_dict"):
        return value.safe_dict()
    if isinstance(value, Mapping):
        return ensure_no_raw_sensitive_fields(dict(value))
    raise ValueError("diagnostics_type_invalid")


def describe_transaction_signer_capabilities() -> DreamDexTransactionSignerCapabilities:
    return UnavailableDreamDexTransactionSigner().describe_capabilities()


# concise aliases used by offline callers
build_signing_request = build_transaction_signing_request
validate_signing_request = validate_transaction_signing_request
build_signing_preview = build_transaction_signing_preview
validate_signing_policy = validate_transaction_signing_policy

__all__ = [name for name in globals() if name.startswith("DreamDex") or name.startswith("build_") or name.startswith("validate_") or name.startswith("serialize_") or name.startswith("describe_") or name in {"POOL_ADDRESS", "ALLOWED_OPERATIONS", "ALLOWED_SELECTORS", "ALLOWED_TARGET_ADDRESSES", "CHAIN_ID", "PLACE_SELECTOR", "CANCEL_SELECTOR", "REDUCE_SELECTOR", "UnavailableDreamDexTransactionSigner"}]
