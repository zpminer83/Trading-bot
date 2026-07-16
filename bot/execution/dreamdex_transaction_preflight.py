"""Pure transaction preflight orchestration for DreamDEX.

The module accepts only the existing typed unsigned envelope and a typed
read-only RPC protocol.  It resolves ephemeral nonce/gas/fee evidence and
builds a new unsigned finalized envelope, but never signs, reserves, persists,
submits, polls, or mutates the original envelope.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import Any, Mapping, Sequence

from bot.execution.dreamdex_readonly_rpc import DreamDexReadOnlyRpc, DreamDexRpcError
from bot.execution.dreamdex_transaction_envelope import (
    DreamDexTransactionEnvelopeEvidence,
    DreamDexUnsignedTransactionEnvelope,
    build_unsigned_transaction_envelope,
    validate_unsigned_transaction_envelope,
)
from bot.execution.dreamdex_unsigned_transaction import CHAIN_ID, MAX_UINT256, DreamDexUnsignedTransactionRequest
from bot.execution.dreamdex_execution_primitives import deterministic_fingerprint, mask_evm_address, mask_hex_hash, ensure_no_raw_sensitive_fields

PREFLIGHT_SCHEMA_VERSION = "1"
PREFLIGHT_STATUS_VALUES = frozenset({"disabled", "unavailable", "blocked", "completed", "failed"})
TRANSACTION_TYPE_VALUES = frozenset({"legacy", "eip1559", "unresolved"})
POOL_ADDRESS = "0x035de7403eac6872787779cca7ccf1b4cdb61379"


def _uint(value: Any, field: str, *, allow_none: bool = True, positive: bool = False, maximum: int = MAX_UINT256) -> int | None:
    if value is None and allow_none:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < (1 if positive else 0) or value > maximum:
        raise ValueError(f"{field}: invalid_uint256")
    return value


def _ceil_bps(value: int, multiplier_bps: int, *, field: str) -> int:
    _uint(value, field, allow_none=False)
    _uint(multiplier_bps, f"{field}_multiplier_bps", allow_none=False)
    product = value * multiplier_bps
    if product > MAX_UINT256 * 10000:
        raise ValueError(f"{field}: multiplication_overflow")
    result = (product + 9999) // 10000
    if result > MAX_UINT256:
        raise ValueError(f"{field}: uint256_overflow")
    return result


def calculate_gas_limit(gas_estimate: int, gas_headroom_bps: int, maximum_gas_limit: int) -> int:
    if isinstance(gas_headroom_bps, bool) or not isinstance(gas_headroom_bps, int) or gas_headroom_bps < 10000:
        raise ValueError("gas_headroom_bps: invalid")
    _uint(gas_estimate, "gas_estimate", allow_none=False, positive=True)
    _uint(maximum_gas_limit, "maximum_gas_limit", allow_none=False, positive=True)
    result = _ceil_bps(gas_estimate, gas_headroom_bps, field="gas_limit")
    if result > maximum_gas_limit:
        raise ValueError("gas_limit_policy_exceeded")
    return result


def calculate_legacy_gas_price(gas_price: int, multiplier_bps: int) -> int:
    result = _ceil_bps(_uint(gas_price, "gas_price", allow_none=False, positive=True), multiplier_bps, field="gas_price")
    if result <= 0:
        raise ValueError("gas_price_zero")
    return result


def calculate_eip1559_max_fee(base_fee: int, multiplier_bps: int, priority_fee: int) -> int:
    _uint(base_fee, "base_fee", allow_none=False)
    _uint(priority_fee, "priority_fee", allow_none=False, positive=True)
    max_fee = _ceil_bps(base_fee, multiplier_bps, field="base_fee") + priority_fee
    if max_fee > MAX_UINT256:
        raise ValueError("max_fee_uint256_overflow")
    if max_fee < priority_fee:
        raise ValueError("max_fee_below_priority_fee")
    return max_fee


def calculate_total_fee(gas_limit: int, fee_per_gas: int) -> int:
    _uint(gas_limit, "gas_limit", allow_none=False, positive=True)
    _uint(fee_per_gas, "fee_per_gas", allow_none=False, positive=True)
    total = gas_limit * fee_per_gas
    if total > MAX_UINT256:
        raise ValueError("total_fee_uint256_overflow")
    return total


def calculate_required_native_balance(value_wei: int, maximum_possible_fee_wei: int) -> int:
    _uint(value_wei, "value_wei", allow_none=False)
    _uint(maximum_possible_fee_wei, "maximum_possible_fee_wei", allow_none=False)
    total = value_wei + maximum_possible_fee_wei
    if total > MAX_UINT256:
        raise ValueError("required_native_balance_overflow")
    return total


@dataclass(frozen=True, repr=False)
class DreamDexTransactionPreflightPolicy:
    schema_version: str = PREFLIGHT_SCHEMA_VERSION
    required_chain_id: int = CHAIN_ID
    required_sender_address: str | None = None
    required_target_address: str | None = POOL_ADDRESS
    maximum_gas_limit: int | None = None
    maximum_total_fee_wei: int | None = None
    gas_headroom_bps: int | None = None
    legacy_gas_multiplier_bps: int | None = None
    base_fee_multiplier_bps: int | None = None
    maximum_priority_fee_per_gas_wei: int | None = None
    require_target_contract_code: bool = True
    require_pending_nonce: bool = True
    require_gas_estimate: bool = True
    require_fee_model: bool = True
    require_native_balance_check: bool = True
    authoritative: bool = False
    unresolved_reasons: tuple[str, ...] = ("rpc_configuration_unavailable", "policy_limits_unresolved")

    def __post_init__(self) -> None:
        if self.required_chain_id != CHAIN_ID:
            raise ValueError("required_chain_id: unsupported")
        for field in ("maximum_gas_limit", "maximum_total_fee_wei", "gas_headroom_bps", "legacy_gas_multiplier_bps", "base_fee_multiplier_bps", "maximum_priority_fee_per_gas_wei"):
            value = getattr(self, field)
            if value is not None:
                _uint(value, field)
        if self.maximum_gas_limit is not None and self.maximum_gas_limit <= 0:
            raise ValueError("maximum_gas_limit: must_be_positive")
        if self.maximum_total_fee_wei is not None and self.maximum_total_fee_wei <= 0:
            raise ValueError("maximum_total_fee_wei: must_be_positive")
        if self.gas_headroom_bps is not None and self.gas_headroom_bps < 10000:
            raise ValueError("gas_headroom_bps: must_be_at_least_10000")
        if self.legacy_gas_multiplier_bps is not None and self.legacy_gas_multiplier_bps <= 0:
            raise ValueError("legacy_gas_multiplier_bps: must_be_positive")
        if self.base_fee_multiplier_bps is not None and self.base_fee_multiplier_bps <= 0:
            raise ValueError("base_fee_multiplier_bps: must_be_positive")
        if self.maximum_priority_fee_per_gas_wei is not None and self.maximum_priority_fee_per_gas_wei <= 0:
            raise ValueError("maximum_priority_fee_per_gas_wei: must_be_positive")
        for field in ("required_sender_address", "required_target_address"):
            value = getattr(self, field)
            if value is not None:
                if not isinstance(value, str) or len(value) != 42 or not value.startswith("0x") or any(char not in "0123456789abcdefABCDEF" for char in value[2:]):
                    raise ValueError(f"{field}: invalid_address")
                object.__setattr__(self, field, value.lower())
        if self.required_target_address is not None and self.required_target_address != POOL_ADDRESS:
            raise ValueError("required_target_address: source_confirmed_pool_required")
        object.__setattr__(self, "unresolved_reasons", tuple(dict.fromkeys(self.unresolved_reasons)))

    def safe_dict(self) -> dict[str, Any]:
        return {"schema_version": self.schema_version, "required_chain_id": self.required_chain_id, "required_sender_address_masked": mask_evm_address(self.required_sender_address), "required_target_address_masked": mask_evm_address(self.required_target_address), "maximum_gas_limit": self.maximum_gas_limit, "maximum_total_fee_wei": self.maximum_total_fee_wei, "gas_headroom_bps": self.gas_headroom_bps, "legacy_gas_multiplier_bps": self.legacy_gas_multiplier_bps, "base_fee_multiplier_bps": self.base_fee_multiplier_bps, "maximum_priority_fee_per_gas_wei": self.maximum_priority_fee_per_gas_wei, "authoritative": False, "unresolved_reasons": self.unresolved_reasons}

    def __repr__(self) -> str:
        return f"DreamDexTransactionPreflightPolicy(chain_id={self.required_chain_id}, sender={mask_evm_address(self.required_sender_address)!r}, target={mask_evm_address(self.required_target_address)!r}, authoritative=False)"


@dataclass(frozen=True, repr=False)
class DreamDexRpcFeeEvidence:
    latest_block_number: int | None
    base_fee_per_gas_wei: int | None
    gas_price_wei: int | None
    priority_fee_per_gas_wei: int | None
    fee_history_available: bool
    eip1559_supported: bool
    source_status: str
    authoritative: bool = False
    unresolved_reasons: tuple[str, ...] = ()

    def safe_dict(self) -> dict[str, Any]:
        return {"latest_block_number": self.latest_block_number, "base_fee_present": self.base_fee_per_gas_wei is not None, "gas_price_present": self.gas_price_wei is not None, "priority_fee_present": self.priority_fee_per_gas_wei is not None, "fee_history_available": self.fee_history_available, "eip1559_supported": self.eip1559_supported, "source_status": self.source_status, "authoritative": False, "unresolved_reasons": self.unresolved_reasons}

    def __repr__(self) -> str:
        return f"DreamDexRpcFeeEvidence(block={self.latest_block_number!r}, eip1559={self.eip1559_supported!r}, source_status={self.source_status!r})"


@dataclass(frozen=True, repr=False)
class DreamDexTransactionPreflightEvidence:
    chain_id: int | None
    chain_match: bool | None
    target_code_status: str
    pending_nonce: int | None
    gas_estimate: int | None
    native_balance_wei: int | None
    fee_evidence: DreamDexRpcFeeEvidence
    call_revert_status: str
    source_status: str
    authoritative: bool = False
    conflicts: tuple[str, ...] = ()
    unresolved_reasons: tuple[str, ...] = ()
    target_code_byte_length: int | None = None
    target_code_sha256: str | None = None

    def safe_dict(self) -> dict[str, Any]:
        return {"chain_id": self.chain_id, "chain_match": self.chain_match, "target_code_status": self.target_code_status, "target_code_byte_length": self.target_code_byte_length, "target_code_sha256": mask_hex_hash(self.target_code_sha256), "pending_nonce_status": "present" if self.pending_nonce is not None else "unavailable", "gas_estimate": self.gas_estimate, "native_balance_status": "available" if self.native_balance_wei is not None else "unavailable", "fee_evidence": self.fee_evidence.safe_dict(), "call_revert_status": self.call_revert_status, "source_status": self.source_status, "authoritative": False, "conflicts": self.conflicts, "unresolved_reasons": self.unresolved_reasons}

    def __repr__(self) -> str:
        return f"DreamDexTransactionPreflightEvidence(chain_id={self.chain_id!r}, chain_match={self.chain_match!r}, target_code_status={self.target_code_status!r}, source_status={self.source_status!r})"


@dataclass(frozen=True, repr=False)
class DreamDexResolvedTransactionParameters:
    transaction_type: str
    nonce: int | None
    gas_estimate: int | None
    gas_limit: int | None
    gas_headroom_bps: int | None
    gas_price_wei: int | None
    max_fee_per_gas_wei: int | None
    max_priority_fee_per_gas_wei: int | None
    maximum_possible_fee_wei: int | None
    required_native_balance_wei: int | None
    native_balance_sufficient: bool | None
    parameter_fingerprint: str | None
    authoritative: bool = False
    blockers: tuple[str, ...] = ()
    validation_errors: tuple[str, ...] = ()

    def safe_dict(self) -> dict[str, Any]:
        return {"transaction_type": self.transaction_type, "nonce_status": "present" if self.nonce is not None else "unavailable", "gas_estimate": self.gas_estimate, "gas_limit": self.gas_limit, "gas_headroom_bps": self.gas_headroom_bps, "gas_price_wei": self.gas_price_wei, "max_fee_per_gas_wei": self.max_fee_per_gas_wei, "max_priority_fee_per_gas_wei": self.max_priority_fee_per_gas_wei, "maximum_possible_fee_wei": self.maximum_possible_fee_wei, "required_native_balance_wei": self.required_native_balance_wei, "native_balance_sufficient": self.native_balance_sufficient, "parameter_fingerprint": mask_hex_hash(self.parameter_fingerprint), "authoritative": False, "blockers": self.blockers, "validation_errors": self.validation_errors}

    def __repr__(self) -> str:
        return f"DreamDexResolvedTransactionParameters(transaction_type={self.transaction_type!r}, nonce={self.nonce!r}, gas_limit={self.gas_limit!r}, parameter_fingerprint={mask_hex_hash(self.parameter_fingerprint)!r})"


@dataclass(frozen=True, repr=False)
class DreamDexTransactionPreflightResult:
    schema_version: str
    preflight_status: str
    original_envelope_fingerprint: str | None
    evidence: DreamDexTransactionPreflightEvidence
    resolved_parameters: DreamDexResolvedTransactionParameters
    finalized_envelope: DreamDexUnsignedTransactionEnvelope | None
    finalized_envelope_fingerprint: str | None
    policy_compliant: bool
    ready_for_signing_policy_review: bool
    ready_for_signer_invocation: bool
    preflight_fingerprint: str
    authoritative: bool
    blockers: tuple[str, ...]
    validation_errors: tuple[str, ...]

    def safe_dict(self) -> dict[str, Any]:
        return {"schema_version": self.schema_version, "preflight_status": self.preflight_status, "original_envelope_fingerprint": mask_hex_hash(self.original_envelope_fingerprint), "evidence": self.evidence.safe_dict(), "resolved_parameters": self.resolved_parameters.safe_dict(), "finalized_envelope_available": self.finalized_envelope is not None, "finalized_envelope_fingerprint": mask_hex_hash(self.finalized_envelope_fingerprint), "policy_compliant": self.policy_compliant, "ready_for_signing_policy_review": self.ready_for_signing_policy_review, "ready_for_signer_invocation": False, "preflight_fingerprint": mask_hex_hash(self.preflight_fingerprint), "authoritative": self.authoritative, "blockers": self.blockers, "validation_errors": self.validation_errors}

    def __repr__(self) -> str:
        return f"DreamDexTransactionPreflightResult(status={self.preflight_status!r}, policy_compliant={self.policy_compliant!r}, finalized_envelope_available={self.finalized_envelope is not None}, authoritative={self.authoritative!r})"


@dataclass(frozen=True, repr=False)
class DreamDexTransactionPreflightPreview:
    preflight_status: str
    network_execution_performed: bool
    chain_id: int | None
    chain_match: bool | None
    target_code_status: str
    nonce_status: str
    gas_estimate_status: str
    gas_limit: int | None
    fee_mode: str
    maximum_possible_fee_wei: int | None
    native_balance_status: str
    native_balance_sufficient: bool | None
    original_envelope_fingerprint: str | None
    finalized_envelope_fingerprint: str | None
    preflight_fingerprint: str | None
    policy_compliant: bool
    ready_for_signing_policy_review: bool
    signer_invocation_allowed: bool
    transaction_submission_allowed: bool
    blockers: tuple[str, ...]

    def safe_dict(self) -> dict[str, Any]:
        return {"preflight_status": self.preflight_status, "network_execution_performed": self.network_execution_performed, "chain_id": self.chain_id, "chain_match": self.chain_match, "target_code_status": self.target_code_status, "nonce_status": self.nonce_status, "gas_estimate_status": self.gas_estimate_status, "gas_limit": self.gas_limit, "fee_mode": self.fee_mode, "maximum_possible_fee_wei": self.maximum_possible_fee_wei, "native_balance_status": self.native_balance_status, "native_balance_sufficient": self.native_balance_sufficient, "original_envelope_fingerprint": mask_hex_hash(self.original_envelope_fingerprint), "finalized_envelope_fingerprint": mask_hex_hash(self.finalized_envelope_fingerprint), "preflight_fingerprint": mask_hex_hash(self.preflight_fingerprint), "policy_compliant": self.policy_compliant, "ready_for_signing_policy_review": self.ready_for_signing_policy_review, "signer_invocation_allowed": False, "transaction_submission_allowed": False, "blockers": self.blockers}

    def __repr__(self) -> str:
        return f"DreamDexTransactionPreflightPreview(status={self.preflight_status!r}, chain_match={self.chain_match!r}, policy_compliant={self.policy_compliant!r}, transaction_submission_allowed=False)"


def _calldata_hex(value: bytes | str | None) -> str:
    if isinstance(value, bytes):
        return "0x" + value.hex()
    if isinstance(value, str) and value.startswith("0x"):
        return value.lower()
    raise ValueError("calldata_unavailable")


def _policy_blockers(policy: DreamDexTransactionPreflightPolicy) -> list[str]:
    missing = []
    if policy.required_sender_address is None:
        missing.append("direct_signer_address_unconfigured")
    if policy.maximum_gas_limit is None or policy.gas_headroom_bps is None:
        missing.append("gas_limit_policy_unresolved")
    if policy.maximum_total_fee_wei is None or policy.legacy_gas_multiplier_bps is None or policy.base_fee_multiplier_bps is None:
        missing.append("transaction_fee_limit_unresolved")
    if policy.maximum_priority_fee_per_gas_wei is None:
        missing.append("fee_priority_limit_unresolved")
    return missing


def _empty_fee(*reasons: str) -> DreamDexRpcFeeEvidence:
    return DreamDexRpcFeeEvidence(None, None, None, None, False, False, "unavailable", False, tuple(dict.fromkeys(reasons)))


def _fingerprint_result(original: str | None, evidence: DreamDexTransactionPreflightEvidence, params: DreamDexResolvedTransactionParameters, policy: DreamDexTransactionPreflightPolicy, blockers: Sequence[str], errors: Sequence[str]) -> str:
    body = {"schema_version": PREFLIGHT_SCHEMA_VERSION, "original_envelope_fingerprint": original, "chain_id": evidence.chain_id, "chain_match": evidence.chain_match, "target_code_status": evidence.target_code_status, "target_code_byte_length": evidence.target_code_byte_length, "target_code_sha256": evidence.target_code_sha256, "pending_nonce": evidence.pending_nonce, "gas_estimate": evidence.gas_estimate, "gas_limit": params.gas_limit, "transaction_type": params.transaction_type, "gas_price_wei": params.gas_price_wei, "max_fee_per_gas_wei": params.max_fee_per_gas_wei, "max_priority_fee_per_gas_wei": params.max_priority_fee_per_gas_wei, "maximum_possible_fee_wei": params.maximum_possible_fee_wei, "native_balance_sufficient": params.native_balance_sufficient, "policy": policy.safe_dict(), "blockers": tuple(sorted(set(blockers))), "validation_errors": tuple(sorted(set(errors)))}
    return deterministic_fingerprint(body, domain="dreamdex/transaction-preflight", schema_version=PREFLIGHT_SCHEMA_VERSION)


def _result(envelope: DreamDexUnsignedTransactionEnvelope | None, policy: DreamDexTransactionPreflightPolicy, *, status: str, evidence: DreamDexTransactionPreflightEvidence, params: DreamDexResolvedTransactionParameters, blockers: Sequence[str] = (), errors: Sequence[str] = (), finalized: DreamDexUnsignedTransactionEnvelope | None = None) -> DreamDexTransactionPreflightResult:
    all_blockers = tuple(dict.fromkeys((*blockers, *params.blockers, *evidence.unresolved_reasons, *policy.unresolved_reasons)))
    all_errors = tuple(dict.fromkeys((*errors, *params.validation_errors)))
    policy_ok = not any(item != "pending_nonce_snapshot_not_reserved" for item in all_blockers) and not all_errors and params.native_balance_sufficient is True
    source_confirmed = evidence.source_status == "source_confirmed" and evidence.authoritative
    authoritative = bool(policy.authoritative and policy_ok and source_confirmed and not evidence.conflicts and not policy.unresolved_reasons)
    fingerprint = _fingerprint_result(envelope.envelope_fingerprint if envelope else None, evidence, params, policy, all_blockers, all_errors)
    return DreamDexTransactionPreflightResult(PREFLIGHT_SCHEMA_VERSION, status, envelope.envelope_fingerprint if envelope else None, evidence, params, finalized, finalized.envelope_fingerprint if finalized else None, policy_ok, policy_ok and finalized is not None, False, fingerprint, authoritative, all_blockers, all_errors)


def unavailable_preflight_result(envelope: Any = None, policy: DreamDexTransactionPreflightPolicy | None = None, *, reason: str = "live_transaction_preflight_unavailable") -> DreamDexTransactionPreflightResult:
    policy = policy or DreamDexTransactionPreflightPolicy()
    original = envelope.envelope_fingerprint if isinstance(envelope, DreamDexUnsignedTransactionEnvelope) else None
    evidence = DreamDexTransactionPreflightEvidence(None, None, "unavailable", None, None, None, _empty_fee(reason), "not_performed", "unavailable", False, (), (reason,))
    params = DreamDexResolvedTransactionParameters("unresolved", None, None, None, policy.gas_headroom_bps, None, None, None, None, None, None, None, False, (reason,), ())
    return _result(envelope if isinstance(envelope, DreamDexUnsignedTransactionEnvelope) else None, policy, status="unavailable", evidence=evidence, params=params, blockers=(reason,))


def _request_for_envelope(envelope: DreamDexUnsignedTransactionEnvelope) -> DreamDexUnsignedTransactionRequest:
    return DreamDexUnsignedTransactionRequest(schema_version=envelope.schema_version, operation=envelope.operation, chain_id=envelope.chain_id, from_address=envelope.from_address, to_address=envelope.to_address, value_wei=envelope.value_wei, calldata=envelope.calldata, source_status="source_confirmed", authoritative=False, validation_errors=(), unresolved_fields=())


def _build_finalized_envelope(envelope: DreamDexUnsignedTransactionEnvelope, *, nonce: int, gas_limit: int, transaction_type: str, gas_price: int | None, max_fee: int | None, priority: int | None) -> DreamDexUnsignedTransactionEnvelope:
    request = _request_for_envelope(envelope)
    evidence = DreamDexTransactionEnvelopeEvidence(source_type="external_manual", source_status="source_confirmed", chain_id_status="source_confirmed", nonce_status="source_confirmed", gas_limit_status="source_confirmed", transaction_type_status="source_confirmed", fee_status="source_confirmed", base_fee_status="source_confirmed", priority_fee_status="source_confirmed", max_fee_status="source_confirmed")
    return build_unsigned_transaction_envelope(request, nonce=nonce, gas_limit=gas_limit, transaction_type=transaction_type, gas_price_wei=gas_price, max_fee_per_gas_wei=max_fee, max_priority_fee_per_gas_wei=priority, evidence=evidence)


def run_transaction_preflight(envelope: Any, rpc: DreamDexReadOnlyRpc, policy: DreamDexTransactionPreflightPolicy) -> DreamDexTransactionPreflightResult:
    if not isinstance(envelope, DreamDexUnsignedTransactionEnvelope):
        return unavailable_preflight_result(reason="envelope_type_invalid", policy=policy)
    if not isinstance(policy, DreamDexTransactionPreflightPolicy):
        raise ValueError("policy_type_invalid")
    structural = validate_unsigned_transaction_envelope(envelope)
    # Preflight is specifically allowed to resolve these fields; all other
    # envelope integrity errors remain fail-closed.
    resolvable = {"transaction_nonce_unresolved", "transaction_gas_unresolved", "transaction_fees_unresolved", "transaction_type_policy_unresolved"}
    common_errors = [error for error in structural.errors if error not in resolvable]
    blockers = _policy_blockers(policy)
    if policy.required_target_address and envelope.to_address != policy.required_target_address:
        blockers.append("target_address_mismatch")
    if policy.required_sender_address and envelope.from_address != policy.required_sender_address:
        blockers.append("sender_address_mismatch")
    evidence = DreamDexTransactionPreflightEvidence(None, None, "unavailable", None, None, None, _empty_fee(), "not_attempted", "unavailable", False, (), tuple(common_errors))
    params = DreamDexResolvedTransactionParameters("unresolved", None, None, None, policy.gas_headroom_bps, None, None, None, None, None, None, None, False, tuple(blockers), tuple(common_errors))
    if common_errors:
        return _result(envelope, policy, status="blocked", evidence=evidence, params=params, blockers=blockers)
    try:
        chain = _uint(rpc.get_chain_id(), "chain_id", allow_none=False)
    except Exception as exc:
        evidence = DreamDexTransactionPreflightEvidence(None, None, "unavailable", None, None, None, _empty_fee("rpc_chain_unavailable"), "not_attempted", "failed", False, (), ("rpc_chain_unavailable",))
        params = DreamDexResolvedTransactionParameters("unresolved", None, None, None, policy.gas_headroom_bps, None, None, None, None, None, None, None, False, tuple((*blockers, "rpc_chain_unavailable")), ())
        return _result(envelope, policy, status="failed", evidence=evidence, params=params)
    chain_match = chain == policy.required_chain_id
    if not chain_match:
        evidence = DreamDexTransactionPreflightEvidence(chain, False, "not_attempted", None, None, None, _empty_fee("rpc_chain_mismatch"), "not_attempted", "source_observed", False, (), ("rpc_chain_mismatch",))
        params = DreamDexResolvedTransactionParameters("unresolved", None, None, None, policy.gas_headroom_bps, None, None, None, None, None, None, None, False, tuple((*blockers, "rpc_chain_mismatch")), ())
        return _result(envelope, policy, status="blocked", evidence=evidence, params=params)
    try:
        code = rpc.get_contract_code(envelope.to_address or "")
        if not isinstance(code, str) or not code.startswith("0x") or len(code) % 2 or any(char not in "0123456789abcdefABCDEF" for char in code[2:]):
            raise ValueError("target_contract_code_malformed")
    except Exception as exc:
        reason = "target_contract_code_malformed" if "malformed" in str(exc) else "target_contract_code_unavailable"
        evidence = DreamDexTransactionPreflightEvidence(chain, True, "malformed" if reason.endswith("malformed") else "unavailable", None, None, None, _empty_fee(reason), "not_attempted", "failed", False, (), (reason,))
        params = DreamDexResolvedTransactionParameters("unresolved", None, None, None, policy.gas_headroom_bps, None, None, None, None, None, None, None, False, tuple((*blockers, reason)), ())
        return _result(envelope, policy, status="failed", evidence=evidence, params=params)
    code_length = max(0, (len(code[2:]) // 2))
    if code.lower() in {"0x", "0x0"}:
        evidence = DreamDexTransactionPreflightEvidence(chain, True, "empty", None, None, None, _empty_fee("target_contract_code_missing"), "not_attempted", "source_observed", False, (), ("target_contract_code_missing",))
        params = DreamDexResolvedTransactionParameters("unresolved", None, None, None, policy.gas_headroom_bps, None, None, None, None, None, None, None, False, tuple((*blockers, "target_contract_code_missing")), ())
        return _result(envelope, policy, status="blocked", evidence=evidence, params=params)
    try:
        nonce = _uint(rpc.get_pending_nonce(envelope.from_address or ""), "pending_nonce", allow_none=False, maximum=(1 << 64) - 1)
    except Exception:
        evidence = DreamDexTransactionPreflightEvidence(chain, True, "present", None, None, None, _empty_fee("pending_nonce_unavailable"), "not_attempted", "failed", False, (), ("pending_nonce_unavailable",))
        params = DreamDexResolvedTransactionParameters("unresolved", None, None, None, policy.gas_headroom_bps, None, None, None, None, None, None, None, False, tuple((*blockers, "pending_nonce_unavailable", "pending_nonce_snapshot_not_reserved")), ())
        return _result(envelope, policy, status="failed", evidence=evidence, params=params)
    try:
        call = {"from": envelope.from_address or "", "to": envelope.to_address or "", "value": hex(envelope.value_wei or 0), "data": _calldata_hex(envelope.calldata)}
        gas_estimate = rpc.estimate_gas(call)
        gas_limit = calculate_gas_limit(gas_estimate, policy.gas_headroom_bps or 0, policy.maximum_gas_limit or 0)
    except ValueError as exc:
        reason = "gas_limit_policy_exceeded" if "policy_exceeded" in str(exc) else "gas_limit_policy_unresolved" if "headroom" in str(exc) or "maximum_gas" in str(exc) else "gas_estimate_reverted" if "revert" in str(exc) else "gas_estimate_unavailable"
        evidence = DreamDexTransactionPreflightEvidence(chain, True, "present", nonce, None, None, _empty_fee(reason), "reverted" if "revert" in str(exc) else "not_attempted", "failed", False, (), (reason,))
        params = DreamDexResolvedTransactionParameters("unresolved", nonce, None, None, policy.gas_headroom_bps, None, None, None, None, None, None, None, False, tuple((*blockers, reason, "pending_nonce_snapshot_not_reserved")), ())
        return _result(envelope, policy, status="blocked", evidence=evidence, params=params)
    except Exception as exc:
        # Preserve only the sanitized category; never expose raw revert data.
        reason = "gas_estimate_reverted" if "revert" in str(exc).lower() else "gas_estimate_unavailable"
        evidence = DreamDexTransactionPreflightEvidence(chain, True, "present", nonce, None, None, _empty_fee(reason), "reverted" if reason.endswith("reverted") else "not_attempted", "failed", False, (), (reason,))
        params = DreamDexResolvedTransactionParameters("unresolved", nonce, None, None, policy.gas_headroom_bps, None, None, None, None, None, None, None, False, tuple((*blockers, reason, "pending_nonce_snapshot_not_reserved")), ())
        return _result(envelope, policy, status="failed", evidence=evidence, params=params)
    try:
        block = rpc.get_latest_block_fee_evidence()
        if block.base_fee_per_gas_wei is not None:
            if policy.base_fee_multiplier_bps is None:
                raise ValueError("transaction_fee_limit_unresolved")
            try:
                priority = rpc.get_max_priority_fee_per_gas()
            except Exception:
                history = rpc.get_fee_history(1, "latest", (50,))
                rewards = history.get("reward") if isinstance(history, Mapping) else None
                raw_priority = rewards[0][0] if isinstance(rewards, list) and rewards and isinstance(rewards[0], list) and rewards[0] else None
                from bot.execution.dreamdex_readonly_rpc import parse_rpc_quantity
                priority = parse_rpc_quantity(raw_priority, field="priority_fee")
            if policy.maximum_priority_fee_per_gas_wei is None or priority > policy.maximum_priority_fee_per_gas_wei:
                raise ValueError("fee_priority_limit_unresolved")
            max_fee = calculate_eip1559_max_fee(block.base_fee_per_gas_wei, policy.base_fee_multiplier_bps or 0, priority)
            fee_evidence = DreamDexRpcFeeEvidence(block.latest_block_number, block.base_fee_per_gas_wei, None, priority, True, True, "source_confirmed", True)
            transaction_type, gas_price = "eip1559", None
            maximum_fee = calculate_total_fee(gas_limit, max_fee)
        else:
            if policy.legacy_gas_multiplier_bps is None:
                raise ValueError("transaction_fee_limit_unresolved")
            gas_price_rpc = rpc.get_gas_price()
            gas_price = calculate_legacy_gas_price(gas_price_rpc, policy.legacy_gas_multiplier_bps or 0)
            fee_evidence = DreamDexRpcFeeEvidence(block.latest_block_number, None, gas_price_rpc, None, False, False, "source_confirmed", True)
            transaction_type, max_fee = "legacy", None
            maximum_fee = calculate_total_fee(gas_limit, gas_price)
        if policy.maximum_total_fee_wei is None:
            raise ValueError("transaction_fee_limit_unresolved")
        if maximum_fee > policy.maximum_total_fee_wei:
            raise ValueError("transaction_fee_limit_exceeded")
    except ValueError as exc:
        reason = str(exc).split(":", 1)[0] or "fee_model_unresolved"
        evidence = DreamDexTransactionPreflightEvidence(chain, True, "present", nonce, gas_estimate, None, _empty_fee(reason), "not_attempted", "failed", False, (), (reason,))
        params = DreamDexResolvedTransactionParameters("unresolved", nonce, gas_estimate, gas_limit, policy.gas_headroom_bps, None, None, None, None, None, None, None, False, tuple((*blockers, reason, "pending_nonce_snapshot_not_reserved")), ())
        return _result(envelope, policy, status="blocked", evidence=evidence, params=params)
    except Exception:
        evidence = DreamDexTransactionPreflightEvidence(chain, True, "present", nonce, gas_estimate, None, _empty_fee("fee_evidence_unavailable"), "not_attempted", "failed", False, (), ("fee_evidence_unavailable",))
        params = DreamDexResolvedTransactionParameters("unresolved", nonce, gas_estimate, gas_limit, policy.gas_headroom_bps, None, None, None, None, None, None, None, False, tuple((*blockers, "fee_evidence_unavailable", "pending_nonce_snapshot_not_reserved")), ())
        return _result(envelope, policy, status="failed", evidence=evidence, params=params)
    try:
        native_balance = _uint(rpc.get_native_balance(envelope.from_address or ""), "native_balance", allow_none=False)
        required = calculate_required_native_balance(envelope.value_wei or 0, maximum_fee)
        sufficient = native_balance >= required
    except Exception:
        evidence = DreamDexTransactionPreflightEvidence(chain, True, "present", nonce, gas_estimate, None, fee_evidence, "not_attempted", "failed", False, (), ("native_fee_balance_unavailable",))
        params = DreamDexResolvedTransactionParameters(transaction_type, nonce, gas_estimate, gas_limit, policy.gas_headroom_bps, gas_price, max_fee, priority if transaction_type == "eip1559" else None, maximum_fee, None, None, None, False, tuple((*blockers, "native_fee_balance_unavailable", "pending_nonce_snapshot_not_reserved")), ())
        return _result(envelope, policy, status="failed", evidence=evidence, params=params)
    code_hash = sha256(bytes.fromhex(code[2:])).hexdigest()
    evidence = DreamDexTransactionPreflightEvidence(chain, True, "present", nonce, gas_estimate, native_balance, fee_evidence, "not_reverted", "source_confirmed", True, (), (), code_length, code_hash)
    parameter_values = {"transaction_type": transaction_type, "nonce": nonce, "gas_estimate": gas_estimate, "gas_limit": gas_limit, "gas_headroom_bps": policy.gas_headroom_bps, "gas_price_wei": gas_price, "max_fee_per_gas_wei": max_fee, "max_priority_fee_per_gas_wei": priority if transaction_type == "eip1559" else None, "maximum_possible_fee_wei": maximum_fee, "native_balance_sufficient": sufficient}
    param_fp = deterministic_fingerprint(parameter_values, domain="dreamdex/transaction-preflight-parameters", schema_version=PREFLIGHT_SCHEMA_VERSION)
    # A pending nonce is a point-in-time observation, never a reservation.
    # Keep the diagnostic blocker even when all other evidence resolves.
    param_blockers = [*blockers, "pending_nonce_snapshot_not_reserved"]
    if not sufficient:
        param_blockers.append("native_fee_balance_insufficient")
    if not policy.unresolved_reasons:
        pass
    params = DreamDexResolvedTransactionParameters(transaction_type, nonce, gas_estimate, gas_limit, policy.gas_headroom_bps, gas_price, max_fee, priority if transaction_type == "eip1559" else None, maximum_fee, required, sufficient, param_fp, False, tuple(dict.fromkeys(param_blockers)), ())
    finalized = None
    hard_blockers = [item for item in param_blockers if item != "pending_nonce_snapshot_not_reserved"]
    if not hard_blockers and sufficient:
        finalized = _build_finalized_envelope(envelope, nonce=nonce, gas_limit=gas_limit, transaction_type=transaction_type, gas_price=gas_price, max_fee=max_fee, priority=priority if transaction_type == "eip1559" else None)
    status = "completed" if finalized is not None and sufficient else "blocked"
    return _result(envelope, policy, status=status, evidence=evidence, params=params, blockers=param_blockers, finalized=finalized)


class DreamDexTransactionPreflight:
    """Side-effect-free coordinator around the pure preflight function."""

    def __init__(self, rpc: DreamDexReadOnlyRpc, policy: DreamDexTransactionPreflightPolicy) -> None:
        self.rpc = rpc
        self.policy = policy

    def run(self, envelope: DreamDexUnsignedTransactionEnvelope) -> DreamDexTransactionPreflightResult:
        return run_transaction_preflight(envelope, self.rpc, self.policy)


def build_transaction_preflight_preview(result: DreamDexTransactionPreflightResult | None = None) -> DreamDexTransactionPreflightPreview:
    if result is None:
        return DreamDexTransactionPreflightPreview("disabled", False, None, None, "unavailable", "unavailable", "unavailable", None, "unresolved", None, "unavailable", None, None, None, None, False, False, False, False, ("live_transaction_preflight_unavailable",))
    evidence, params = result.evidence, result.resolved_parameters
    return DreamDexTransactionPreflightPreview(result.preflight_status, result.preflight_status not in {"disabled", "unavailable"}, evidence.chain_id, evidence.chain_match, evidence.target_code_status, "present" if evidence.pending_nonce is not None else "unavailable", "present" if evidence.gas_estimate is not None else "unavailable", params.gas_limit, params.transaction_type, params.maximum_possible_fee_wei, "present" if evidence.native_balance_wei is not None else "unavailable", params.native_balance_sufficient, result.original_envelope_fingerprint, result.finalized_envelope_fingerprint, result.preflight_fingerprint, result.policy_compliant, result.ready_for_signing_policy_review, False, False, result.blockers)


def serialize_transaction_preflight_diagnostics(value: DreamDexTransactionPreflightResult | DreamDexTransactionPreflightPreview | Mapping[str, Any]) -> dict[str, Any]:
    if hasattr(value, "safe_dict"):
        return value.safe_dict()
    return ensure_no_raw_sensitive_fields(dict(value))


run_preflight = run_transaction_preflight
preflight_transaction = run_transaction_preflight
build_preflight_preview = build_transaction_preflight_preview

__all__ = [name for name in globals() if name.startswith("DreamDex") or name.startswith("calculate_") or name.startswith("build_") or name.startswith("serialize_") or name.startswith("unavailable_") or name in {"run_preflight", "run_transaction_preflight", "preflight_transaction", "POOL_ADDRESS", "CHAIN_ID", "PREFLIGHT_SCHEMA_VERSION"}]
