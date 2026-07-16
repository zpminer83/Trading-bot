"""Offline direct-owner execution audit and calldata previews.

This module is deliberately encoding-only.  It never imports a wallet client,
signer, RPC transport, or order lifecycle implementation and has no methods
that can submit, cancel, approve, or broadcast a transaction.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from enum import Enum
from hashlib import sha256
import json
import os
from pathlib import Path
import re
from typing import Any, Mapping, Sequence

from eth_abi import encode as abi_encode
from eth_utils import keccak


VENDOR_ROOT = Path(__file__).resolve().parents[2] / "vendor" / "dreamdex-bot-kit"
DIRECT_EXECUTION_MODES = frozenset({"direct_owner", "operator", "unavailable", "conflicting"})
DIRECT_TRANSPORTS = frozenset({"onchain_transaction", "authenticated_rest", "unauthenticated_rest", "eth_call", "local_only", "unsupported", "conflicting"})
DIRECT_ORDER_TYPES = {"limit": 0, "gtc": 0, "fok": 1, "ioc": 2, "post_only": 3}
DIRECT_TIME_IN_FORCE = {"gtc": 0, "fok": 1, "ioc": 2}
ZERO_ADDRESS = "0x" + "0" * 40
PLACE_SIGNATURE = "placeOrder(bool,uint64,uint256,uint256,uint64,uint8,uint8,address,uint96)"
CANCEL_SIGNATURE = "cancelOrder(uint128)"
REDUCE_SIGNATURE = "reduceOrder(uint128,uint256)"
PLACE_SELECTOR = "0x" + keccak(text=PLACE_SIGNATURE)[:4].hex()
CANCEL_SELECTOR = "0x" + keccak(text=CANCEL_SIGNATURE)[:4].hex()
REDUCE_SELECTOR = "0x" + keccak(text=REDUCE_SIGNATURE)[:4].hex()
ORDER_PLACED_TOPIC = "0xd90f62f61ee2f606b132cfdfd883ddd079228b6fd6bffd9d7cf848daf824639d"
ORDER_CANCELLED_TOPIC = "0x06ff08ed6b6987bb7df963009d8b54dc03988f4e465c009924929bb010fe03e7"
_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")

# Public, declaration-only input.  This is intentionally not a key variable
# and is never used to construct an account or a transport.
DIRECT_SIGNER_ADDRESS_ENV = "DREAMDEX_READ_ONLY_DIRECT_SIGNER_ADDRESS"
DIRECT_SIGNER_STATUSES = frozenset({"unconfigured", "user_declared", "source_compatible", "source_conflicting", "invalid"})
MATCH_STATUSES = frozenset({"confirmed", "mismatch", "unresolved"})
SIGNER_CAPABILITIES = ("direct_place_transaction", "direct_cancel_transaction", "direct_reduce_transaction")


def _safe_optional_address(value: Any, label: str) -> str | None:
    if value in (None, ""):
        return None
    return _address(value, label)


def _address(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _ADDRESS_RE.fullmatch(value):
        raise ValueError(f"{label}: configuration_invalid")
    return value.lower()


def _mask(value: str | None) -> str:
    if not value:
        return "<unresolved>"
    return value[:4] + "..." + value[-4:]


def _utc_path(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


class DirectExecutionMode(str, Enum):
    direct_owner = "direct_owner"
    operator = "operator"
    unavailable = "unavailable"
    conflicting = "conflicting"


ExecutionMode = DirectExecutionMode
DirectOwnerExecutionMode = DirectExecutionMode


@dataclass(frozen=True, repr=False)
class DreamDexDirectOwnerExecutionIdentity:
    contest_login_address: str | None = None
    configured_owner_address: str | None = None
    platform_trading_address: str | None = None
    transaction_signer_role: str = "unresolved"
    transaction_sender_address: str | None = None
    contract_order_owner_subject: str = "unresolved"
    vault_owner_subject: str = "unresolved"
    smart_wallet_role: str = "unresolved"
    authenticated_api_subject: str | None = None
    mapping_status: str = "unresolved"
    authoritative: bool = False
    evidence_sources: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()
    unresolved_reasons: tuple[str, ...] = (
        "direct_owner_execution_mapping_unresolved",
        "transaction_signer_unavailable",
    )

    def __post_init__(self) -> None:
        for name in ("contest_login_address", "configured_owner_address", "platform_trading_address", "transaction_sender_address", "authenticated_api_subject"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _address(value, name))
        if self.mapping_status not in {"unresolved", "observed_non_authoritative", "confirmed", "conflicting"}:
            raise ValueError("invalid mapping_status")
        if self.authoritative:
            raise ValueError("direct-owner identity cannot be authoritative without transaction evidence")

    def safe_dict(self) -> dict[str, Any]:
        return {
            "contest_login_address": _mask(self.contest_login_address),
            "configured_owner_address": _mask(self.configured_owner_address),
            "platform_trading_address": _mask(self.platform_trading_address),
            "transaction_signer_role": self.transaction_signer_role,
            "transaction_sender_address": _mask(self.transaction_sender_address),
            "contract_order_owner_subject": self.contract_order_owner_subject,
            "vault_owner_subject": self.vault_owner_subject,
            "smart_wallet_role": self.smart_wallet_role,
            "authenticated_api_subject": _mask(self.authenticated_api_subject),
            "mapping_status": self.mapping_status,
            "authoritative": False,
            "evidence_sources": self.evidence_sources,
            "conflicts": self.conflicts,
            "unresolved_reasons": self.unresolved_reasons,
        }

    def __repr__(self) -> str:
        return f"DreamDexDirectOwnerExecutionIdentity(owner={_mask(self.configured_owner_address)!r}, signer_role={self.transaction_signer_role!r}, mapping_status={self.mapping_status!r}, authoritative=False)"


@dataclass(frozen=True, repr=False)
class DirectAccountConstructionTrace:
    """Read-only source trace for the vendor's account/client construction."""

    account_constructor_status: str
    account_constructor_type: str
    wallet_client_binding_status: str
    execution_context_binding_status: str
    transaction_from_semantics: str
    source_trace_status: str
    source_files: tuple[str, ...] = ()
    source_fingerprints: tuple[tuple[str, str], ...] = ()
    source_roles: tuple[tuple[str, str], ...] = ()
    trace_steps: tuple[str, ...] = ()
    smart_wallet_used_in_signing_path: bool | None = None
    python_parity_status: str = "unavailable"

    def safe_dict(self) -> dict[str, Any]:
        return {
            "account_constructor_status": self.account_constructor_status,
            "account_constructor_type": self.account_constructor_type,
            "wallet_client_binding_status": self.wallet_client_binding_status,
            "execution_context_binding_status": self.execution_context_binding_status,
            "transaction_from_semantics": self.transaction_from_semantics,
            "source_trace_status": self.source_trace_status,
            "source_files": self.source_files,
            "source_fingerprints": self.source_fingerprints,
            "source_roles": self.source_roles,
            "trace_steps": self.trace_steps,
            "smart_wallet_used_in_signing_path": self.smart_wallet_used_in_signing_path,
            "python_parity_status": self.python_parity_status,
        }


@dataclass(frozen=True, repr=False)
class DirectSignerCandidateEvidence:
    candidate: str
    address: str | None
    evidence: tuple[str, ...] = ()
    compatible_with_context_account: str = "unresolved"
    used_as_transaction_sender: str = "unresolved"
    used_as_vault_subject: str = "unresolved"
    used_as_rest_subject: str = "unresolved"
    used_as_authenticated_subject: str = "unresolved"
    authoritative: bool = False
    unresolved_reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.address is not None:
            object.__setattr__(self, "address", _address(self.address, "candidate_address"))
        if self.authoritative:
            raise ValueError("candidate evidence cannot be authoritative from declarations alone")

    def safe_dict(self) -> dict[str, Any]:
        return {
            "candidate": self.candidate,
            "address": _mask(self.address),
            "evidence": self.evidence,
            "compatible_with_context_account": self.compatible_with_context_account,
            "used_as_transaction_sender": self.used_as_transaction_sender,
            "used_as_vault_subject": self.used_as_vault_subject,
            "used_as_rest_subject": self.used_as_rest_subject,
            "used_as_authenticated_subject": self.used_as_authenticated_subject,
            "authoritative": False,
            "unresolved_reasons": self.unresolved_reasons,
        }

    def __repr__(self) -> str:
        return f"DirectSignerCandidateEvidence(candidate={self.candidate!r}, address={_mask(self.address)!r}, authoritative=False)"


@dataclass(frozen=True, repr=False)
class DreamDexDirectSignerBindingEvidence:
    execution_mode: str = "direct_owner"
    account_constructor_status: str = "unavailable"
    account_constructor_type: str = "unavailable"
    wallet_client_binding_status: str = "unavailable"
    execution_context_binding_status: str = "unavailable"
    transaction_from_semantics: str = "unavailable"
    contest_owner_candidate: str | None = None
    platform_trading_wallet_candidate: str | None = None
    configured_owner_match_status: str = "unresolved"
    configured_trading_match_status: str = "unresolved"
    smart_wallet_used_in_signing_path: bool | None = None
    signer_address_source: str = "unavailable"
    signer_role: str = "unresolved"
    source_trace_status: str = "unavailable"
    python_parity_status: str = "unavailable"
    authoritative: bool = False
    evidence_sources: tuple[str, ...] = ()
    source_trace: tuple[dict[str, str], ...] = ()
    conflicts: tuple[str, ...] = ()
    unresolved_reasons: tuple[str, ...] = ()
    direct_signer_configured: str = "unconfigured"
    source_compatibility_status: str = "unresolved"
    direct_signer_address: str | None = None
    key_availability: str = "unavailable"
    candidate_matrix: tuple[DirectSignerCandidateEvidence, ...] = ()
    required_capabilities: tuple[str, ...] = SIGNER_CAPABILITIES

    def __post_init__(self) -> None:
        for name in ("contest_owner_candidate", "platform_trading_wallet_candidate", "direct_signer_address"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _address(value, name))
        if self.direct_signer_configured not in DIRECT_SIGNER_STATUSES:
            raise ValueError("invalid direct_signer_configured status")
        if self.source_compatibility_status not in {"unresolved", "source_compatible", "source_conflicting"}:
            raise ValueError("invalid source_compatibility_status")
        if self.configured_owner_match_status not in MATCH_STATUSES or self.configured_trading_match_status not in MATCH_STATUSES:
            raise ValueError("invalid signer match status")
        if self.authoritative:
            raise ValueError("direct signer binding is never authoritative without runtime signer evidence")

    def safe_dict(self) -> dict[str, Any]:
        return {
            "execution_mode": self.execution_mode,
            "account_constructor_status": self.account_constructor_status,
            "account_constructor_type": self.account_constructor_type,
            "wallet_client_binding_status": self.wallet_client_binding_status,
            "execution_context_binding_status": self.execution_context_binding_status,
            "transaction_from_semantics": self.transaction_from_semantics,
            "contest_owner_candidate": _mask(self.contest_owner_candidate),
            "platform_trading_wallet_candidate": _mask(self.platform_trading_wallet_candidate),
            "configured_owner_match_status": self.configured_owner_match_status,
            "configured_trading_match_status": self.configured_trading_match_status,
            "smart_wallet_used_in_signing_path": self.smart_wallet_used_in_signing_path,
            "signer_address_source": self.signer_address_source,
            "signer_role": self.signer_role,
            "source_trace_status": self.source_trace_status,
            "python_parity_status": self.python_parity_status,
            "authoritative": False,
            "evidence_sources": self.evidence_sources,
            "source_trace": self.source_trace,
            "conflicts": self.conflicts,
            "unresolved_reasons": self.unresolved_reasons,
            "direct_signer_configured": self.direct_signer_configured,
            "source_compatibility_status": self.source_compatibility_status,
            "direct_signer_address": _mask(self.direct_signer_address) if self.direct_signer_address else "<missing>",
            "key_availability": self.key_availability,
            "candidate_matrix": tuple(item.safe_dict() for item in self.candidate_matrix),
            "required_capabilities": self.required_capabilities,
        }

    def __repr__(self) -> str:
        return f"DreamDexDirectSignerBindingEvidence(configured={self.direct_signer_configured!r}, address={_mask(self.direct_signer_address)!r}, authoritative=False)"


@dataclass(frozen=True, repr=False)
class DreamDexDirectTransactionSignerRequirements:
    required_chain_id: int | None = 5031
    required_address: str | None = None
    capabilities: tuple[str, ...] = SIGNER_CAPABILITIES
    transaction_types: tuple[str, ...] = ("placeOrder", "cancelOrder", "reduceOrder")
    native_value_support: str = "conditional_getAutoPullRequirement"
    contract_call_support: str = "required"
    receipt_access_required: bool = True
    nonce_management_required: bool = True
    gas_estimation_required: bool = True
    production_status: str = "unavailable"
    unresolved_reasons: tuple[str, ...] = ("direct_signer_key_unavailable", "direct_transaction_transport_unimplemented")

    def __post_init__(self) -> None:
        if self.required_address is not None:
            object.__setattr__(self, "required_address", _address(self.required_address, "required_address"))

    def safe_dict(self) -> dict[str, Any]:
        return {
            "required_chain_id": self.required_chain_id,
            "required_address": _mask(self.required_address),
            "capabilities": self.capabilities,
            "transaction_types": self.transaction_types,
            "native_value_support": self.native_value_support,
            "contract_call_support": self.contract_call_support,
            "receipt_access_required": self.receipt_access_required,
            "nonce_management_required": self.nonce_management_required,
            "gas_estimation_required": self.gas_estimation_required,
            "production_status": self.production_status,
            "unresolved_reasons": self.unresolved_reasons,
        }

    def __repr__(self) -> str:
        return f"DreamDexDirectTransactionSignerRequirements(chain_id={self.required_chain_id!r}, address={_mask(self.required_address)!r}, production_status={self.production_status!r})"


@dataclass(frozen=True)
class DirectExecutionOperation:
    operation: str
    transport: str
    target: str
    method: str | None
    canonical_signature: str | None
    selector: str | None
    authentication_requirement: str
    signer_requirement: str
    value_requirement: str
    receipt_requirement: str
    status: str
    source_evidence: tuple[str, ...] = ()
    unresolved_reasons: tuple[str, ...] = ()
    from_semantics: str = "unavailable"
    chain_id_semantics: str = "unavailable"
    nonce_semantics: str = "unavailable"
    gas_policy: str = "unavailable"
    fee_fields: str = "unavailable"
    revert_handling: str = "unavailable"
    replacement_behavior: str = "unavailable"
    timeout_behavior: str = "unavailable"

    def __post_init__(self) -> None:
        if self.transport not in DIRECT_TRANSPORTS:
            raise ValueError("unsupported direct transport")

    def safe_dict(self) -> dict[str, Any]:
        return {
            "operation": self.operation,
            "transport": self.transport,
            "target": self.target,
            "method": self.method,
            "canonical_signature": self.canonical_signature,
            "selector": self.selector,
            "authentication_requirement": self.authentication_requirement,
            "signer_requirement": self.signer_requirement,
            "value_requirement": self.value_requirement,
            "receipt_requirement": self.receipt_requirement,
            "status": self.status,
            "source_evidence": self.source_evidence,
            "unresolved_reasons": self.unresolved_reasons,
            "from_semantics": self.from_semantics,
            "chain_id_semantics": self.chain_id_semantics,
            "nonce_semantics": self.nonce_semantics,
            "gas_policy": self.gas_policy,
            "fee_fields": self.fee_fields,
            "revert_handling": self.revert_handling,
            "replacement_behavior": self.replacement_behavior,
            "timeout_behavior": self.timeout_behavior,
        }


@dataclass(frozen=True)
class DirectOwnerExecutionAudit:
    selected_mode: str
    operator_mode_active: bool
    execution_authority: str
    authoritative: bool
    identity: DreamDexDirectOwnerExecutionIdentity
    operations: tuple[DirectExecutionOperation, ...]
    vendor_files: tuple[str, ...]
    vendor_file_fingerprints: tuple[tuple[str, str], ...]
    function_evidence: tuple[dict[str, Any], ...]
    event_evidence: tuple[dict[str, Any], ...]
    smart_wallet_semantics: str
    order_id_source: str
    unresolved_reasons: tuple[str, ...]
    selector_consistency: str = "confirmed"
    native_value_semantics: str = "unavailable"

    @property
    def direct_owner_selected(self) -> bool:
        return self.selected_mode == DirectExecutionMode.direct_owner.value

    @property
    def vendor_fingerprint(self) -> str:
        body = json.dumps(self.vendor_file_fingerprints, separators=(",", ":"), sort_keys=True)
        return sha256(body.encode("utf-8")).hexdigest()

    def safe_dict(self) -> dict[str, Any]:
        return {
            "selected_mode": self.selected_mode,
            "operator_mode_active": False,
            "execution_authority": self.execution_authority,
            "authoritative": False,
            "identity": self.identity.safe_dict(),
            "operations": tuple(item.safe_dict() for item in self.operations),
            "vendor_files": self.vendor_files,
            "vendor_file_fingerprints": self.vendor_file_fingerprints,
            "vendor_fingerprint": self.vendor_fingerprint,
            "function_evidence": self.function_evidence,
            "event_evidence": self.event_evidence,
            "smart_wallet_semantics": self.smart_wallet_semantics,
            "order_id_source": self.order_id_source,
            "selector_consistency": self.selector_consistency,
            "native_value_semantics": self.native_value_semantics,
            "unresolved_reasons": self.unresolved_reasons,
        }


def _find_vendor_files(root: Path) -> tuple[str, ...]:
    terms = ("placeOrder", "cancelOrder", "reduceOrder", "OrderPlaced", "OrderCancelled", "getOwnOpenOrders", "clientOrderId", "vault", "msg.sender", "beneficiary")
    found: list[str] = []
    if not root.is_dir():
        return ()
    for path in root.rglob("*"):
        if not path.is_file() or "node_modules" in path.parts or path.suffix.lower() not in {".ts", ".py", ".md", ".json"}:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if any(term in text for term in terms):
            found.append(_utc_path(root, path))
    return tuple(sorted(found))


def audit_direct_selectors(*, declared_selectors: Mapping[str, str] | None = None, vendor_root: str | Path | None = None) -> tuple[dict[str, Any], ...]:
    declared_selectors = declared_selectors or {}
    contract_text = ""
    if vendor_root is not None:
        try:
            contract_text = (Path(vendor_root) / "packages/core/src/contract.ts").read_text(encoding="utf-8", errors="ignore")
        except OSError:
            contract_text = ""
    definitions = (("placeOrder", PLACE_SIGNATURE, PLACE_SELECTOR), ("cancelOrder", CANCEL_SIGNATURE, CANCEL_SELECTOR), ("reduceOrder", REDUCE_SIGNATURE, REDUCE_SELECTOR))
    result = []
    for name, signature, computed in definitions:
        declared = declared_selectors.get(name)
        status = "conflicting" if declared is not None and declared.lower() != computed.lower() else ("unavailable" if contract_text and f'name: "{name}"' not in contract_text else "confirmed")
        result.append({"name": name, "signature": signature, "selector": declared or computed, "computed_selector": computed, "status": status})
    return tuple(result)


def audit_direct_owner_vendor(vendor_root: str | Path | None = None, *, declared_selectors: Mapping[str, str] | None = None) -> DirectOwnerExecutionAudit:
    root = Path(vendor_root) if vendor_root is not None else VENDOR_ROOT
    files = _find_vendor_files(root)
    fingerprints = tuple((relative, sha256((root / relative).read_bytes()).hexdigest()) for relative in files if (root / relative).is_file())
    source_contract = "packages/core/src/contract.ts"
    source_execute = "packages/core/src/execute.ts"
    source_pool = "packages/core/src/pool.ts"
    source_gotchas = "packages/core/src/gotchas.ts"
    source_tokens = "packages/core/src/config/tokens.ts"
    selector_audit = audit_direct_selectors(declared_selectors=declared_selectors, vendor_root=root)
    selector_consistency = "conflicting" if any(item["status"] == "conflicting" for item in selector_audit) else "confirmed"
    selector_by_name = {item["name"]: item for item in selector_audit}
    evidence = (
        {"name": "placeOrder", "signature": PLACE_SIGNATURE, "selector": selector_by_name["placeOrder"]["selector"], "target": "pool", "mutability": "payable", "source_file": source_contract, "status": "source_confirmed" if selector_by_name["placeOrder"]["status"] == "confirmed" else selector_by_name["placeOrder"]["status"]},
        {"name": "cancelOrder", "signature": CANCEL_SIGNATURE, "selector": selector_by_name["cancelOrder"]["selector"], "target": "pool", "mutability": "nonpayable", "source_file": source_contract, "status": "source_confirmed" if selector_by_name["cancelOrder"]["status"] == "confirmed" else selector_by_name["cancelOrder"]["status"]},
        {"name": "reduceOrder", "signature": REDUCE_SIGNATURE, "selector": selector_by_name["reduceOrder"]["selector"], "target": "pool", "mutability": "nonpayable", "source_file": source_contract, "status": "source_confirmed" if selector_by_name["reduceOrder"]["status"] == "confirmed" else selector_by_name["reduceOrder"]["status"]},
    )
    event_evidence = (
        {"name": "OrderPlaced", "signature": "unavailable; vendor exposes topic only", "topic0": ORDER_PLACED_TOPIC, "indexed_fields": "order_id topic[1] as consumed by execute.ts", "order_id_source": "receipt indexed topic[1]", "status": "source_confirmed", "source_file": source_contract},
        {"name": "OrderCancelled", "signature": "unavailable; vendor exposes topic only", "topic0": ORDER_CANCELLED_TOPIC, "indexed_fields": "order_id event field; exact layout unavailable", "order_id_source": "receipt event", "status": "source_confirmed", "source_file": source_contract},
    )
    operation_sources = (source_contract, source_execute, source_pool, source_gotchas, source_tokens)
    operations = (
        DirectExecutionOperation("place_order", "onchain_transaction", "pool", "placeOrder", PLACE_SIGNATURE, PLACE_SELECTOR, "none", "required", "conditional_getAutoPullRequirement", "success_receipt_and_OrderPlaced", "source_confirmed", operation_sources, from_semantics="ctx.account.address", chain_id_semantics="ctx.walletClient.chain; network configuration", nonce_semantics="wallet client/provider managed; exact policy unavailable", gas_policy="estimateContractGas with 13/10 headroom; floors 5,000,000 native BUY, 2,000,000 native SELL, 700,000 ERC20", fee_fields="wallet client/provider managed", revert_handling="simulation false, reverted receipt, or missing OrderPlaced log blocks", replacement_behavior="unavailable", timeout_behavior="waitForTransactionReceipt delegated"),
        DirectExecutionOperation("cancel_order", "onchain_transaction", "pool", "cancelOrder", CANCEL_SIGNATURE, CANCEL_SELECTOR, "none", "required", "zero", "success_receipt_and_OrderCancelled", "source_confirmed", operation_sources, from_semantics="ctx.account.address", chain_id_semantics="ctx.walletClient.chain; network configuration", nonce_semantics="wallet client/provider managed; exact policy unavailable", gas_policy="vendor leaves gas policy to simulation/provider", fee_fields="wallet client/provider managed", revert_handling="receipt failure blocks; event confirmation required by audit", replacement_behavior="unavailable", timeout_behavior="waitForTransactionReceipt delegated"),
        DirectExecutionOperation("reduce_order", "onchain_transaction", "pool", "reduceOrder", REDUCE_SIGNATURE, REDUCE_SELECTOR, "none", "required", "zero", "success_receipt", "source_confirmed", operation_sources, from_semantics="ctx.account.address", chain_id_semantics="ctx.walletClient.chain; network configuration", nonce_semantics="wallet client/provider managed; exact policy unavailable", gas_policy="vendor leaves gas policy to simulation/provider", fee_fields="wallet client/provider managed", revert_handling="receipt failure blocks", replacement_behavior="unavailable", timeout_behavior="waitForTransactionReceipt delegated"),
        DirectExecutionOperation("query_order", "eth_call", "pool", "getOrder", "getOrder(uint128)", "0x" + keccak(text="getOrder(uint128)")[:4].hex(), "none", "not_required", "zero", "not_applicable", "source_confirmed", (source_contract, source_pool)),
        DirectExecutionOperation("query_open_orders", "eth_call", "pool", "getOwnOpenOrders", "getOwnOpenOrders()", "0x" + keccak(text="getOwnOpenOrders()")[:4].hex(), "none", "not_required", "zero", "not_applicable", "source_confirmed", (source_contract, source_pool), ("caller_scoped_subject_unconfirmed",)),
    )
    identity = DreamDexDirectOwnerExecutionIdentity(
        transaction_signer_role="direct owner account (source model; address unresolved)",
        contract_order_owner_subject="transaction sender / ctx.account.address (source-confirmed direct path)",
        vault_owner_subject="transaction sender / ctx.account.address (source-confirmed default path)",
        smart_wallet_role="unresolved; code type alone is not role evidence",
        mapping_status="observed_non_authoritative",
        authoritative=False,
        evidence_sources=(source_execute, source_pool, "docs/session-keys.md"),
        unresolved_reasons=("transaction_signer_unavailable", "smart_wallet_owner_mapping_unresolved"),
    )
    reasons = ["transaction_signer_unavailable", "order_id_lifecycle_unconfirmed", "direct_order_reconciliation_unavailable", "smart_wallet_owner_mapping_unresolved"]
    if selector_consistency == "conflicting":
        reasons.append("direct_order_selector_conflicting")
    return DirectOwnerExecutionAudit("direct_owner", False, "unavailable", False, identity, operations, files, fingerprints, evidence, event_evidence, "observed_non_authoritative", "receipt OrderPlaced topic[1]", tuple(reasons), selector_consistency, "NATIVE_SENTINEL -> getAutoPullRequirement; msg.value=requiredAmount only when input token is native")


build_direct_owner_execution_audit = audit_direct_owner_vendor


def audit_direct_account_construction(vendor_root: str | Path | None = None) -> DirectAccountConstructionTrace:
    """Trace source semantics without importing or invoking vendor clients."""
    root = Path(vendor_root) if vendor_root is not None else VENDOR_ROOT
    client = root / "packages/core/src/client.ts"
    execute = root / "packages/core/src/execute.ts"
    pool = root / "packages/core/src/pool.ts"
    py_client = root / "packages/core-py/dreamdex_core/client.py"
    py_execute = root / "packages/core-py/dreamdex_core/execute.py"
    files = [path for path in (client, execute, pool, py_client, py_execute) if path.is_file()]
    if not files:
        return DirectAccountConstructionTrace("unavailable", "unavailable", "unavailable", "unavailable", "unavailable", "unavailable")
    texts = {path: path.read_text(encoding="utf-8", errors="ignore") for path in files}
    c, e, p = texts.get(client, ""), texts.get(execute, ""), texts.get(pool, "")
    pyc, pye = texts.get(py_client, ""), texts.get(py_execute, "")
    confirmed = all(term in c for term in ("privateKeyToAccount", "createWalletClient", "createPublicClient", "return { net, account, publicClient, walletClient, owner }"))
    ctx_confirmed = all(term in e for term in ("export interface ExecCtx", "account: Account", "ctx.account.address", "writeContract"))
    source_files = tuple(_utc_path(root, path) for path in files)
    fingerprints = tuple((_utc_path(root, path), sha256(path.read_bytes()).hexdigest()) for path in files)
    roles = tuple((name, role) for name, role in (
        ("packages/core/src/client.ts", "account_constructor_and_client_binding"),
        ("packages/core/src/execute.ts", "execution_context_and_transaction_sender"),
        ("packages/core/src/pool.ts", "pool_execution_context_and_owner_subject"),
        ("packages/core-py/dreamdex_core/client.py", "python_account_constructor"),
        ("packages/core-py/dreamdex_core/execute.py", "python_place_cancel_execution"),
    ) if name in source_files)
    steps = (
        "createChainContext input privateKey -> privateKeyToAccount",
        "account -> createWalletClient({account, chain, transport})",
        "ChainContext -> ExecCtx {publicClient,walletClient,account}",
        "ExecCtx account -> ctx.account.address",
        "simulateContract/writeContract account -> transaction from supplied account address",
    )
    return DirectAccountConstructionTrace(
        "source_confirmed" if confirmed else "unavailable",
        "viem privateKeyToAccount -> Account",
        "source_confirmed" if confirmed else "unavailable",
        "source_confirmed" if ctx_confirmed else "unavailable",
        "supplied_account_address" if ctx_confirmed else "unavailable",
        "confirmed" if confirmed and ctx_confirmed else "unavailable",
        source_files,
        fingerprints,
        roles,
        steps,
        False,
        "partial" if pyc and pye else "unavailable",
    )


trace_direct_account_construction = audit_direct_account_construction


def _match_declared(address: str | None, candidate: str | None) -> str:
    if address is None or candidate is None:
        return "unresolved"
    return "confirmed" if address.lower() == candidate.lower() else "mismatch"


def build_direct_signer_candidate_matrix(
    *,
    contest_owner_address: str | None = None,
    platform_trading_address: str | None = None,
    direct_signer_address: str | None = None,
) -> tuple[DirectSignerCandidateEvidence, ...]:
    owner = _safe_optional_address(contest_owner_address, "contest_owner_address")
    trading = _safe_optional_address(platform_trading_address, "platform_trading_address")
    signer = _safe_optional_address(direct_signer_address, "direct_signer_address")
    common = ("public declaration only", "no runtime key or transaction evidence")
    result = [
        DirectSignerCandidateEvidence("contest_owner", owner, common + ("candidate from configured/login owner",), _match_declared(signer, owner), "source_semantics_only", "unresolved", "unresolved", "unresolved", False, ("key_control_unverified",)),
        DirectSignerCandidateEvidence("platform_trading_wallet", trading, common + ("candidate from configured trading address",), _match_declared(signer, trading), "not_observed_in_direct_ctx", "unresolved", "unresolved", "unresolved", False, ("smart_wallet_signing_role_unresolved",)),
        DirectSignerCandidateEvidence("external_declared", signer, common + ("DREAMDEX_READ_ONLY_DIRECT_SIGNER_ADDRESS",) if signer else common, "user_declared" if signer else "unresolved", "unresolved", "unresolved", "unresolved", "unresolved", False, ("key_control_unverified",)),
        DirectSignerCandidateEvidence("unavailable", None, ("no public signer declaration",), "unresolved", "unresolved", "unresolved", "unresolved", "unresolved", False, ("direct_signer_address_unconfigured",)),
    ]
    return tuple(result)


def _declared_direct_signer(environ: Mapping[str, str] | None = None) -> tuple[str, str | None]:
    values = environ if environ is not None else {}
    raw = values.get(DIRECT_SIGNER_ADDRESS_ENV) if environ is not None else os.environ.get(DIRECT_SIGNER_ADDRESS_ENV)
    if raw in (None, ""):
        return "unconfigured", None
    try:
        return "user_declared", _address(raw, DIRECT_SIGNER_ADDRESS_ENV)
    except ValueError:
        return "invalid", None


def build_direct_signer_binding_evidence(
    *,
    contest_owner_address: str | None = None,
    platform_trading_address: str | None = None,
    environ: Mapping[str, str] | None = None,
    vendor_root: str | Path | None = None,
) -> DreamDexDirectSignerBindingEvidence:
    trace = audit_direct_account_construction(vendor_root)
    status, signer = _declared_direct_signer(environ)
    owner = _safe_optional_address(contest_owner_address, "contest_owner_address")
    trading = _safe_optional_address(platform_trading_address, "platform_trading_address")
    reasons: list[str] = []
    if status in {"unconfigured", "invalid"}:
        reasons.append("direct_signer_address_unconfigured" if status == "unconfigured" else "direct_signer_address_invalid")
    reasons.extend(("direct_signer_key_unavailable", "direct_signer_binding_non_authoritative", "direct_transaction_transport_unimplemented"))
    if trace.python_parity_status != "confirmed":
        reasons.append("python_direct_execution_unsupported")
    conflicts: list[str] = []
    compatibility = "unresolved"
    if status == "user_declared":
        compatibility = "source_compatible" if signer in {owner, trading} and signer is not None else "source_conflicting"
    if status == "user_declared" and compatibility == "source_conflicting":
        conflicts.append("declared_signer_matches_neither_candidate")
    role = "unresolved"
    if signer is not None:
        role = "contest_owner" if signer == owner else ("platform_trading_wallet" if signer == trading else "external_declared")
    return DreamDexDirectSignerBindingEvidence(
        account_constructor_status=trace.account_constructor_status,
        account_constructor_type=trace.account_constructor_type,
        wallet_client_binding_status=trace.wallet_client_binding_status,
        execution_context_binding_status=trace.execution_context_binding_status,
        transaction_from_semantics=trace.transaction_from_semantics,
        contest_owner_candidate=owner,
        platform_trading_wallet_candidate=trading,
        configured_owner_match_status=_match_declared(signer, owner),
        configured_trading_match_status=_match_declared(signer, trading),
        smart_wallet_used_in_signing_path=trace.smart_wallet_used_in_signing_path,
        signer_address_source=(DIRECT_SIGNER_ADDRESS_ENV if signer is not None else "unavailable"),
        signer_role=role,
        source_trace_status=trace.source_trace_status,
        python_parity_status=trace.python_parity_status,
        authoritative=False,
        evidence_sources=trace.source_files,
        source_trace=tuple({"source": source, "role": role} for source, role in trace.source_roles),
        conflicts=tuple(conflicts),
        unresolved_reasons=tuple(dict.fromkeys(reasons)),
        direct_signer_configured=status,
        source_compatibility_status=compatibility,
        direct_signer_address=signer,
        key_availability="unavailable",
        candidate_matrix=build_direct_signer_candidate_matrix(contest_owner_address=owner, platform_trading_address=trading, direct_signer_address=signer),
    )


def build_direct_transaction_signer_requirements(address: str | None = None) -> DreamDexDirectTransactionSignerRequirements:
    return DreamDexDirectTransactionSignerRequirements(required_address=_safe_optional_address(address, "direct_signer_address"))


build_direct_signer_binding = build_direct_signer_binding_evidence


def direct_owner_blocking_reasons(audit: DirectOwnerExecutionAudit | None = None, *, binding: DreamDexDirectSignerBindingEvidence | None = None) -> tuple[str, ...]:
    audit = audit or audit_direct_owner_vendor()
    binding = binding or build_direct_signer_binding_evidence()
    return tuple(dict.fromkeys((*audit.unresolved_reasons, *binding.unresolved_reasons, "direct_owner_execution_mapping_unresolved", "direct_order_transport_unconfirmed", "transaction_signer_unavailable", "order_id_lifecycle_unconfirmed", "direct_order_reconciliation_unavailable")))


def build_direct_owner_identity(*, contest_login_address: str | None = None, configured_owner_address: str | None = None, platform_trading_address: str | None = None, authenticated_api_subject: str | None = None) -> DreamDexDirectOwnerExecutionIdentity:
    return DreamDexDirectOwnerExecutionIdentity(
        contest_login_address=contest_login_address,
        configured_owner_address=configured_owner_address,
        platform_trading_address=platform_trading_address,
        authenticated_api_subject=authenticated_api_subject,
        transaction_signer_role="unresolved",
        transaction_sender_address=None,
        contract_order_owner_subject="unresolved",
        vault_owner_subject="unresolved",
        smart_wallet_role="unresolved",
        mapping_status="unresolved",
        authoritative=False,
    )


@dataclass(frozen=True, repr=False)
class DreamDexDirectOrderSpecification:
    symbol: str
    side: str
    order_type: str
    price: Decimal
    quantity: Decimal
    time_in_force: str
    post_only: bool
    reduce_only: bool
    client_order_id: str | None = None
    deadline: int | None = None
    owner_subject: str | None = None
    signer_subject: str | None = None
    target_contract: str | None = None
    native_value: Decimal | None = None
    tick_size: Decimal | None = None
    quantity_step: Decimal | None = None
    minimum_quantity: Decimal | None = None
    minimum_notional: Decimal | None = None
    validation_status: str = "unvalidated"
    authoritative: bool = False
    unresolved_reasons: tuple[str, ...] = ()
    price_scale: int = 1
    quantity_scale: int = 1
    user_data: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "side", str(self.side).lower())
        object.__setattr__(self, "order_type", str(self.order_type).lower())
        object.__setattr__(self, "time_in_force", str(self.time_in_force).lower())
        if self.side not in {"buy", "sell"}:
            raise ValueError("side must be buy or sell")
        if self.price <= 0 or self.quantity <= 0:
            raise ValueError("price and quantity must be positive")
        if self.price_scale <= 0 or self.quantity_scale <= 0:
            raise ValueError("scales must be positive")
        if self.target_contract is not None:
            object.__setattr__(self, "target_contract", _address(self.target_contract, "target_contract"))
        for name in ("owner_subject", "signer_subject"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _address(value, name))

    def __repr__(self) -> str:
        return f"DreamDexDirectOrderSpecification(symbol={self.symbol!r}, side={self.side!r}, order_type={self.order_type!r}, price=<redacted>, quantity=<redacted>, target={_mask(self.target_contract)!r}, authoritative=False)"

    def safe_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "side": self.side,
            "order_type": self.order_type,
            "time_in_force": self.time_in_force,
            "post_only": self.post_only,
            "reduce_only": self.reduce_only,
            "client_order_id_present": self.client_order_id is not None,
            "deadline_present": self.deadline is not None,
            "owner_subject": _mask(self.owner_subject),
            "signer_subject": _mask(self.signer_subject),
            "target_contract": _mask(self.target_contract),
            "native_value_category": "conditional_getAutoPullRequirement" if self.native_value is None else ("native_value_required" if self.native_value > 0 else "zero"),
            "tick_size": str(self.tick_size) if self.tick_size is not None else None,
            "quantity_step": str(self.quantity_step) if self.quantity_step is not None else None,
            "minimum_quantity": str(self.minimum_quantity) if self.minimum_quantity is not None else None,
            "minimum_notional": str(self.minimum_notional) if self.minimum_notional is not None else None,
            "validation_status": self.validation_status,
            "authoritative": False,
            "unresolved_reasons": self.unresolved_reasons,
        }


@dataclass(frozen=True)
class DirectOrderValidationResult:
    valid: bool
    status: str
    reasons: tuple[str, ...]
    raw_price: int | None = None
    raw_quantity: int | None = None

    @property
    def approved(self) -> bool:
        return self.valid


def _raw_decimal(value: Decimal, scale: int, label: str) -> int:
    raw = value * Decimal(scale)
    if raw != raw.to_integral_value():
        raise ValueError(f"{label}: not representable in raw units")
    return int(raw)


def validate_direct_order_specification(spec: DreamDexDirectOrderSpecification, *, require_complete_account: bool = True, expected_target_contract: str | None = None) -> DirectOrderValidationResult:
    reasons: list[str] = []
    if spec.target_contract is None:
        reasons.append("target_contract_unavailable")
    elif expected_target_contract is not None and spec.target_contract != _address(expected_target_contract, "expected_target_contract"):
        reasons.append("target_contract_conflict")
    if spec.order_type not in DIRECT_ORDER_TYPES or spec.order_type == "market":
        reasons.append("unsupported_order_type")
    if spec.time_in_force not in DIRECT_TIME_IN_FORCE:
        reasons.append("unsupported_time_in_force")
    if spec.post_only and spec.order_type not in {"limit", "post_only"}:
        reasons.append("post_only_semantics_conflict")
    if spec.reduce_only and spec.side != "sell":
        reasons.append("reduce_only_side_unavailable")
    if spec.client_order_id is not None:
        reasons.append("client_order_id_unsupported")
    if spec.deadline is None or spec.deadline <= 0:
        reasons.append("deadline_unavailable")
    if spec.tick_size is None:
        reasons.append("tick_size_unavailable")
    elif spec.price % spec.tick_size != 0:
        reasons.append("invalid_price_tick")
    if spec.quantity_step is None:
        reasons.append("quantity_step_unavailable")
    elif spec.quantity % spec.quantity_step != 0:
        reasons.append("invalid_quantity_step")
    if spec.minimum_quantity is None:
        reasons.append("minimum_quantity_unavailable")
    elif spec.quantity < spec.minimum_quantity:
        reasons.append("quantity_below_minimum")
    if spec.minimum_notional is None:
        reasons.append("minimum_notional_unavailable")
    elif spec.price * spec.quantity < spec.minimum_notional:
        reasons.append("minimum_notional")
    if require_complete_account and (spec.owner_subject is None or spec.signer_subject is None):
        reasons.append("transaction_signer_unavailable")
    if spec.owner_subject is not None and spec.signer_subject is not None and spec.owner_subject != spec.signer_subject:
        reasons.append("owner_signer_mismatch")
    try:
        raw_price = _raw_decimal(spec.price, spec.price_scale, "price")
        raw_quantity = _raw_decimal(spec.quantity, spec.quantity_scale, "quantity")
    except ValueError as exc:
        reasons.append(str(exc))
        raw_price = raw_quantity = None
    if raw_price is not None and raw_price <= 0:
        reasons.append("price_raw_zero")
    status = "valid" if not reasons else "blocked"
    return DirectOrderValidationResult(not reasons, status, tuple(dict.fromkeys(reasons)), raw_price, raw_quantity)


@dataclass(frozen=True, repr=False)
class DirectOrderCallPreview:
    target_masked: str
    function_name: str
    selector: str
    calldata_length: int | None
    calldata_fingerprint: str | None
    native_value_category: str
    validation_status: str
    unresolved_reasons: tuple[str, ...]
    target_contract: str | None = None
    order_id: int | None = None

    def safe_dict(self) -> dict[str, Any]:
        return {
            "target_masked": self.target_masked,
            "function_name": self.function_name,
            "selector": self.selector,
            "calldata_length": self.calldata_length,
            "calldata_fingerprint": self.calldata_fingerprint,
            "native_value_category": self.native_value_category,
            "validation_status": self.validation_status,
            "unresolved_reasons": self.unresolved_reasons,
        }

    def __repr__(self) -> str:
        return f"DirectOrderCallPreview(function_name={self.function_name!r}, selector={self.selector!r}, calldata_length={self.calldata_length!r}, calldata_fingerprint={self.calldata_fingerprint!r}, target={self.target_masked!r})"


def _preview(function_name: str, selector: str, target: str, calldata: bytes | None, status: str, reasons: Sequence[str], value_category: str, order_id: int | None = None) -> DirectOrderCallPreview:
    return DirectOrderCallPreview(_mask(target), function_name, selector, (len(calldata) if calldata is not None else None), (sha256(calldata).hexdigest() if calldata is not None else None), value_category, status, tuple(dict.fromkeys(reasons)), target, order_id)


def compute_safe_calldata_fingerprint(value: bytes | bytearray | DirectOrderCallPreview) -> str | None:
    if isinstance(value, DirectOrderCallPreview):
        return value.calldata_fingerprint
    if isinstance(value, (bytes, bytearray)):
        return sha256(bytes(value)).hexdigest()
    raise TypeError("only encoded bytes or a preview are accepted")


def _place_calldata(spec: DreamDexDirectOrderSpecification, raw_price: int, raw_quantity: int) -> bytes:
    order_type = 3 if spec.post_only else DIRECT_ORDER_TYPES[spec.order_type]
    tif = DIRECT_TIME_IN_FORCE[spec.time_in_force]
    # Vendor uses selfMatchingOption=CancelTaker (0), builder=zero address,
    # and builderFeeBpsTimes1k=0 on the untagged direct path.
    return bytes.fromhex(PLACE_SELECTOR[2:]) + abi_encode(
        ["bool", "uint64", "uint256", "uint256", "uint64", "uint8", "uint8", "address", "uint96"],
        [spec.side == "buy", spec.user_data, raw_price, raw_quantity, spec.deadline, order_type, 0, ZERO_ADDRESS, 0],
    )


def build_place_order_call_preview(spec: DreamDexDirectOrderSpecification) -> DirectOrderCallPreview:
    validation = validate_direct_order_specification(spec, require_complete_account=False)
    if spec.target_contract is None or validation.raw_price is None or validation.raw_quantity is None or spec.deadline is None:
        return _preview("placeOrder", PLACE_SELECTOR, spec.target_contract or ZERO_ADDRESS, None, validation.status, validation.reasons, "unavailable")
    try:
        calldata = _place_calldata(spec, validation.raw_price, validation.raw_quantity)
    except (ValueError, TypeError, InvalidOperation) as exc:
        return _preview("placeOrder", PLACE_SELECTOR, spec.target_contract, None, "blocked", (*validation.reasons, "encoding_error"), "unavailable")
    value_category = "conditional_getAutoPullRequirement" if spec.native_value is None else ("native_value_required" if spec.native_value > 0 else "zero")
    return _preview("placeOrder", PLACE_SELECTOR, spec.target_contract, calldata, validation.status, validation.reasons, value_category)


def _resolve_cancel_inputs(spec_or_target: DreamDexDirectOrderSpecification | str | None, target_contract: str | None, order_id: int | None, owner_subject: str | None, signer_subject: str | None) -> tuple[str | None, int | None, list[str]]:
    reasons: list[str] = []
    if isinstance(spec_or_target, DreamDexDirectOrderSpecification):
        target_contract = target_contract or spec_or_target.target_contract
        owner_subject = owner_subject or spec_or_target.owner_subject
        signer_subject = signer_subject or spec_or_target.signer_subject
    elif isinstance(spec_or_target, str):
        target_contract = target_contract or spec_or_target
    if target_contract is None:
        reasons.append("target_contract_unavailable")
    else:
        target_contract = _address(target_contract, "target_contract")
    if order_id is None or order_id < 0:
        reasons.append("order_id_unavailable")
    if owner_subject is not None and signer_subject is not None and owner_subject.lower() != signer_subject.lower():
        reasons.append("cancel_owner_mismatch")
    if signer_subject is None:
        reasons.append("transaction_signer_unavailable")
    return target_contract, order_id, reasons


def build_cancel_order_call_preview(spec_or_target: DreamDexDirectOrderSpecification | str | None = None, *, target_contract: str | None = None, order_id: int | None = None, owner_subject: str | None = None, signer_subject: str | None = None) -> DirectOrderCallPreview:
    target, resolved_id, reasons = _resolve_cancel_inputs(spec_or_target, target_contract, order_id, owner_subject, signer_subject)
    calldata = None
    if target is not None and resolved_id is not None and resolved_id >= 0 and not reasons:
        calldata = bytes.fromhex(CANCEL_SELECTOR[2:]) + abi_encode(["uint128"], [resolved_id])
    return _preview("cancelOrder", CANCEL_SELECTOR, target or ZERO_ADDRESS, calldata, "valid" if calldata is not None else "blocked", reasons, "zero", resolved_id)


def build_reduce_order_call_preview(spec_or_target: DreamDexDirectOrderSpecification | str | None = None, *, target_contract: str | None = None, order_id: int | None = None, new_quantity_remaining: int | None = None, signer_subject: str | None = None) -> DirectOrderCallPreview:
    target, resolved_id, reasons = _resolve_cancel_inputs(spec_or_target, target_contract, order_id, None, signer_subject)
    if new_quantity_remaining is None or new_quantity_remaining < 0:
        reasons.append("new_quantity_remaining_unavailable")
    calldata = None
    if target is not None and resolved_id is not None and resolved_id >= 0 and new_quantity_remaining is not None and new_quantity_remaining >= 0 and not reasons:
        calldata = bytes.fromhex(REDUCE_SELECTOR[2:]) + abi_encode(["uint128", "uint256"], [resolved_id, new_quantity_remaining])
    return _preview("reduceOrder", REDUCE_SELECTOR, target or ZERO_ADDRESS, calldata, "valid" if calldata is not None else "blocked", reasons, "zero", resolved_id)


@dataclass(frozen=True)
class OrderIdLifecycleEvidence:
    source: str
    status: str
    order_id: int | None
    event_name: str | None
    topic0: str | None
    owner_match: str
    pool_match: str
    authoritative: bool = False
    unresolved_reasons: tuple[str, ...] = ()

    def safe_dict(self) -> dict[str, Any]:
        return {"source": self.source, "status": self.status, "order_id": self.order_id, "event_name": self.event_name, "topic0": self.topic0, "owner_match": self.owner_match, "pool_match": self.pool_match, "authoritative": False, "unresolved_reasons": self.unresolved_reasons}


def parse_order_placed_event(receipt: Mapping[str, Any] | None, *, expected_owner: str | None = None, expected_pool: str | None = None) -> OrderIdLifecycleEvidence:
    if not isinstance(receipt, Mapping):
        return OrderIdLifecycleEvidence("receipt_event", "absent", None, "OrderPlaced", ORDER_PLACED_TOPIC, "unresolved", "unresolved", False, ("order_id_event_absent",))
    if receipt.get("status") in {0, "0x0", "reverted", "failed"}:
        return OrderIdLifecycleEvidence("receipt_event", "blocked", None, "OrderPlaced", ORDER_PLACED_TOPIC, "unresolved", "unresolved", False, ("receipt_reverted",))
    logs = receipt.get("logs")
    for log in logs if isinstance(logs, Sequence) and not isinstance(logs, (str, bytes, bytearray)) else ():
        if not isinstance(log, Mapping):
            continue
        topics = log.get("topics")
        topic0 = topics[0].lower() if isinstance(topics, Sequence) and topics and isinstance(topics[0], str) else None
        if topic0 != ORDER_PLACED_TOPIC:
            continue
        order_id = None
        if len(topics) > 1 and isinstance(topics[1], str) and topics[1].startswith("0x"):
            try:
                order_id = int(topics[1], 16)
            except ValueError:
                order_id = None
        owner_match = "unresolved"
        if expected_owner is not None and isinstance(log.get("owner"), str):
            owner_match = "confirmed" if log["owner"].lower() == _address(expected_owner, "expected_owner") else "mismatch"
        pool_match = "unresolved"
        if expected_pool is not None and isinstance(log.get("address"), str):
            pool_match = "confirmed" if log["address"].lower() == _address(expected_pool, "expected_pool") else "mismatch"
        reasons = [] if order_id is not None and owner_match != "mismatch" and pool_match != "mismatch" else ["order_id_event_malformed_or_mismatch"]
        return OrderIdLifecycleEvidence("receipt_event", "confirmed" if not reasons else "blocked", order_id, "OrderPlaced", topic0, owner_match, pool_match, False, tuple(reasons))
    return OrderIdLifecycleEvidence("receipt_event", "absent", None, "OrderPlaced", ORDER_PLACED_TOPIC, "unresolved", "unresolved", False, ("order_id_event_absent",))


def parse_order_cancelled_event(receipt: Mapping[str, Any] | None) -> OrderIdLifecycleEvidence:
    if not isinstance(receipt, Mapping):
        return OrderIdLifecycleEvidence("receipt_event", "absent", None, "OrderCancelled", ORDER_CANCELLED_TOPIC, "unresolved", "unresolved", False, ("cancellation_event_absent",))
    if receipt.get("status") in {0, "0x0", "reverted", "failed"}:
        return OrderIdLifecycleEvidence("receipt_event", "blocked", None, "OrderCancelled", ORDER_CANCELLED_TOPIC, "unresolved", "unresolved", False, ("receipt_reverted",))
    logs = receipt.get("logs")
    for log in logs if isinstance(logs, Sequence) and not isinstance(logs, (str, bytes, bytearray)) else ():
        topics = log.get("topics") if isinstance(log, Mapping) else None
        topic0 = topics[0].lower() if isinstance(topics, Sequence) and topics and isinstance(topics[0], str) else None
        if topic0 == ORDER_CANCELLED_TOPIC:
            order_id = None
            if len(topics) > 1 and isinstance(topics[1], str) and topics[1].startswith("0x"):
                try:
                    order_id = int(topics[1], 16)
                except ValueError:
                    pass
            return OrderIdLifecycleEvidence("receipt_event", "confirmed" if order_id is not None else "blocked", order_id, "OrderCancelled", topic0, "unresolved", "unresolved", False, () if order_id is not None else ("cancellation_event_malformed",))
    return OrderIdLifecycleEvidence("receipt_event", "absent", None, "OrderCancelled", ORDER_CANCELLED_TOPIC, "unresolved", "unresolved", False, ("cancellation_event_absent",))


def build_order_specification(**kwargs: Any) -> DreamDexDirectOrderSpecification:
    return DreamDexDirectOrderSpecification(**kwargs)


build_place_order_preview = build_place_order_call_preview
build_cancel_order_preview = build_cancel_order_call_preview
build_reduce_order_preview = build_reduce_order_call_preview
parse_order_placed_receipt = parse_order_placed_event
parse_order_cancelled_receipt = parse_order_cancelled_event


__all__ = [
    "DIRECT_EXECUTION_MODES", "DIRECT_TRANSPORTS", "DIRECT_SIGNER_ADDRESS_ENV", "DIRECT_SIGNER_STATUSES", "MATCH_STATUSES", "SIGNER_CAPABILITIES", "PLACE_SIGNATURE", "CANCEL_SIGNATURE", "REDUCE_SIGNATURE", "PLACE_SELECTOR", "CANCEL_SELECTOR", "REDUCE_SELECTOR", "ORDER_PLACED_TOPIC", "ORDER_CANCELLED_TOPIC",
    "DirectExecutionMode", "ExecutionMode", "DirectOwnerExecutionMode", "DreamDexDirectOwnerExecutionIdentity", "DirectAccountConstructionTrace", "DirectSignerCandidateEvidence", "DreamDexDirectSignerBindingEvidence", "DreamDexDirectTransactionSignerRequirements", "DreamDexDirectOrderSpecification", "DirectExecutionOperation", "DirectOwnerExecutionAudit", "DirectOrderValidationResult", "DirectOrderCallPreview", "OrderIdLifecycleEvidence",
    "audit_direct_selectors", "audit_direct_owner_vendor", "build_direct_owner_execution_audit", "audit_direct_account_construction", "trace_direct_account_construction", "build_direct_signer_candidate_matrix", "build_direct_signer_binding_evidence", "build_direct_signer_binding", "build_direct_transaction_signer_requirements", "direct_owner_blocking_reasons", "build_direct_owner_identity", "validate_direct_order_specification", "build_place_order_call_preview", "build_cancel_order_call_preview", "build_reduce_order_call_preview", "build_place_order_preview", "build_cancel_order_preview", "build_reduce_order_preview", "compute_safe_calldata_fingerprint", "parse_order_placed_event", "parse_order_cancelled_event", "parse_order_placed_receipt", "parse_order_cancelled_receipt", "build_order_specification",
]
