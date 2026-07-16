"""Deterministic, offline unsigned transaction requests for DreamDEX.

This module stops at validation and redacted preview.  It has no transport,
provider, signer, key handling, subprocess, or environment access.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import re
from typing import Any, Mapping, Protocol

from bot.execution.dreamdex_direct_order_encoding import (
    CANCEL_SELECTOR,
    PLACE_SELECTOR,
    REDUCE_SELECTOR,
    DreamDexDirectOrderSpecification,
    build_cancel_order_call_preview,
    build_place_order_call_preview,
    build_reduce_order_call_preview,
)


CHAIN_ID = 5031
SCHEMA_VERSION = "1"
SUPPORTED_OPERATIONS = frozenset({"place_order", "cancel_order", "reduce_order"})
TRANSACTION_TYPE = "eip1559"
_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_SELECTORS = {"place_order": PLACE_SELECTOR, "cancel_order": CANCEL_SELECTOR, "reduce_order": REDUCE_SELECTOR}
_ZERO = "0x" + "0" * 40
MAX_UINT256 = (1 << 256) - 1


def _address(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _ADDRESS_RE.fullmatch(value):
        raise ValueError(f"{field}: invalid_address")
    return value.lower()


def _mask(value: str | None) -> str:
    if not value:
        return "<missing>"
    return value[:4] + "..." + value[-4:]


def _calldata_bytes(value: bytes | bytearray | str | None) -> bytes | None:
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    if isinstance(value, str) and value.startswith("0x") and len(value) % 2 == 0:
        try:
            return bytes.fromhex(value[2:])
        except ValueError:
            return None
    return None


def _calldata_hash(value: bytes | None) -> str | None:
    return sha256(value).hexdigest() if value is not None else None


def _safe_int(value: Any, field: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field}: invalid_nonnegative_integer")
    return value


@dataclass(frozen=True, repr=False)
class DreamDexUnsignedTransactionRequest:
    schema_version: str
    operation: str
    chain_id: int | None
    from_address: str | None
    to_address: str | None
    value_wei: int | None
    calldata: bytes | str | None
    calldata_sha256: str | None = None
    calldata_length: int | None = None
    gas_limit: int | None = None
    nonce: int | None = None
    max_fee_per_gas: int | None = None
    max_priority_fee_per_gas: int | None = None
    transaction_type: str | None = TRANSACTION_TYPE
    source_status: str = "unavailable"
    authoritative: bool = False
    unresolved_fields: tuple[str, ...] = ()
    validation_errors: tuple[str, ...] = ()
    value_status: str = "unavailable"
    input_asset_kind: str | None = None

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
            digest = _calldata_hash(raw)
            if self.calldata_sha256 is not None and self.calldata_sha256 != digest:
                raise ValueError("calldata: fingerprint_mismatch")
            object.__setattr__(self, "calldata_sha256", digest)
            if self.calldata_length is not None and self.calldata_length != len(raw):
                raise ValueError("calldata: length_mismatch")
            object.__setattr__(self, "calldata_length", len(raw))
        for field in ("chain_id", "value_wei", "gas_limit", "nonce", "max_fee_per_gas", "max_priority_fee_per_gas"):
            parsed = _safe_int(getattr(self, field), field)
            if field == "value_wei" and parsed is not None and parsed > MAX_UINT256:
                raise ValueError("value_wei: uint256_overflow")
        if self.authoritative:
            raise ValueError("unsigned request cannot be authoritative")

    def safe_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "operation": self.operation,
            "chain_id": self.chain_id,
            "from_address": _mask(self.from_address),
            "to_address": _mask(self.to_address),
            "value_wei": self.value_wei,
            "calldata_sha256": self.calldata_sha256,
            "calldata_length": self.calldata_length,
            "gas_limit": self.gas_limit,
            "nonce": self.nonce,
            "max_fee_per_gas": self.max_fee_per_gas,
            "max_priority_fee_per_gas": self.max_priority_fee_per_gas,
            "transaction_type": self.transaction_type,
            "source_status": self.source_status,
            "authoritative": False,
            "unresolved_fields": self.unresolved_fields,
            "validation_errors": self.validation_errors,
            "value_status": self.value_status,
            "input_asset_kind": self.input_asset_kind,
        }

    def __repr__(self) -> str:
        return f"DreamDexUnsignedTransactionRequest(operation={self.operation!r}, chain_id={self.chain_id!r}, from_address={_mask(self.from_address)!r}, to_address={_mask(self.to_address)!r}, calldata_sha256={self.calldata_sha256!r}, calldata_length={self.calldata_length!r}, authoritative=False)"


@dataclass(frozen=True, repr=False)
class DreamDexUnsignedTransactionPreview:
    operation: str
    chain_id: int | None
    from_address_masked: str
    to_address_masked: str
    value_wei: int | None
    calldata_length: int | None
    calldata_sha256: str | None
    calldata_selector: str | None
    gas_limit_status: str
    nonce_status: str
    fee_status: str
    ready_for_signing: bool
    ready_for_submission: bool
    blockers: tuple[str, ...]
    value_status: str = "unavailable"

    def safe_dict(self) -> dict[str, Any]:
        return {
            "operation": self.operation,
            "chain_id": self.chain_id,
            "from_address_masked": self.from_address_masked,
            "to_address_masked": self.to_address_masked,
            "value_wei": self.value_wei,
            "calldata_length": self.calldata_length,
            "calldata_sha256": self.calldata_sha256,
            "calldata_selector": self.calldata_selector,
            "gas_limit_status": self.gas_limit_status,
            "nonce_status": self.nonce_status,
            "fee_status": self.fee_status,
            "ready_for_signing": False,
            "ready_for_submission": False,
            "blockers": self.blockers,
            "value_status": self.value_status,
        }

    def __repr__(self) -> str:
        return f"DreamDexUnsignedTransactionPreview(operation={self.operation!r}, chain_id={self.chain_id!r}, from_address={self.from_address_masked!r}, to_address={self.to_address_masked!r}, calldata_selector={self.calldata_selector!r}, calldata_sha256={self.calldata_sha256!r}, ready_for_signing=False, ready_for_submission=False)"


@dataclass(frozen=True, repr=False)
class DreamDexUnsignedTransactionRequirements:
    required_chain_id: int | None
    required_from_address: str | None
    required_to_address: str | None
    value_policy: str
    calldata_policy: str
    gas_policy: str
    nonce_policy: str
    fee_policy: str
    signer_required: bool
    receipt_required: bool
    production_status: str
    unresolved_reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.required_from_address is not None:
            object.__setattr__(self, "required_from_address", _address(self.required_from_address, "required_from_address"))
        if self.required_to_address is not None:
            object.__setattr__(self, "required_to_address", _address(self.required_to_address, "required_to_address"))

    def safe_dict(self) -> dict[str, Any]:
        return {
            "required_chain_id": self.required_chain_id,
            "required_from_address": _mask(self.required_from_address),
            "required_to_address": _mask(self.required_to_address),
            "value_policy": self.value_policy,
            "calldata_policy": self.calldata_policy,
            "gas_policy": self.gas_policy,
            "nonce_policy": self.nonce_policy,
            "fee_policy": self.fee_policy,
            "signer_required": self.signer_required,
            "receipt_required": self.receipt_required,
            "production_status": self.production_status,
            "unresolved_reasons": self.unresolved_reasons,
        }

    def __repr__(self) -> str:
        return f"DreamDexUnsignedTransactionRequirements(chain_id={self.required_chain_id!r}, from_address={_mask(self.required_from_address)!r}, to_address={_mask(self.required_to_address)!r}, production_status={self.production_status!r})"


@dataclass(frozen=True)
class UnsignedTransactionValidationResult:
    valid: bool
    status: str
    errors: tuple[str, ...] = ()

    @property
    def approved(self) -> bool:
        return self.valid


def _target_values(to_address: str | None, pool_address: str | None, source_confirmed_pool_address: str | None) -> tuple[str | None, list[str]]:
    errors: list[str] = []
    target = to_address or pool_address
    # ``pool_address`` is the configured market pool input; callers that have
    # a separately recorded source-confirmed value can pass it explicitly.
    expected = source_confirmed_pool_address or pool_address
    if target is None:
        errors.append("pool_address_unavailable")
    else:
        try:
            target = _address(target, "to_address")
        except ValueError:
            target = None
            errors.append("invalid_to_address")
    if expected is None:
        errors.append("source_confirmed_pool_unavailable")
    else:
        try:
            expected = _address(expected, "source_confirmed_pool_address")
        except ValueError:
            expected = None
            errors.append("invalid_source_confirmed_pool")
    if target is not None and expected is not None and target != expected:
        errors.append("target_pool_mismatch")
    return target, errors


def _from_values(from_address: str | None, declared_signer_address: str | None, signer_address: str | None) -> tuple[str | None, list[str]]:
    errors: list[str] = []
    actual = from_address
    declared = declared_signer_address or signer_address
    if actual is None:
        errors.append("from_address_unavailable")
    else:
        try:
            actual = _address(actual, "from_address")
        except ValueError:
            actual = None
            errors.append("invalid_from_address")
    if declared is None:
        errors.append("direct_signer_declaration_unavailable")
    else:
        try:
            declared = _address(declared, "declared_signer_address")
        except ValueError:
            declared = None
            errors.append("invalid_declared_signer_address")
    if actual is not None and declared is not None and actual != declared:
        errors.append("from_signer_mismatch")
    return actual, errors


def _value_policy(operation: str, value_wei: int | None, *, input_asset_kind: str | None = None, native_requirement_wei: int | None = None) -> tuple[str, list[str]]:
    errors: list[str] = []
    if value_wei is None or isinstance(value_wei, bool) or not isinstance(value_wei, int) or not 0 <= value_wei <= MAX_UINT256:
        return "unavailable", ["invalid_value_wei"]
    if operation in {"cancel_order", "reduce_order"}:
        return ("zero_required", []) if value_wei == 0 else ("invalid_nonzero_value", ["nonzero_value_for_zero_operation"])
    if input_asset_kind == "erc20":
        return ("zero_required", []) if value_wei == 0 else ("invalid_nonzero_value", ["erc20_place_value_must_be_zero"])
    if input_asset_kind == "native":
        if native_requirement_wei is None:
            return "native_requirement_unavailable", ["native_requirement_unavailable"]
        if isinstance(native_requirement_wei, bool) or not isinstance(native_requirement_wei, int) or not 0 <= native_requirement_wei <= MAX_UINT256:
            return "native_requirement_unavailable", ["invalid_native_requirement"]
        return ("native_requirement_confirmed", []) if value_wei == native_requirement_wei else ("invalid_nonzero_value", ["native_value_mismatch"])
    return "unavailable", ["input_asset_kind_unavailable"]


def _request(
    *, operation: str, chain_id: int | None, from_address: str | None, to_address: str | None,
    value_wei: int | None, calldata: bytes | None, errors: list[str], value_status: str,
    input_asset_kind: str | None,
) -> DreamDexUnsignedTransactionRequest:
    unresolved = ["gas_limit", "nonce", "max_fee_per_gas", "max_priority_fee_per_gas", "signer_key", "receipt_access"]
    if not calldata:
        unresolved.append("calldata")
    safe_value = value_wei if isinstance(value_wei, int) and not isinstance(value_wei, bool) and 0 <= value_wei <= MAX_UINT256 else None
    return DreamDexUnsignedTransactionRequest(
        schema_version=SCHEMA_VERSION,
        operation=operation,
        chain_id=chain_id,
        from_address=from_address,
        to_address=to_address,
        value_wei=safe_value,
        calldata=calldata,
        gas_limit=None,
        nonce=None,
        max_fee_per_gas=None,
        max_priority_fee_per_gas=None,
        transaction_type=TRANSACTION_TYPE,
        source_status="source_confirmed" if not errors else "blocked",
        authoritative=False,
        unresolved_fields=tuple(dict.fromkeys(unresolved)),
        validation_errors=tuple(dict.fromkeys(errors)),
        value_status=value_status,
        input_asset_kind=input_asset_kind,
    )


def build_unsigned_place_order_request(
    spec: DreamDexDirectOrderSpecification,
    *,
    chain_id: int | None = CHAIN_ID,
    from_address: str | None = None,
    to_address: str | None = None,
    pool_address: str | None = None,
    source_confirmed_pool_address: str | None = None,
    declared_signer_address: str | None = None,
    signer_address: str | None = None,
    value_wei: int | None = None,
    input_asset_kind: str | None = None,
    native_requirement_wei: int | None = None,
    confirmed_native_requirement_wei: int | None = None,
) -> DreamDexUnsignedTransactionRequest:
    errors: list[str] = []
    if chain_id != CHAIN_ID:
        errors.append("chain_id_mismatch" if chain_id is not None else "chain_id_unavailable")
    target, target_errors = _target_values(to_address, pool_address, source_confirmed_pool_address)
    errors.extend(target_errors)
    actual_from, from_errors = _from_values(from_address, declared_signer_address, signer_address)
    errors.extend(from_errors)
    if not isinstance(spec, DreamDexDirectOrderSpecification):
        errors.append("invalid_order_specification")
        return _request(operation="place_order", chain_id=chain_id, from_address=actual_from, to_address=target, value_wei=value_wei, calldata=None, errors=errors, value_status="unavailable", input_asset_kind=input_asset_kind)
    if target is not None and spec.target_contract != target:
        errors.append("specification_target_mismatch")
    preview = build_place_order_call_preview(spec)
    errors.extend(preview.unresolved_reasons)
    native_requirement_wei = native_requirement_wei if native_requirement_wei is not None else confirmed_native_requirement_wei
    value_status, value_errors = _value_policy("place_order", value_wei, input_asset_kind=input_asset_kind, native_requirement_wei=native_requirement_wei)
    errors.extend(value_errors)
    calldata = preview._calldata
    if calldata is None:
        errors.append("calldata_unavailable")
    return _request(operation="place_order", chain_id=chain_id, from_address=actual_from, to_address=target, value_wei=value_wei, calldata=calldata, errors=errors, value_status=value_status, input_asset_kind=input_asset_kind)


def build_unsigned_cancel_order_request(
    *, order_id: int | None, chain_id: int | None = CHAIN_ID, from_address: str | None = None,
    to_address: str | None = None, pool_address: str | None = None,
    source_confirmed_pool_address: str | None = None, declared_signer_address: str | None = None,
    signer_address: str | None = None,
) -> DreamDexUnsignedTransactionRequest:
    errors: list[str] = []
    if chain_id != CHAIN_ID:
        errors.append("chain_id_mismatch" if chain_id is not None else "chain_id_unavailable")
    target, target_errors = _target_values(to_address, pool_address, source_confirmed_pool_address)
    errors.extend(target_errors)
    actual_from, from_errors = _from_values(from_address, declared_signer_address, signer_address)
    errors.extend(from_errors)
    valid_order_id = order_id is not None and not isinstance(order_id, bool) and isinstance(order_id, int) and 0 <= order_id <= (1 << 128) - 1
    if not valid_order_id:
        errors.append("order_id_uint128_invalid")
    calldata = None
    if valid_order_id:
        preview = build_cancel_order_call_preview(target_contract=target, order_id=order_id, signer_subject=actual_from)
        errors.extend(preview.unresolved_reasons)
        calldata = preview._calldata
        if calldata is None:
            errors.append("calldata_unavailable")
    return _request(operation="cancel_order", chain_id=chain_id, from_address=actual_from, to_address=target, value_wei=0, calldata=calldata, errors=errors, value_status="zero_required", input_asset_kind=None)


def build_unsigned_reduce_order_request(
    *, order_id: int | None, reduce_quantity: int | None = None, new_quantity_remaining: int | None = None,
    chain_id: int | None = CHAIN_ID, from_address: str | None = None, to_address: str | None = None,
    pool_address: str | None = None, source_confirmed_pool_address: str | None = None,
    declared_signer_address: str | None = None, signer_address: str | None = None,
) -> DreamDexUnsignedTransactionRequest:
    errors: list[str] = []
    if chain_id != CHAIN_ID:
        errors.append("chain_id_mismatch" if chain_id is not None else "chain_id_unavailable")
    target, target_errors = _target_values(to_address, pool_address, source_confirmed_pool_address)
    errors.extend(target_errors)
    actual_from, from_errors = _from_values(from_address, declared_signer_address, signer_address)
    errors.extend(from_errors)
    quantity = reduce_quantity if reduce_quantity is not None else new_quantity_remaining
    valid_order_id = order_id is not None and not isinstance(order_id, bool) and isinstance(order_id, int) and 0 <= order_id <= (1 << 128) - 1
    valid_quantity = quantity is not None and not isinstance(quantity, bool) and isinstance(quantity, int) and 0 < quantity <= (1 << 256) - 1
    if not valid_order_id:
        errors.append("order_id_uint128_invalid")
    if not valid_quantity:
        errors.append("reduce_quantity_uint256_invalid")
    calldata = None
    if valid_order_id and valid_quantity:
        preview = build_reduce_order_call_preview(target_contract=target, order_id=order_id, new_quantity_remaining=quantity, signer_subject=actual_from)
        errors.extend(preview.unresolved_reasons)
        calldata = preview._calldata
        if calldata is None:
            errors.append("calldata_unavailable")
    return _request(operation="reduce_order", chain_id=chain_id, from_address=actual_from, to_address=target, value_wei=0, calldata=calldata, errors=errors, value_status="zero_required", input_asset_kind=None)


def validate_unsigned_transaction_request(
    request: DreamDexUnsignedTransactionRequest,
    *, expected_pool_address: str | None = None,
    declared_signer_address: str | None = None,
) -> UnsignedTransactionValidationResult:
    errors = list(request.validation_errors)
    if request.operation not in SUPPORTED_OPERATIONS:
        errors.append("unsupported_operation")
    if request.chain_id != CHAIN_ID:
        errors.append("chain_id_mismatch" if request.chain_id is not None else "chain_id_unavailable")
    if request.from_address is None:
        errors.append("from_address_unavailable")
    if declared_signer_address is not None and request.from_address is not None:
        try:
            if request.from_address != _address(declared_signer_address, "declared_signer_address"):
                errors.append("from_signer_mismatch")
        except ValueError:
            errors.append("invalid_declared_signer_address")
    if request.to_address is None:
        errors.append("to_address_unavailable")
    if expected_pool_address is not None and request.to_address is not None:
        try:
            if request.to_address != _address(expected_pool_address, "expected_pool_address"):
                errors.append("target_pool_mismatch")
        except ValueError:
            errors.append("invalid_expected_pool")
    raw = _calldata_bytes(request.calldata)
    selector = "0x" + raw[:4].hex() if raw is not None and len(raw) >= 4 else None
    expected_selector = _SELECTORS.get(request.operation)
    if selector is None:
        errors.append("calldata_unavailable")
    elif expected_selector != selector:
        errors.append("selector_operation_mismatch")
    if request.operation in {"cancel_order", "reduce_order"} and request.value_wei != 0:
        errors.append("nonzero_value_for_zero_operation")
    if request.value_wei is None:
        errors.append("value_unavailable")
    if request.value_wei is not None and request.value_wei < 0:
        errors.append("negative_value")
    unique = tuple(dict.fromkeys(errors))
    return UnsignedTransactionValidationResult(not unique, "valid" if not unique else "blocked", unique)


def build_unsigned_transaction_preview(request: DreamDexUnsignedTransactionRequest) -> DreamDexUnsignedTransactionPreview:
    validation = validate_unsigned_transaction_request(request)
    blockers = list(validation.errors)
    blockers.extend(request.unresolved_fields)
    blockers.extend(("direct_transaction_signing_unimplemented", "direct_transaction_submission_unimplemented", "receipt_access_unavailable"))
    raw = _calldata_bytes(request.calldata)
    selector = "0x" + raw[:4].hex() if raw is not None and len(raw) >= 4 else None
    return DreamDexUnsignedTransactionPreview(
        operation=request.operation,
        chain_id=request.chain_id,
        from_address_masked=_mask(request.from_address),
        to_address_masked=_mask(request.to_address),
        value_wei=request.value_wei,
        calldata_length=request.calldata_length,
        calldata_sha256=request.calldata_sha256,
        calldata_selector=selector,
        gas_limit_status="confirmed" if request.gas_limit is not None else "unresolved",
        nonce_status="confirmed" if request.nonce is not None else "unresolved",
        fee_status="confirmed" if request.max_fee_per_gas is not None and request.max_priority_fee_per_gas is not None else "unresolved",
        ready_for_signing=False,
        ready_for_submission=False,
        blockers=tuple(dict.fromkeys(blockers)),
        value_status=request.value_status,
    )


def build_unsigned_transaction_requirements(*, operation: str, from_address: str | None, to_address: str | None) -> DreamDexUnsignedTransactionRequirements:
    if operation not in SUPPORTED_OPERATIONS:
        raise ValueError("unsupported_operation")
    value_policy = "native_requirement_or_zero_for_erc20" if operation == "place_order" else "zero_required"
    return DreamDexUnsignedTransactionRequirements(
        required_chain_id=CHAIN_ID,
        required_from_address=from_address,
        required_to_address=to_address,
        value_policy=value_policy,
        calldata_policy="exact_source_confirmed_selector_and_abi",
        gas_policy="unresolved_no_provider_lookup",
        nonce_policy="unresolved_no_provider_lookup",
        fee_policy="unresolved_no_provider_lookup",
        signer_required=True,
        receipt_required=True,
        production_status="unavailable",
        unresolved_reasons=("direct_signer_key_unavailable", "direct_transaction_transport_unimplemented", "direct_order_reconciliation_unavailable"),
    )


class DreamDexTransactionTransport(Protocol):
    def describe_capabilities(self) -> Mapping[str, str]: ...
    def validate_request(self, request: DreamDexUnsignedTransactionRequest) -> UnsignedTransactionValidationResult: ...


class UnavailableDreamDexTransactionTransport:
    """Capability-only boundary; intentionally has no runtime transaction API."""

    def describe_capabilities(self) -> Mapping[str, str]:
        return {
            "build_unsigned_place": "available_offline",
            "build_unsigned_cancel": "available_offline",
            "build_unsigned_reduce": "available_offline",
            "validate_unsigned_request": "available_offline",
            "preview_unsigned_request": "available_offline",
            "sign_transaction": "unavailable",
            "submit_transaction": "unavailable",
            "wait_for_receipt": "unavailable",
        }

    def validate_request(self, request: DreamDexUnsignedTransactionRequest) -> UnsignedTransactionValidationResult:
        return validate_unsigned_transaction_request(request)


# Short aliases keep the API discoverable without introducing another
# execution surface.
build_unsigned_cancel_request = build_unsigned_cancel_order_request
build_unsigned_reduce_request = build_unsigned_reduce_order_request
build_unsigned_place_request = build_unsigned_place_order_request
validate_unsigned_request = validate_unsigned_transaction_request
preview_unsigned_request = build_unsigned_transaction_preview


__all__ = [
    "CHAIN_ID", "SCHEMA_VERSION", "SUPPORTED_OPERATIONS", "PLACE_SELECTOR", "CANCEL_SELECTOR", "REDUCE_SELECTOR",
    "DreamDexUnsignedTransactionRequest", "DreamDexUnsignedTransactionPreview", "DreamDexUnsignedTransactionRequirements",
    "UnsignedTransactionValidationResult", "DreamDexTransactionTransport", "UnavailableDreamDexTransactionTransport",
    "build_unsigned_place_order_request", "build_unsigned_cancel_order_request", "build_unsigned_reduce_order_request",
    "build_unsigned_place_request", "build_unsigned_cancel_request", "build_unsigned_reduce_request",
    "validate_unsigned_transaction_request", "validate_unsigned_request", "build_unsigned_transaction_preview", "preview_unsigned_request", "build_unsigned_transaction_requirements",
]
