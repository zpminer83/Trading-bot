"""Offline, fail-closed rehearsal of a single DreamDEX place-order candidate.

This module deliberately stops before production journal, signing, nonce lease,
prompt, keystore, and submission.  A caller may provide an explicit typed
read-only collector for evidence; no collector is invoked by default.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
import time
from types import MappingProxyType
from typing import Any, Callable, Mapping, Protocol

from .dreamdex_execution_primitives import mask_evm_address, mask_hex_hash, sha256_hex, validate_evm_address

READ_ONLY_REHEARSAL_RPC_ALLOWLIST = frozenset({
    "eth_chainId", "eth_getCode", "eth_getTransactionCount", "eth_estimateGas",
    "eth_gasPrice", "eth_maxPriorityFeePerGas", "eth_getBalance",
})
READ_ONLY_REHEARSAL_FORBIDDEN_PREFIXES = ("eth_send", "personal_", "wallet_", "debug_", "trace_")
READ_ONLY_REHEARSAL_FORBIDDEN_METHODS = frozenset({
    "eth_sendTransaction", "eth_sendRawTransaction", "eth_getTransactionReceipt",
    "eth_getTransactionByHash", "eth_getLogs", "eth_subscribe", "eth_newFilter",
    "personal_sign", "wallet_sendTransaction",
})


class ReadOnlyRehearsalReader(Protocol):
    """Typed callable boundary for one read-only evidence source."""

    def __call__(self) -> Any: ...


@dataclass(frozen=True, repr=False)
class DreamDexLiveReadOnlyRehearsalDependencies:
    """The deliberately narrow dependency bundle for explicit live rehearsal.

    It contains only readers and immutable safe configuration snapshots.  No
    signer, secret provider, journal repository, approval provider, submitter,
    or raw transaction transport can be attached to this type.
    """

    public_market_reader: ReadOnlyRehearsalReader
    authenticated_account_reader: ReadOnlyRehearsalReader
    typed_rpc_preflight_reader: ReadOnlyRehearsalReader
    monotonic_clock: Callable[[], float] = time.monotonic
    safe_config: Mapping[str, Any] = field(default_factory=dict)
    risk_snapshot: Mapping[str, Any] = field(default_factory=dict)
    fair_play_snapshot: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in (
            "public_market_reader",
            "authenticated_account_reader",
            "typed_rpc_preflight_reader",
            "monotonic_clock",
        ):
            if not callable(getattr(self, name)):
                raise TypeError(f"{name}_must_be_callable")
        try:
            safe_config = dict(self.safe_config)
            risk_snapshot = dict(self.risk_snapshot)
            fair_play_snapshot = dict(self.fair_play_snapshot)
        except (TypeError, ValueError):
            raise TypeError("read_only_snapshots_must_be_mappings") from None
        forbidden = ("token", "bearer", "private", "password", "secret", "key", "seed")
        for key in safe_config:
            if any(part in str(key).lower() for part in forbidden):
                raise ValueError("secret_configuration_not_allowed")
        object.__setattr__(self, "safe_config", MappingProxyType(safe_config))
        object.__setattr__(self, "risk_snapshot", MappingProxyType(risk_snapshot))
        object.__setattr__(self, "fair_play_snapshot", MappingProxyType(fair_play_snapshot))

    def __repr__(self) -> str:
        return "DreamDexLiveReadOnlyRehearsalDependencies(<read-only>)"


def _d(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _safe_fp(value: Any) -> str:
    return sha256_hex(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode())


@dataclass(frozen=True, repr=False)
class DreamDexZeroMutationRehearsalPolicy:
    schema_version: str = "dreamdex-zero-mutation-rehearsal-v1"
    required_chain_id: int = 5031
    required_market_symbol: str | None = None
    required_market_address: str | None = None
    expected_signer_address: str | None = None
    maximum_market_age_ms: int = 30_000
    maximum_account_age_ms: int = 30_000
    maximum_order_notional: Decimal = Decimal("100")
    maximum_position_notional: Decimal = Decimal("100")
    maximum_open_orders: int = 1
    maximum_fee_wei: int = 10**18
    require_authoritative_market_data: bool = True
    require_authoritative_account_data: bool = True
    require_market_rules: bool = True
    require_trading_enabled: bool = True
    require_contract_code: bool = True
    require_pending_nonce: bool = True
    require_fee_evidence: bool = True
    require_gas_estimate: bool = True
    require_balance_evidence: bool = True
    require_runtime_launch_gate: bool = True
    require_risk_approval: bool = True
    require_fair_play_approval: bool = True
    allow_temporary_rehearsal_journal: bool = False
    allow_approval_prompt: bool = False
    allow_keystore_access: bool = False
    allow_signing: bool = False
    allow_submission: bool = False
    authoritative: bool = False
    unresolved_reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.required_chain_id != 5031:
            raise ValueError("rehearsal_chain_must_be_5031")
        if self.maximum_market_age_ms < 0 or self.maximum_account_age_ms < 0:
            raise ValueError("rehearsal_age_limits_must_be_non_negative")
        if self.maximum_order_notional <= 0 or self.maximum_position_notional <= 0:
            raise ValueError("rehearsal_notional_limits_must_be_positive")
        if self.maximum_open_orders < 1 or self.maximum_fee_wei < 0:
            raise ValueError("rehearsal_numeric_limits_invalid")
        for name in ("required_market_address", "expected_signer_address"):
            value = getattr(self, name)
            if value is not None:
                validate_evm_address(value, field=name)
        for flag in ("allow_temporary_rehearsal_journal", "allow_approval_prompt", "allow_keystore_access", "allow_signing", "allow_submission", "authoritative"):
            if getattr(self, flag):
                raise ValueError(f"{flag}_must_remain_disabled")

    def safe_dict(self) -> dict[str, Any]:
        result = {"schema_version": self.schema_version, "required_chain_id": self.required_chain_id,
                  "required_market_symbol": self.required_market_symbol,
                  "required_market_address": mask_evm_address(self.required_market_address) if self.required_market_address else None,
                  "expected_signer_address": mask_evm_address(self.expected_signer_address) if self.expected_signer_address else None,
                  "maximum_market_age_ms": self.maximum_market_age_ms, "maximum_account_age_ms": self.maximum_account_age_ms,
                  "maximum_order_notional": str(self.maximum_order_notional), "maximum_position_notional": str(self.maximum_position_notional),
                  "maximum_open_orders": self.maximum_open_orders, "maximum_fee_wei": self.maximum_fee_wei,
                  "require_authoritative_market_data": self.require_authoritative_market_data,
                  "require_authoritative_account_data": self.require_authoritative_account_data,
                  "require_market_rules": self.require_market_rules,
                  "require_trading_enabled": self.require_trading_enabled,
                  "require_contract_code": self.require_contract_code,
                  "require_pending_nonce": self.require_pending_nonce,
                  "require_fee_evidence": self.require_fee_evidence,
                  "require_gas_estimate": self.require_gas_estimate,
                  "require_balance_evidence": self.require_balance_evidence,
                  "require_runtime_launch_gate": self.require_runtime_launch_gate,
                  "require_risk_approval": self.require_risk_approval,
                  "require_fair_play_approval": self.require_fair_play_approval,
                  "allow_temporary_rehearsal_journal": False, "allow_approval_prompt": False, "allow_keystore_access": False,
                  "allow_signing": False, "allow_submission": False, "authoritative": False,
                  "unresolved_reasons": list(self.unresolved_reasons)}
        return result

    def __repr__(self) -> str:
        return "DreamDexZeroMutationRehearsalPolicy(<safe>)"


@dataclass(frozen=True, repr=False)
class DreamDexZeroMutationRehearsalEvidence:
    market_status: str = "unavailable"
    account_status: str = "unavailable"
    rpc_status: str = "unavailable"
    chain_id: int | None = None
    target_code_status: str = "unavailable"
    pending_nonce_status: str = "unavailable"
    native_balance_status: str = "unavailable"
    gas_estimate_status: str = "unavailable"
    fee_status: str = "unavailable"
    market_rules_status: str = "unavailable"
    runtime_gate_status: str = "unavailable"
    risk_status: str = "unavailable"
    fair_play_status: str = "unavailable"
    market_age_ms: int | None = None
    account_age_ms: int | None = None
    source_fingerprint: str = ""
    market_fingerprint: str = ""
    account_fingerprint: str = ""
    risk_fingerprint: str = ""
    fair_play_fingerprint: str = ""
    observed_monotonic_ms: int | None = None
    network_read_call_count: int = 0
    source_authority: str = "non_authoritative"
    market_identity_status: str = "unavailable"
    account_identity_status: str = "unavailable"
    trading_enabled: bool | None = None
    contract_code_present: bool | None = None
    pending_nonce: int | None = None
    gas_estimate: int | None = None
    estimated_fee_wei: int | None = None
    native_balance_wei: int | None = None
    drawdown_fraction: Decimal | None = None
    preemptive_drawdown: Decimal | None = None
    hard_drawdown_limit: Decimal | None = None
    kill_switch_latched: bool = False
    emergency_exit_requested: bool = False
    emergency_exit_completed: bool = False
    gap_risk_status: str = "unavailable"
    gap_risk_budget_approved: bool | None = None
    gap_risk_blockers: tuple[str, ...] = ()
    account_authority_status: str = "unresolved"
    open_order_status: str = "unavailable"
    fills_status: str = "unavailable"
    public_market_call_count: int = 0
    authenticated_account_call_count: int = 0
    read_only_rpc_call_count: int = 0
    projected_shocked_drawdown: Decimal | None = None
    maximum_total_fee_wei: int | None = None
    transaction_type: str = "unresolved"
    orderbook_status: str = "unavailable"
    open_order_count: int | None = None

    def safe_dict(self) -> dict[str, Any]:
        return {"market_status": self.market_status, "account_status": self.account_status, "rpc_status": self.rpc_status,
                "chain_id": self.chain_id, "target_code_status": self.target_code_status,
                "orderbook_status": self.orderbook_status,
                "open_order_count": self.open_order_count,
                "pending_nonce_status": self.pending_nonce_status, "native_balance_status": self.native_balance_status,
                "gas_estimate_status": self.gas_estimate_status, "fee_status": self.fee_status,
                "market_rules_status": self.market_rules_status, "runtime_gate_status": self.runtime_gate_status,
                "risk_status": self.risk_status, "fair_play_status": self.fair_play_status,
                "market_age_ms": self.market_age_ms, "account_age_ms": self.account_age_ms,
                "source_fingerprint": mask_hex_hash(self.source_fingerprint) if self.source_fingerprint else "",
                "market_fingerprint": mask_hex_hash(self.market_fingerprint) if self.market_fingerprint else "",
                "account_fingerprint": mask_hex_hash(self.account_fingerprint) if self.account_fingerprint else "",
                "risk_fingerprint": mask_hex_hash(self.risk_fingerprint) if self.risk_fingerprint else "",
                "fair_play_fingerprint": mask_hex_hash(self.fair_play_fingerprint) if self.fair_play_fingerprint else "",
                "observed_monotonic_ms": self.observed_monotonic_ms, "network_read_call_count": self.network_read_call_count,
                "source_authority": self.source_authority, "market_identity_status": self.market_identity_status,
                "account_identity_status": self.account_identity_status, "trading_enabled": self.trading_enabled,
                "contract_code_present": self.contract_code_present, "pending_nonce": self.pending_nonce,
                "gas_estimate": self.gas_estimate, "estimated_fee_wei": self.estimated_fee_wei,
                "native_balance_wei": self.native_balance_wei, "open_order_status": self.open_order_status,
                "drawdown_fraction": str(self.drawdown_fraction) if self.drawdown_fraction is not None else None,
                "preemptive_drawdown": str(self.preemptive_drawdown) if self.preemptive_drawdown is not None else None,
                "hard_drawdown_limit": str(self.hard_drawdown_limit) if self.hard_drawdown_limit is not None else None,
                "kill_switch_latched": self.kill_switch_latched, "emergency_exit_requested": self.emergency_exit_requested,
                "emergency_exit_completed": self.emergency_exit_completed,
                "gap_risk_status": self.gap_risk_status,
                "gap_risk_budget_approved": self.gap_risk_budget_approved,
                "gap_risk_blockers": list(self.gap_risk_blockers),
                "account_authority_status": self.account_authority_status,
                "open_order_status": self.open_order_status,
                "fills_status": self.fills_status,
                "public_market_call_count": self.public_market_call_count,
                "authenticated_account_call_count": self.authenticated_account_call_count,
                "read_only_rpc_call_count": self.read_only_rpc_call_count,
                "projected_shocked_drawdown": str(self.projected_shocked_drawdown) if self.projected_shocked_drawdown is not None else None,
                "maximum_total_fee_wei": self.maximum_total_fee_wei,
                "transaction_type": self.transaction_type}

    def __repr__(self) -> str:
        return "DreamDexZeroMutationRehearsalEvidence(<safe>)"


@dataclass(frozen=True, repr=False)
class DreamDexRehearsalCandidate:
    operation: str
    market_symbol: str
    side: str
    order_type: str
    price: Decimal
    quantity: Decimal
    notional: Decimal
    noncrossing: bool
    candidate_fingerprint: str
    native_value: Decimal = Decimal("0")
    maximum_transaction_fee: Decimal = Decimal("0")
    nonce: int | None = None
    transaction_type: str = "limit"
    rehearsal_only: bool = True
    non_executable: bool = True
    requires_full_evidence_recollection: bool = True
    requires_new_production_journal_intent: bool = True
    requires_new_nonce_reservation: bool = True
    requires_new_lease: bool = True
    requires_separate_approval_ceremony: bool = True

    def safe_dict(self) -> dict[str, Any]:
        return {"operation": self.operation, "market_symbol": self.market_symbol, "side": self.side,
                "order_type": self.order_type, "price": str(self.price), "quantity": str(self.quantity),
                "notional": str(self.notional), "noncrossing": self.noncrossing,
                "candidate_fingerprint": mask_hex_hash(self.candidate_fingerprint), "native_value": str(self.native_value),
                "maximum_transaction_fee": str(self.maximum_transaction_fee), "nonce": self.nonce,
                "transaction_type": self.transaction_type, "rehearsal_only": self.rehearsal_only,
                "non_executable": self.non_executable,
                "requires_full_evidence_recollection": self.requires_full_evidence_recollection,
                "requires_new_production_journal_intent": self.requires_new_production_journal_intent,
                "requires_new_nonce_reservation": self.requires_new_nonce_reservation,
                "requires_new_lease": self.requires_new_lease,
                "requires_separate_approval_ceremony": self.requires_separate_approval_ceremony}

    def __repr__(self) -> str:
        return "DreamDexRehearsalCandidate(<safe>)"


@dataclass(frozen=True, repr=False)
class DreamDexZeroMutationRehearsalResult:
    schema_version: str
    rehearsal_status: str
    chain_evidence_status: str = "unavailable"
    market_evidence_status: str = "unavailable"
    account_evidence_status: str = "unavailable"
    market_rules_status: str = "unavailable"
    trading_status: str = "unavailable"
    contract_code_status: str = "unavailable"
    pending_nonce_status: str = "unavailable"
    gas_estimate_status: str = "unavailable"
    fee_evidence_status: str = "unavailable"
    balance_status: str = "unavailable"
    risk_status: str = "unavailable"
    fair_play_status: str = "unavailable"
    runtime_launch_status: str = "unavailable"
    unsigned_request_status: str = "unavailable"
    envelope_status: str = "unavailable"
    preflight_status: str = "unavailable"
    approval_preview_status: str = "unavailable"
    approval_binding_status: str = "unavailable"
    production_journal_write_performed: bool = False
    temporary_journal_used: bool = False
    temporary_journal_removed: bool = False
    approval_prompt_performed: bool = False
    keystore_read_performed: bool = False
    password_prompt_performed: bool = False
    signer_invocation_count: int = 0
    submission_call_count: int = 0
    mutation_rpc_call_count: int = 0
    network_read_call_count: int = 0
    ready_for_human_review: bool = False
    ready_for_signer_invocation: bool = False
    ready_for_real_submission: bool = False
    rehearsal_fingerprint: str = ""
    authoritative: bool = False
    blockers: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    validation_errors: tuple[str, ...] = ()
    candidate_fingerprint: str | None = None
    gap_risk_status: str = "unavailable"
    gap_risk_budget_approved: bool | None = None
    mode: str = "fixture"
    public_market_call_count: int = 0
    authenticated_account_call_count: int = 0
    read_only_rpc_call_count: int = 0
    projected_shocked_drawdown: Decimal | None = None
    maximum_total_fee_wei: int | None = None
    transaction_type: str = "unresolved"
    source_authority: str = "non_authoritative"
    market_identity_status: str = "unavailable"
    account_identity_status: str = "unavailable"
    account_authority_status: str = "unresolved"
    open_order_status: str = "unavailable"
    fills_status: str = "unavailable"
    trading_enabled: bool | None = None
    orderbook_status: str = "unavailable"
    open_order_count: int | None = None

    @property
    def readiness_status(self) -> str:
        return "ready" if self.ready_for_human_review else "blocked"

    @property
    def mutation_call_count(self) -> int:
        return self.mutation_rpc_call_count

    @property
    def temporary_rehearsal_journal_used(self) -> bool:
        return self.temporary_journal_used

    @property
    def approval_prompt_shown(self) -> bool:
        return self.approval_prompt_performed

    @property
    def keystore_accessed(self) -> bool:
        return self.keystore_read_performed

    @property
    def password_requested(self) -> bool:
        return self.password_prompt_performed

    @property
    def submission_attempt_count(self) -> int:
        return self.submission_call_count

    @property
    def ready_for_signing(self) -> bool:
        return self.ready_for_signer_invocation

    @property
    def ready_for_submission(self) -> bool:
        return self.ready_for_real_submission

    def safe_dict(self) -> dict[str, Any]:
        return {"schema_version": self.schema_version, "rehearsal_status": self.rehearsal_status,
                "chain_evidence_status": self.chain_evidence_status, "market_evidence_status": self.market_evidence_status,
                "account_evidence_status": self.account_evidence_status, "market_rules_status": self.market_rules_status,
                "orderbook_status": self.orderbook_status,
                "trading_status": self.trading_status, "contract_code_status": self.contract_code_status,
                "pending_nonce_status": self.pending_nonce_status, "gas_estimate_status": self.gas_estimate_status,
                "fee_evidence_status": self.fee_evidence_status, "balance_status": self.balance_status,
                "risk_status": self.risk_status, "fair_play_status": self.fair_play_status,
                "runtime_launch_status": self.runtime_launch_status, "unsigned_request_status": self.unsigned_request_status,
                "envelope_status": self.envelope_status, "preflight_status": self.preflight_status,
                "approval_preview_status": self.approval_preview_status, "approval_binding_status": self.approval_binding_status,
                "production_journal_write_performed": False, "temporary_journal_used": False, "temporary_journal_removed": self.temporary_journal_removed,
                "approval_prompt_performed": False, "keystore_read_performed": False, "password_prompt_performed": False,
                "signer_invocation_count": 0, "submission_call_count": 0, "mutation_rpc_call_count": 0,
                "network_read_call_count": self.network_read_call_count, "ready_for_human_review": self.ready_for_human_review,
                "ready_for_signer_invocation": False, "ready_for_real_submission": False, "rehearsal_fingerprint": mask_hex_hash(self.rehearsal_fingerprint),
                "authoritative": False, "blockers": list(self.blockers), "validation_errors": list(self.validation_errors),
                "gap_risk_status": self.gap_risk_status,
                "gap_risk_budget_approved": self.gap_risk_budget_approved,
                "mode": self.mode,
                "public_market_call_count": self.public_market_call_count,
                "authenticated_account_call_count": self.authenticated_account_call_count,
                "read_only_rpc_call_count": self.read_only_rpc_call_count,
                "projected_shocked_drawdown": str(self.projected_shocked_drawdown) if self.projected_shocked_drawdown is not None else None,
                "maximum_total_fee_wei": self.maximum_total_fee_wei,
                "transaction_type": self.transaction_type,
                "source_authority": self.source_authority,
                "market_identity_status": self.market_identity_status,
                "account_identity_status": self.account_identity_status,
                "account_authority_status": self.account_authority_status,
                "open_order_status": self.open_order_status,
                "fills_status": self.fills_status,
                "trading_enabled": self.trading_enabled,
                "open_order_count": self.open_order_count,
                "candidate_fingerprint": mask_hex_hash(self.candidate_fingerprint),
                # Compatibility aliases for early offline callers.
                "readiness_status": self.readiness_status, "mutation_call_count": self.mutation_call_count,
                "temporary_rehearsal_journal_used": self.temporary_rehearsal_journal_used,
                "approval_prompt_shown": self.approval_prompt_shown, "keystore_accessed": self.keystore_accessed,
                "password_requested": self.password_requested, "submission_attempt_count": self.submission_attempt_count,
                "ready_for_signing": self.ready_for_signing, "ready_for_submission": self.ready_for_submission}

    def __repr__(self) -> str:
        return "DreamDexZeroMutationRehearsalResult(<safe>)"


def build_rehearsal_candidate(*, market_symbol: str, side: str, price: Any, quantity: Any,
                              market_rules: Mapping[str, Any], best_bid: Any = None,
                              best_ask: Any = None, policy: DreamDexZeroMutationRehearsalPolicy | None = None) -> DreamDexRehearsalCandidate | None:
    policy = policy or DreamDexZeroMutationRehearsalPolicy(required_market_symbol=market_symbol)
    if market_symbol != policy.required_market_symbol or side != "BUY":
        return None
    required = ("tick_size", "quantity_step", "minimum_quantity", "minimum_notional")
    if any(_d(market_rules.get(key)) is None or _d(market_rules.get(key)) <= 0 for key in required):
        return None
    p, q = _d(price), _d(quantity)
    if p is None or q is None or p <= 0 or q <= 0:
        return None
    tick, step, minimum, minimum_notional = (_d(market_rules[k]) for k in required)
    if p % tick != 0 or q < minimum or q % step != 0:
        return None
    if side == "BUY" and best_ask is not None and p >= _d(best_ask):
        return None
    if side == "SELL" and best_bid is not None and p <= _d(best_bid):
        return None
    notional = p * q
    if notional < minimum_notional or notional > policy.maximum_order_notional:
        return None
    payload = {"operation": "place_order", "market_symbol": market_symbol, "side": side, "order_type": "limit", "price": str(p), "quantity": str(q)}
    return DreamDexRehearsalCandidate("place_order", market_symbol, side, "limit", p, q, notional, True, _safe_fp(payload))


def collect_live_read_only_rehearsal_evidence(collector: Callable[[], DreamDexZeroMutationRehearsalEvidence | Mapping[str, Any]]) -> DreamDexZeroMutationRehearsalEvidence:
    value = collector()
    if isinstance(value, DreamDexZeroMutationRehearsalEvidence):
        return value
    if isinstance(value, Mapping):
        return DreamDexZeroMutationRehearsalEvidence(**dict(value))
    raise TypeError("typed_read_only_rehearsal_evidence_required")


def _reader_value(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _safe_source_status(value: Any, default: str = "unavailable") -> str:
    status = _reader_value(value, "status", default)
    return str(status) if status is not None else default


def _best_level(levels: Any, *, reverse: bool) -> Decimal | None:
    """Read one safe price from a typed/read-only order-book snapshot."""
    if isinstance(levels, Mapping):
        levels = [{"price": price, "quantity": quantity} for price, quantity in levels.items()]
    if not isinstance(levels, (list, tuple)):
        return None
    prices: list[Decimal] = []
    for level in levels:
        if isinstance(level, Mapping):
            raw = level.get("price", level.get("p"))
            raw_quantity = level.get("quantity", level.get("qty", level.get("amount", level.get("q"))))
        elif isinstance(level, (list, tuple)) and level:
            raw = level[0]
            raw_quantity = level[1] if len(level) > 1 else None
        else:
            raw = None
            raw_quantity = None
        parsed = _d(raw)
        quantity = _d(raw_quantity)
        if parsed is not None and parsed > 0 and (raw_quantity is None or (quantity is not None and quantity > 0)):
            prices.append(parsed)
    if not prices:
        return None
    return max(prices) if reverse else min(prices)


def collect_live_read_only_rehearsal_evidence_from_dependencies(
    dependencies: DreamDexLiveReadOnlyRehearsalDependencies,
) -> DreamDexZeroMutationRehearsalEvidence:
    """Collect only read-only evidence from the explicit dependency bundle.

    Source exceptions are intentionally reduced to unavailable statuses; raw
    exception text, URLs, headers, tokens, and payload values never enter the
    evidence model.
    """
    started = dependencies.monotonic_clock()
    public_calls = auth_calls = rpc_calls = 0
    try:
        market = dependencies.public_market_reader()
        public_calls = 1
    except Exception:
        market = None
    try:
        account = dependencies.authenticated_account_reader()
        auth_calls = 1
    except Exception:
        account = None
    try:
        rpc = dependencies.typed_rpc_preflight_reader()
        rpc_calls = int(_reader_value(rpc, "read_only_rpc_call_count", 0) or 0)
        if rpc_calls <= 0:
            rpc_calls = 1
    except Exception:
        rpc = None

    metadata = _reader_value(market, "metadata")
    rules = _reader_value(metadata, "trading_rules")
    market_status = _safe_source_status(market)
    rules_available = bool(_reader_value(rules, "available", False))
    book = _reader_value(market, "orderbook")
    if isinstance(book, Mapping):
        bids = book.get("bids", book.get("bid", []))
        asks = book.get("asks", book.get("ask", []))
    else:
        bids = asks = []
    best_bid = _best_level(bids, reverse=True)
    best_ask = _best_level(asks, reverse=False)
    spread_bps = None
    if best_bid is not None and best_ask is not None and best_bid < best_ask:
        spread_bps = (best_ask - best_bid) / ((best_ask + best_bid) / Decimal("2")) * Decimal("10000")
    maximum_spread_bps = _d(dependencies.safe_config.get("maximum_spread_bps", "1000")) or Decimal("1000")
    orderbook_status = "available" if best_bid is not None and best_ask is not None and best_bid < best_ask and spread_bps is not None and spread_bps <= maximum_spread_bps else "unavailable"
    market_symbol = _reader_value(metadata, "symbol")
    market_pool = _reader_value(metadata, "pool_contract", _reader_value(metadata, "pool_address"))
    expected_symbol = dependencies.safe_config.get("required_market_symbol")
    expected_pool = dependencies.safe_config.get("required_market_address")
    market_identity = "confirmed" if market_symbol and (expected_symbol is None or market_symbol == expected_symbol) and market_pool and (expected_pool is None or market_pool == expected_pool) else "unavailable"
    trading_enabled = _reader_value(rules, "trading_enabled") is True
    observed_at = _reader_value(metadata, "observed_at", _reader_value(market, "observed_at"))
    market_age_ms = None
    if observed_at is not None:
        try:
            from datetime import datetime, timezone
            now = datetime.now(timezone.utc)
            if getattr(observed_at, "tzinfo", None) is None:
                observed_at = observed_at.replace(tzinfo=timezone.utc)
            market_age_ms = max(0, int((now - observed_at).total_seconds() * 1000))
        except Exception:
            market_age_ms = None

    account_identity = _reader_value(account, "account_address_semantics", "unresolved")
    account_authority = "confirmed" if account_identity in {"resolved", "authoritative"} else "unresolved"
    open_order_status = str(_reader_value(account, "open_orders_status", "unavailable"))
    fills_status = str(_reader_value(account, "fills_status", "unavailable"))
    account_status = "available" if account is not None and account_authority == "confirmed" and open_order_status in {"available", "confirmed", "available_empty"} and fills_status in {"available", "confirmed", "available_empty"} else "unavailable"
    account_age_ms = None
    account_observed = _reader_value(account, "observed_at")
    if account_observed is not None:
        try:
            from datetime import datetime, timezone
            now = datetime.now(timezone.utc)
            if getattr(account_observed, "tzinfo", None) is None:
                account_observed = account_observed.replace(tzinfo=timezone.utc)
            account_age_ms = max(0, int((now - account_observed).total_seconds() * 1000))
        except Exception:
            account_age_ms = None

    rpc_map = rpc if isinstance(rpc, Mapping) else {}
    rpc_status = str(rpc_map.get("status", "unavailable"))
    gap_map = dependencies.risk_snapshot
    fair_map = dependencies.fair_play_snapshot
    gap_status = str(gap_map.get("status", "unavailable"))
    gap_approved = gap_map.get("gap_risk_budget_approved")
    projected_dd = _d(gap_map.get("projected_shocked_drawdown"))
    fair_status = str(fair_map.get("status", "unavailable"))
    elapsed = max(0.0, dependencies.monotonic_clock() - started)
    _ = elapsed  # monotonic timing is intentionally not exposed as a value.
    return DreamDexZeroMutationRehearsalEvidence(
        market_status=market_status if market is not None and orderbook_status == "available" else "unavailable",
        orderbook_status=orderbook_status,
        account_status=account_status,
        rpc_status=rpc_status,
        chain_id=rpc_map.get("chain_id"),
        target_code_status=str(rpc_map.get("target_code_status", "unavailable")),
        pending_nonce_status=str(rpc_map.get("pending_nonce_status", "unavailable")),
        native_balance_status=str(rpc_map.get("native_balance_status", "unavailable")),
        gas_estimate_status=str(rpc_map.get("gas_estimate_status", "unavailable")),
        fee_status=str(rpc_map.get("fee_status", "unavailable")),
        market_rules_status="available" if rules_available else "unavailable",
        runtime_gate_status=str(gap_map.get("runtime_gate_status", "unavailable")),
        risk_status=str(gap_map.get("risk_status", "unavailable")),
        fair_play_status=fair_status,
        market_age_ms=market_age_ms,
        account_age_ms=account_age_ms,
        network_read_call_count=public_calls + auth_calls + rpc_calls,
        source_authority="authoritative" if market_status == "available" and rules_available else "non_authoritative",
        market_identity_status=market_identity,
        account_identity_status=account_authority,
        trading_enabled=trading_enabled,
        contract_code_present=rpc_map.get("contract_code_present"),
        pending_nonce=rpc_map.get("pending_nonce"),
        gas_estimate=rpc_map.get("gas_estimate"),
        estimated_fee_wei=rpc_map.get("maximum_total_fee_wei"),
        native_balance_wei=rpc_map.get("native_balance_wei"),
        open_order_status=open_order_status,
        fills_status=fills_status,
        open_order_count=_reader_value(account, "open_order_count"),
        drawdown_fraction=_d(gap_map.get("drawdown_fraction")),
        preemptive_drawdown=_d(gap_map.get("preemptive_drawdown")),
        hard_drawdown_limit=_d(gap_map.get("hard_drawdown_limit")),
        kill_switch_latched=bool(gap_map.get("kill_switch_latched", False)),
        emergency_exit_requested=bool(gap_map.get("emergency_exit_requested", False)),
        emergency_exit_completed=bool(gap_map.get("emergency_exit_completed", False)),
        gap_risk_status=gap_status,
        gap_risk_budget_approved=gap_approved,
        gap_risk_blockers=tuple(str(item) for item in gap_map.get("blockers", ())),
        account_authority_status=account_authority,
        public_market_call_count=public_calls,
        authenticated_account_call_count=auth_calls,
        read_only_rpc_call_count=rpc_calls,
        projected_shocked_drawdown=projected_dd,
        maximum_total_fee_wei=rpc_map.get("maximum_total_fee_wei"),
        transaction_type=str(rpc_map.get("transaction_type", "unresolved")),
    )


def run_zero_mutation_rehearsal(*, policy: DreamDexZeroMutationRehearsalPolicy,
                                evidence: DreamDexZeroMutationRehearsalEvidence,
                                candidate: DreamDexRehearsalCandidate | None = None,
                                execute_read_only: bool = False,
                                collector: Callable[[], DreamDexZeroMutationRehearsalEvidence | Mapping[str, Any]] | None = None,
                                mode: str = "fixture") -> DreamDexZeroMutationRehearsalResult:
    if mode not in {"fixture", "live-read-only"}:
        raise ValueError("invalid_rehearsal_mode")
    if execute_read_only:
        if collector is None:
            return _result(policy, evidence, ("read_only_collector_unavailable",), candidate, mode=mode)
        evidence = collect_live_read_only_rehearsal_evidence(collector)
    blockers: list[str] = []
    if evidence.market_status != "available" or evidence.source_authority != "authoritative" or (evidence.market_age_ms is not None and evidence.market_age_ms > policy.maximum_market_age_ms):
        blockers.append("market_evidence_unavailable_or_stale")
    if evidence.orderbook_status != "available":
        blockers.append("orderbook_evidence_unavailable")
    if evidence.account_status != "available" or evidence.account_age_ms is None or evidence.account_age_ms > policy.maximum_account_age_ms:
        blockers.append("account_evidence_unavailable_or_stale")
    if policy.require_authoritative_account_data and evidence.account_authority_status not in {"confirmed", "authoritative"}:
        blockers.append("account_identity_not_authoritative")
    if policy.require_authoritative_account_data and evidence.open_order_status not in {"available", "confirmed", "available_empty"}:
        blockers.append("open_order_evidence_unavailable")
    if policy.require_authoritative_account_data and evidence.fills_status not in {"available", "confirmed", "available_empty"}:
        blockers.append("fills_evidence_unavailable")
    if evidence.open_order_count is not None and evidence.open_order_count > policy.maximum_open_orders:
        blockers.append("maximum_open_orders_exceeded")
    if policy.require_authoritative_market_data and evidence.market_identity_status not in {"confirmed", "source_confirmed"}:
        blockers.append("market_identity_unconfirmed")
    if policy.require_authoritative_account_data and evidence.account_identity_status not in {"confirmed", "source_confirmed"}:
        blockers.append("account_identity_unconfirmed")
    for field_name, blocker in (("rpc_status", "rpc_evidence_unavailable"), ("target_code_status", "target_contract_code_unavailable"), ("pending_nonce_status", "pending_nonce_unavailable"), ("native_balance_status", "balance_evidence_unavailable"), ("gas_estimate_status", "gas_estimate_unavailable"), ("fee_status", "fee_evidence_unavailable"), ("market_rules_status", "market_rules_unavailable"), ("runtime_gate_status", "runtime_launch_gate_blocked"), ("risk_status", "risk_unavailable"), ("fair_play_status", "fair_play_unavailable")):
        if getattr(evidence, field_name) != "available":
            blockers.append(blocker)
    if evidence.chain_id != policy.required_chain_id:
        blockers.append("rpc_chain_mismatch")
    if evidence.kill_switch_latched:
        blockers.append("kill_switch_latched")
    if evidence.emergency_exit_requested and not evidence.emergency_exit_completed:
        blockers.append("emergency_exit_unresolved")
    if evidence.drawdown_fraction is None:
        blockers.append("drawdown_evidence_unavailable")
    elif evidence.hard_drawdown_limit is None or evidence.drawdown_fraction > evidence.hard_drawdown_limit:
        blockers.append("drawdown_above_hard_limit")
    elif evidence.preemptive_drawdown is None or evidence.drawdown_fraction >= evidence.preemptive_drawdown:
        blockers.append("drawdown_above_preemptive_threshold")
    if evidence.gap_risk_status != "available" or evidence.gap_risk_budget_approved is not True:
        blockers.append("gap_risk_unavailable")
    if evidence.projected_shocked_drawdown is None:
        blockers.append("projected_shocked_drawdown_unavailable")
    elif evidence.hard_drawdown_limit is None or evidence.projected_shocked_drawdown >= evidence.hard_drawdown_limit:
        blockers.append("projected_shocked_drawdown_above_hard_limit")
    if policy.require_trading_enabled and evidence.trading_enabled is not True:
        blockers.append("trading_status_unavailable")
    if policy.require_contract_code and evidence.contract_code_present is not True:
        blockers.append("target_contract_code_missing")
    if policy.require_pending_nonce and evidence.pending_nonce is None:
        blockers.append("pending_nonce_unavailable")
    if policy.require_gas_estimate and evidence.gas_estimate is None:
        blockers.append("gas_estimate_unavailable")
    if policy.require_fee_evidence and evidence.estimated_fee_wei is None:
        blockers.append("fee_evidence_unavailable")
    if evidence.estimated_fee_wei is not None and evidence.estimated_fee_wei > policy.maximum_fee_wei:
        blockers.append("transaction_fee_limit_exceeded")
    if evidence.maximum_total_fee_wei is not None and evidence.maximum_total_fee_wei > policy.maximum_fee_wei:
        blockers.append("maximum_total_fee_limit_exceeded")
    if policy.require_balance_evidence and evidence.native_balance_wei is None:
        blockers.append("balance_evidence_unavailable")
    if evidence.native_balance_wei is not None and evidence.estimated_fee_wei is not None and evidence.native_balance_wei < evidence.estimated_fee_wei:
        blockers.append("native_fee_balance_insufficient")
    if candidate is None:
        blockers.append("candidate_unavailable_or_invalid")
    elif candidate.noncrossing is not True or candidate.notional > policy.maximum_order_notional:
        blockers.append("candidate_order_policy_rejected")
    return _result(policy, evidence, tuple(dict.fromkeys(blockers)), candidate, mode=mode)


def _result(policy: DreamDexZeroMutationRehearsalPolicy, evidence: DreamDexZeroMutationRehearsalEvidence,
            blockers: tuple[str, ...], candidate: DreamDexRehearsalCandidate | None, *, mode: str = "fixture") -> DreamDexZeroMutationRehearsalResult:
    ready = not blockers
    payload = {"policy": policy.safe_dict(), "evidence": {k: v for k, v in evidence.safe_dict().items() if k not in {"observed_monotonic_ms"}}, "candidate": candidate.safe_dict() if candidate else None}
    fp = _safe_fp(payload)
    status = "available" if ready else "unavailable"
    return DreamDexZeroMutationRehearsalResult(
        schema_version=policy.schema_version,
        rehearsal_status="ready_for_human_review" if ready else "blocked",
        chain_evidence_status="available" if evidence.chain_id == policy.required_chain_id else "unavailable",
        market_evidence_status=evidence.market_status,
        orderbook_status=evidence.orderbook_status,
        account_evidence_status=evidence.account_status,
        market_rules_status=evidence.market_rules_status,
        trading_status=status if evidence.runtime_gate_status == "available" else evidence.runtime_gate_status,
        contract_code_status=evidence.target_code_status,
        pending_nonce_status=evidence.pending_nonce_status,
        gas_estimate_status=evidence.gas_estimate_status,
        fee_evidence_status=evidence.fee_status,
        balance_status=evidence.native_balance_status,
        risk_status=evidence.risk_status,
        fair_play_status=evidence.fair_play_status,
        runtime_launch_status=evidence.runtime_gate_status,
        unsigned_request_status=status if candidate else "unavailable",
        envelope_status=status if ready else "unavailable",
        preflight_status=status if ready else "unavailable",
        approval_preview_status=status if ready else "unavailable",
        approval_binding_status=status if ready else "unavailable",
        production_journal_write_performed=False,
        temporary_journal_used=False,
        temporary_journal_removed=False,
        approval_prompt_performed=False,
        keystore_read_performed=False,
        password_prompt_performed=False,
        signer_invocation_count=0,
        submission_call_count=0,
        mutation_rpc_call_count=0,
        network_read_call_count=evidence.network_read_call_count,
        ready_for_human_review=ready,
        ready_for_signer_invocation=False,
        ready_for_real_submission=False,
        rehearsal_fingerprint=fp,
        authoritative=False,
        blockers=blockers,
        validation_errors=(),
        candidate_fingerprint=candidate.candidate_fingerprint if candidate else None,
        gap_risk_status=evidence.gap_risk_status,
        gap_risk_budget_approved=evidence.gap_risk_budget_approved,
        mode=mode,
        public_market_call_count=evidence.public_market_call_count,
        authenticated_account_call_count=evidence.authenticated_account_call_count,
        read_only_rpc_call_count=evidence.read_only_rpc_call_count,
        projected_shocked_drawdown=evidence.projected_shocked_drawdown,
        maximum_total_fee_wei=evidence.maximum_total_fee_wei,
        transaction_type=evidence.transaction_type,
        source_authority=evidence.source_authority,
        market_identity_status=evidence.market_identity_status,
        account_identity_status=evidence.account_identity_status,
        account_authority_status=evidence.account_authority_status,
        open_order_status=evidence.open_order_status,
        fills_status=evidence.fills_status,
        trading_enabled=evidence.trading_enabled,
        open_order_count=evidence.open_order_count,
    )


__all__ = ["READ_ONLY_REHEARSAL_RPC_ALLOWLIST", "READ_ONLY_REHEARSAL_FORBIDDEN_PREFIXES", "READ_ONLY_REHEARSAL_FORBIDDEN_METHODS"] + [name for name in globals() if name.startswith("DreamDex") or name.startswith("build_") or name.startswith("collect_") or name.startswith("run_")]
