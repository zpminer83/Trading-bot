"""Bounded, read-only transaction receipt and event confirmation.

This module accepts only a durable submission record and a journal.  It never
accepts a free-form hash, sends a transaction, polls indefinitely, or stores
raw receipt/log data.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from hashlib import sha256
import json
import time
from typing import Any, Mapping, Protocol, Sequence

from bot.execution.dreamdex_direct_order_encoding import ORDER_CANCELLED_TOPIC, ORDER_PLACED_TOPIC, parse_order_cancelled_event, parse_order_placed_event
from bot.execution.dreamdex_execution_journal import DreamDexExecutionJournal, JournalState
from bot.execution.dreamdex_execution_primitives import mask_evm_address, mask_hex_hash, validate_evm_address, validate_tx_hash
from bot.execution.dreamdex_readonly_rpc import DreamDexRpcError, parse_rpc_quantity

SCHEMA_VERSION = "1"
CONFIRMATION_STATUSES = frozenset({"pending", "confirmed_success", "confirmed_reverted", "confirmed_missing_event", "reorg_detected", "recovery_required", "unavailable"})
OPERATIONS = frozenset({"place_order", "cancel_order", "reduce_order"})
_HEX_HASH_LEN = 66


def _tuple(values: Sequence[str] | None) -> tuple[str, ...]:
    return tuple(dict.fromkeys(str(value) for value in (values or ()) if str(value)))


def _fp(value: Any, domain: str) -> str:
    return sha256((domain + ":" + json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)).encode()).hexdigest()


def _mask_hash(value: str | None) -> str:
    return mask_hex_hash(value)


def _hash(value: Any, field: str) -> str:
    parsed = validate_tx_hash(value, field=field)
    if parsed is None:
        raise ValueError(f"{field}: missing")
    return parsed


def _optional_hash(value: Any, field: str) -> str | None:
    return None if value is None else _hash(value, field)


def _safe_int(value: Any, field: str, *, required: bool = False) -> int | None:
    if value is None and not required:
        return None
    if isinstance(value, bool):
        raise ValueError(f"{field}: bool_not_integer")
    if isinstance(value, int):
        if value < 0:
            raise ValueError(f"{field}: negative")
        return value
    return parse_rpc_quantity(value, field=field)


def _safe_data(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.startswith("0x") or len(value) % 2:
        raise ValueError(f"{field}: malformed_hex")
    try:
        bytes.fromhex(value[2:])
    except ValueError as exc:
        raise ValueError(f"{field}: malformed_hex") from exc
    return value.lower()


class TransactionConfirmationRpc(Protocol):
    def get_transaction_receipt(self, transaction_hash: str) -> Mapping[str, Any] | None: ...
    def get_block_by_number(self, block_number: int) -> Mapping[str, Any] | None: ...
    def get_block_number(self) -> int: ...


@dataclass(frozen=True, repr=False)
class DreamDexTransactionConfirmationPolicy:
    schema_version: str = SCHEMA_VERSION
    required_chain_id: int | None = None
    required_target_address: str | None = None
    minimum_confirmations: int = 1
    maximum_observation_attempts: int = 1
    observation_interval_ms: int = 0
    maximum_monitor_duration_ms: int = 1000
    require_canonical_block_match: bool = True
    require_stable_receipt: bool = True
    require_expected_contract_event: bool = True
    require_exact_order_id_for_cancel: bool = True
    treat_reverted_as_terminal: bool = True
    persist_pending_observation: bool = False
    automatic_resend_allowed: bool = False
    replacement_allowed: bool = False
    authoritative: bool = False
    unresolved_reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError("confirmation_policy_schema_invalid")
        if self.required_chain_id is not None and (isinstance(self.required_chain_id, bool) or not isinstance(self.required_chain_id, int) or self.required_chain_id < 0):
            raise ValueError("required_chain_id_invalid")
        if self.required_target_address is not None:
            object.__setattr__(self, "required_target_address", validate_evm_address(self.required_target_address, field="required_target_address"))
        for name in ("minimum_confirmations", "maximum_observation_attempts", "observation_interval_ms", "maximum_monitor_duration_ms"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name}_invalid")
        if self.minimum_confirmations < 1 or self.maximum_observation_attempts < 1 or self.maximum_monitor_duration_ms < 1:
            raise ValueError("confirmation_policy_limit_invalid")
        if self.automatic_resend_allowed or self.replacement_allowed:
            raise ValueError("resend_and_replacement_disabled")
        object.__setattr__(self, "authoritative", False)
        object.__setattr__(self, "unresolved_reasons", _tuple(self.unresolved_reasons))

    def safe_dict(self) -> dict[str, Any]:
        return {"schema_version": self.schema_version, "required_chain_id": self.required_chain_id, "required_target_address_masked": mask_evm_address(self.required_target_address), "minimum_confirmations": self.minimum_confirmations, "maximum_observation_attempts": self.maximum_observation_attempts, "observation_interval_ms": self.observation_interval_ms, "maximum_monitor_duration_ms": self.maximum_monitor_duration_ms, "require_canonical_block_match": self.require_canonical_block_match, "require_stable_receipt": self.require_stable_receipt, "require_expected_contract_event": self.require_expected_contract_event, "require_exact_order_id_for_cancel": self.require_exact_order_id_for_cancel, "treat_reverted_as_terminal": self.treat_reverted_as_terminal, "persist_pending_observation": self.persist_pending_observation, "automatic_resend_allowed": False, "replacement_allowed": False, "authoritative": False, "unresolved_reasons": self.unresolved_reasons}

    def __repr__(self) -> str:
        return f"DreamDexTransactionConfirmationPolicy(minimum_confirmations={self.minimum_confirmations!r}, maximum_observation_attempts={self.maximum_observation_attempts!r}, authoritative=False)"


@dataclass(frozen=True, repr=False)
class DreamDexTransactionReceiptEvidence:
    schema_version: str = SCHEMA_VERSION
    transaction_hash: str | None = None
    receipt_found: bool = False
    receipt_status: str = "unavailable"
    block_number: int | None = None
    block_hash: str | None = None
    transaction_index: int | None = None
    target_address: str | None = None
    cumulative_gas_used: int | None = None
    gas_used: int | None = None
    effective_gas_price_wei: int | None = None
    log_count: int = 0
    expected_contract_log_count: int = 0
    receipt_fingerprint: str = ""
    source_status: str = "unavailable"
    authoritative: bool = False
    validation_errors: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.transaction_hash is not None:
            object.__setattr__(self, "transaction_hash", _hash(self.transaction_hash, "transaction_hash"))
        if self.block_hash is not None:
            object.__setattr__(self, "block_hash", _hash(self.block_hash, "block_hash"))
        if self.target_address is not None:
            object.__setattr__(self, "target_address", validate_evm_address(self.target_address, field="target_address"))
        for name in ("block_number", "transaction_index", "cumulative_gas_used", "gas_used", "effective_gas_price_wei", "log_count", "expected_contract_log_count"):
            value = getattr(self, name)
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
                raise ValueError(f"{name}_invalid")
        if self.receipt_status not in {"unavailable", "success", "reverted", "malformed"}:
            raise ValueError("receipt_status_invalid")
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError("receipt_schema_invalid")
        object.__setattr__(self, "validation_errors", _tuple(self.validation_errors))

    def safe_dict(self) -> dict[str, Any]:
        return {"schema_version": self.schema_version, "transaction_hash": _mask_hash(self.transaction_hash), "receipt_found": self.receipt_found, "receipt_status": self.receipt_status, "block_number": self.block_number, "block_hash": _mask_hash(self.block_hash), "transaction_index": self.transaction_index, "target_address_masked": mask_evm_address(self.target_address), "cumulative_gas_used": self.cumulative_gas_used, "gas_used": self.gas_used, "effective_gas_price_wei": self.effective_gas_price_wei, "log_count": self.log_count, "expected_contract_log_count": self.expected_contract_log_count, "receipt_fingerprint": _mask_hash(self.receipt_fingerprint), "source_status": self.source_status, "authoritative": self.authoritative, "validation_errors": self.validation_errors}

    def __repr__(self) -> str:
        return f"DreamDexTransactionReceiptEvidence(transaction_hash={_mask_hash(self.transaction_hash)!r}, receipt_status={self.receipt_status!r}, block_number={self.block_number!r})"


@dataclass(frozen=True, repr=False)
class DreamDexCanonicalBlockEvidence:
    schema_version: str = SCHEMA_VERSION
    receipt_block_number: int | None = None
    receipt_block_hash: str | None = None
    canonical_block_found: bool = False
    canonical_block_hash: str | None = None
    block_hash_match: bool | None = None
    latest_block_number: int | None = None
    confirmation_count: int = 0
    required_confirmation_count: int = 0
    finality_reached: bool = False
    block_evidence_fingerprint: str = ""
    source_status: str = "unavailable"
    authoritative: bool = False
    validation_errors: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.receipt_block_hash is not None: object.__setattr__(self, "receipt_block_hash", _hash(self.receipt_block_hash, "receipt_block_hash"))
        if self.canonical_block_hash is not None: object.__setattr__(self, "canonical_block_hash", _hash(self.canonical_block_hash, "canonical_block_hash"))
        for name in ("receipt_block_number", "latest_block_number", "confirmation_count", "required_confirmation_count"):
            value = getattr(self, name)
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0): raise ValueError(f"{name}_invalid")
        object.__setattr__(self, "validation_errors", _tuple(self.validation_errors))

    def safe_dict(self) -> dict[str, Any]:
        return {"schema_version": self.schema_version, "receipt_block_number": self.receipt_block_number, "receipt_block_hash": _mask_hash(self.receipt_block_hash), "canonical_block_found": self.canonical_block_found, "canonical_block_hash": _mask_hash(self.canonical_block_hash), "block_hash_match": self.block_hash_match, "latest_block_number": self.latest_block_number, "confirmation_count": self.confirmation_count, "required_confirmation_count": self.required_confirmation_count, "finality_reached": self.finality_reached, "block_evidence_fingerprint": _mask_hash(self.block_evidence_fingerprint), "source_status": self.source_status, "authoritative": self.authoritative, "validation_errors": self.validation_errors}

    def __repr__(self) -> str:
        return f"DreamDexCanonicalBlockEvidence(receipt_block_number={self.receipt_block_number!r}, confirmation_count={self.confirmation_count!r}, finality_reached={self.finality_reached!r})"


@dataclass(frozen=True, repr=False)
class DreamDexOrderContractEventEvidence:
    schema_version: str = SCHEMA_VERSION
    expected_operation: str = "place_order"
    expected_event_type: str = "OrderPlaced"
    event_found: bool = False
    matching_event_count: int = 0
    conflicting_event_count: int = 0
    contract_address: str | None = None
    transaction_hash: str | None = None
    block_number: int | None = None
    block_hash: str | None = None
    order_id: int | None = None
    expected_order_id: int | None = None
    order_id_match: bool | None = None
    event_fingerprint: str = ""
    source_status: str = "unavailable"
    authoritative: bool = False
    validation_errors: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.expected_operation not in OPERATIONS: raise ValueError("expected_operation_invalid")
        expected = {"place_order": "OrderPlaced", "cancel_order": "OrderCancelled", "reduce_order": "ReduceUnavailable"}[self.expected_operation]
        if self.expected_event_type != expected: raise ValueError("expected_event_type_invalid")
        if self.contract_address is not None: object.__setattr__(self, "contract_address", validate_evm_address(self.contract_address, field="contract_address"))
        if self.transaction_hash is not None: object.__setattr__(self, "transaction_hash", _hash(self.transaction_hash, "transaction_hash"))
        if self.block_hash is not None: object.__setattr__(self, "block_hash", _hash(self.block_hash, "block_hash"))
        for name in ("block_number", "order_id", "expected_order_id", "matching_event_count", "conflicting_event_count"):
            value = getattr(self, name)
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0): raise ValueError(f"{name}_invalid")
        object.__setattr__(self, "validation_errors", _tuple(self.validation_errors))

    def safe_dict(self) -> dict[str, Any]:
        return {"schema_version": self.schema_version, "expected_operation": self.expected_operation, "expected_event_type": self.expected_event_type, "event_found": self.event_found, "matching_event_count": self.matching_event_count, "conflicting_event_count": self.conflicting_event_count, "contract_address_masked": mask_evm_address(self.contract_address), "transaction_hash": _mask_hash(self.transaction_hash), "block_number": self.block_number, "block_hash": _mask_hash(self.block_hash), "order_id_present": self.order_id is not None, "expected_order_id_present": self.expected_order_id is not None, "order_id_match": self.order_id_match, "event_fingerprint": _mask_hash(self.event_fingerprint), "source_status": self.source_status, "authoritative": self.authoritative, "validation_errors": self.validation_errors}

    def __repr__(self) -> str:
        return f"DreamDexOrderContractEventEvidence(event_type={self.expected_event_type!r}, event_found={self.event_found!r}, order_id_confirmed={self.order_id is not None and self.order_id_match is not False!r})"


@dataclass(frozen=True, repr=False)
class DreamDexTransactionConfirmationResult:
    schema_version: str
    status: str
    intent_id: str
    submission_id: str
    transaction_hash: str
    receipt_evidence: DreamDexTransactionReceiptEvidence
    block_evidence: DreamDexCanonicalBlockEvidence
    event_evidence: DreamDexOrderContractEventEvidence
    journal_state_before: str
    journal_state_after: str
    observation_count: int
    receipt_stable: bool
    reorg_detected: bool
    transaction_pending: bool
    transaction_reverted: bool
    expected_event_confirmed: bool
    order_id_confirmed: bool
    finality_reached: bool
    confirmation_complete: bool
    ready_for_order_reconciliation: bool
    ready_for_new_execution_action: bool
    result_fingerprint: str
    authoritative: bool
    blockers: tuple[str, ...] = ()
    validation_errors: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.status not in CONFIRMATION_STATUSES: raise ValueError("confirmation_status_invalid")
        object.__setattr__(self, "blockers", _tuple(self.blockers)); object.__setattr__(self, "validation_errors", _tuple(self.validation_errors))

    def safe_dict(self) -> dict[str, Any]:
        return {"schema_version": self.schema_version, "status": self.status, "intent_id": mask_hex_hash(self.intent_id), "submission_id": mask_hex_hash(self.submission_id), "transaction_hash": _mask_hash(self.transaction_hash), "receipt_evidence": self.receipt_evidence.safe_dict(), "block_evidence": self.block_evidence.safe_dict(), "event_evidence": self.event_evidence.safe_dict(), "journal_state_before": self.journal_state_before, "journal_state_after": self.journal_state_after, "observation_count": self.observation_count, "receipt_stable": self.receipt_stable, "reorg_detected": self.reorg_detected, "transaction_pending": self.transaction_pending, "transaction_reverted": self.transaction_reverted, "expected_event_confirmed": self.expected_event_confirmed, "order_id_confirmed": self.order_id_confirmed, "finality_reached": self.finality_reached, "confirmation_complete": self.confirmation_complete, "ready_for_order_reconciliation": self.ready_for_order_reconciliation, "ready_for_new_execution_action": False, "result_fingerprint": mask_hex_hash(self.result_fingerprint), "authoritative": self.authoritative, "blockers": self.blockers, "validation_errors": self.validation_errors}

    def __repr__(self) -> str:
        return f"DreamDexTransactionConfirmationResult(status={self.status!r}, transaction_hash={_mask_hash(self.transaction_hash)!r}, confirmation_complete={self.confirmation_complete!r})"


@dataclass(frozen=True, repr=False)
class DreamDexTransactionConfirmationPreview:
    confirmation_feature: str = "available_offline"
    observation_execution_performed: bool = False
    rpc_calls_performed: bool = False
    receipt_found: bool = False
    receipt_status: str = "unavailable"
    canonical_block_match: bool | None = None
    confirmation_count: int = 0
    required_confirmation_count: int | None = None
    finality_reached: bool = False
    expected_event_type: str = "unavailable"
    expected_event_found: bool = False
    order_id_confirmed: bool = False
    receipt_stable: bool | None = None
    reorg_detected: bool = False
    journal_state: str = "unavailable"
    confirmation_complete: bool = False
    ready_for_order_reconciliation: bool = False
    ready_for_new_execution_action: bool = False
    resend_allowed: bool = False
    replacement_allowed: bool = False
    raw_receipt_output_allowed: bool = False
    raw_log_output_allowed: bool = False
    blockers: tuple[str, ...] = ()

    def safe_dict(self) -> dict[str, Any]:
        return {"confirmation_feature": self.confirmation_feature, "observation_execution_performed": self.observation_execution_performed, "rpc_calls_performed": self.rpc_calls_performed, "receipt_found": self.receipt_found, "receipt_status": self.receipt_status, "canonical_block_match": self.canonical_block_match, "confirmation_count": self.confirmation_count, "required_confirmation_count": self.required_confirmation_count, "finality_reached": self.finality_reached, "expected_event_type": self.expected_event_type, "expected_event_found": self.expected_event_found, "order_id_confirmed": self.order_id_confirmed, "receipt_stable": self.receipt_stable, "reorg_detected": self.reorg_detected, "journal_state": self.journal_state, "confirmation_complete": self.confirmation_complete, "ready_for_order_reconciliation": self.ready_for_order_reconciliation, "ready_for_new_execution_action": False, "resend_allowed": False, "replacement_allowed": False, "raw_receipt_output_allowed": False, "raw_log_output_allowed": False, "blockers": self.blockers}

    def __repr__(self) -> str:
        return f"DreamDexTransactionConfirmationPreview(feature={self.confirmation_feature!r}, rpc_calls_performed={self.rpc_calls_performed!r})"


def _empty_result(intent_id: str, submission_id: str, tx_hash: str, *, state: str, blockers: Sequence[str], errors: Sequence[str] = (), observation_count: int = 0) -> DreamDexTransactionConfirmationResult:
    receipt = DreamDexTransactionReceiptEvidence(transaction_hash=tx_hash, validation_errors=tuple(errors), receipt_fingerprint=_fp({"found": False, "hash": tx_hash}, "receipt"))
    block = DreamDexCanonicalBlockEvidence(validation_errors=tuple(errors), block_evidence_fingerprint=_fp({"found": False}, "block"))
    event = DreamDexOrderContractEventEvidence(validation_errors=tuple(errors), event_fingerprint=_fp({"found": False}, "event"))
    return DreamDexTransactionConfirmationResult(SCHEMA_VERSION, "unavailable", intent_id, submission_id, tx_hash, receipt, block, event, state, state, observation_count, False, False, True, False, False, False, False, False, False, _fp({"status": "unavailable", "intent": intent_id}, "result"), False, tuple(blockers), tuple(errors))


def _normalize_receipt(receipt: Mapping[str, Any] | None, tx_hash: str, *, expected_target: str | None = None) -> tuple[DreamDexTransactionReceiptEvidence, Mapping[str, Any] | None, tuple[str, ...]]:
    if receipt is None:
        return DreamDexTransactionReceiptEvidence(transaction_hash=tx_hash, receipt_fingerprint=_fp({"found": False, "hash": tx_hash}, "receipt")), None, ()
    errors: list[str] = []
    try:
        found_hash = _hash(receipt.get("transactionHash"), "receipt_transaction_hash")
        if found_hash != tx_hash: errors.append("transaction_receipt_hash_mismatch")
        status_raw = receipt.get("status")
        if status_raw not in {"0x0", "0x1"}: errors.append("transaction_receipt_status_invalid")
        status = "success" if status_raw == "0x1" else "reverted" if status_raw == "0x0" else "malformed"
        block_number = _safe_int(receipt.get("blockNumber"), "block_number", required=True)
        block_hash = _hash(receipt.get("blockHash"), "block_hash")
        tx_index = _safe_int(receipt.get("transactionIndex"), "transaction_index", required=True)
        target = receipt.get("to") or receipt.get("contractAddress")
        if target is not None: target = validate_evm_address(target, field="target_address")
        if expected_target and target is None: errors.append("receipt_target_unavailable")
        elif expected_target and target != expected_target.lower(): errors.append("receipt_target_mismatch")
        gas = {name: _safe_int(receipt.get(raw), name) for name, raw in (("cumulative_gas_used", "cumulativeGasUsed"), ("gas_used", "gasUsed"), ("effective_gas_price_wei", "effectiveGasPrice"))}
        logs = receipt.get("logs")
        if not isinstance(logs, list): raise ValueError("logs_not_list")
        log_meta = []
        for log in logs:
            if not isinstance(log, Mapping): raise ValueError("log_not_object")
            address = validate_evm_address(log.get("address"), field="log_address")
            topics = log.get("topics")
            if not isinstance(topics, list) or any(not isinstance(t, str) for t in topics): raise ValueError("log_topics_invalid")
            topic_values = tuple(_safe_data(t, "topic") for t in topics)
            data = _safe_data(log.get("data"), "log_data")
            removed = log.get("removed", False)
            if not isinstance(removed, bool): raise ValueError("log_removed_invalid")
            log_meta.append({"address": address, "topics": topic_values, "data_hash": sha256(data.encode()).hexdigest(), "removed": removed})
        payload = {"hash": found_hash, "status": status, "block_number": block_number, "block_hash": block_hash, "transaction_index": tx_index, "target": target, "gas": gas, "logs": log_meta}
        evidence = DreamDexTransactionReceiptEvidence(transaction_hash=found_hash, receipt_found=True, receipt_status=status, block_number=block_number, block_hash=block_hash, transaction_index=tx_index, target_address=target, cumulative_gas_used=gas["cumulative_gas_used"], gas_used=gas["gas_used"], effective_gas_price_wei=gas["effective_gas_price_wei"], log_count=len(logs), receipt_fingerprint=_fp(payload, "receipt"), source_status="source_confirmed" if not errors else "blocked", authoritative=False, validation_errors=tuple(errors))
        return evidence, receipt, tuple(errors)
    except (ValueError, TypeError, KeyError) as exc:
        return DreamDexTransactionReceiptEvidence(transaction_hash=tx_hash, receipt_found=True, receipt_status="malformed", receipt_fingerprint=_fp({"hash": tx_hash, "malformed": str(exc).split(":", 1)[0]}, "receipt"), source_status="blocked", validation_errors=("transaction_receipt_malformed",)), receipt, ("transaction_receipt_malformed",)


def _event_evidence(receipt: Mapping[str, Any] | None, receipt_ev: DreamDexTransactionReceiptEvidence, operation: str, expected_pool: str | None, expected_order_id: int | None) -> DreamDexOrderContractEventEvidence:
    event_type = {"place_order": "OrderPlaced", "cancel_order": "OrderCancelled", "reduce_order": "ReduceUnavailable"}.get(operation, "OrderPlaced")
    if operation == "reduce_order":
        return DreamDexOrderContractEventEvidence(expected_operation=operation, expected_event_type=event_type, source_status="blocked", validation_errors=("reduce_event_semantics_unavailable",), event_fingerprint=_fp({"operation": operation}, "event"))
    topic = ORDER_PLACED_TOPIC if operation == "place_order" else ORDER_CANCELLED_TOPIC
    matches = []
    wrong_pool = False
    # Reuse the source-audited decoder for the canonical topic/order-id
    # semantics; the local pass below only adds strict pool, removed-log, and
    # conflict accounting required by this confirmation boundary.
    audited = parse_order_placed_event(receipt, expected_pool=expected_pool) if operation == "place_order" else parse_order_cancelled_event(receipt)
    if receipt and isinstance(receipt.get("logs"), list):
        for log in receipt["logs"]:
            if not isinstance(log, Mapping) or log.get("removed") is True: continue
            topics = log.get("topics")
            if not isinstance(topics, list) or not topics or not isinstance(topics[0], str) or topics[0].lower() != topic.lower(): continue
            if expected_pool and str(log.get("address", "")).lower() != expected_pool.lower():
                wrong_pool = True
                continue
            if len(topics) < 2 or not isinstance(topics[1], str): continue
            try: order_id = int(topics[1], 16)
            except (TypeError, ValueError): continue
            matches.append((order_id, str(log.get("address", "")).lower()))
    if not matches and not wrong_pool and audited.status == "confirmed" and audited.order_id is not None:
        matches.append((audited.order_id, expected_pool.lower()))
    conflict = len({item[0] for item in matches}) > 1
    order_id = matches[0][0] if matches else None
    match = None if expected_order_id is None else (order_id == expected_order_id if order_id is not None else False)
    errors = []
    if wrong_pool: errors.append("event_contract_mismatch")
    if conflict: errors.append("expected_contract_event_conflict")
    if expected_order_id is not None and match is False: errors.append("order_id_mismatch")
    return DreamDexOrderContractEventEvidence(expected_operation=operation, expected_event_type=event_type, event_found=bool(matches), matching_event_count=len(matches), conflicting_event_count=1 if conflict else 0, contract_address=matches[0][1] if matches else expected_pool, transaction_hash=receipt_ev.transaction_hash, block_number=receipt_ev.block_number, block_hash=receipt_ev.block_hash, order_id=order_id, expected_order_id=expected_order_id, order_id_match=match, event_fingerprint=_fp({"topic": topic, "matches": matches, "expected": expected_order_id}, "event"), source_status="source_confirmed" if matches and not errors else "blocked", validation_errors=tuple(errors))


def observe_transaction_confirmation_once(*, journal: DreamDexExecutionJournal, submission_record: Mapping[str, Any], rpc: TransactionConfirmationRpc, policy: DreamDexTransactionConfirmationPolicy, expected_operation: str, expected_pool: str, expected_order_id: int | None = None) -> DreamDexTransactionConfirmationResult:
    """Observe one receipt/canonical block sequence; no submission call is possible."""
    if not isinstance(submission_record, Mapping) and not hasattr(submission_record, "intent_id"): raise TypeError("typed_submission_record_required")
    def record_value(name: str, default: Any = None) -> Any:
        return submission_record.get(name, default) if isinstance(submission_record, Mapping) else getattr(submission_record, name, default)
    intent_id = record_value("intent_id"); submission_id = record_value("submission_id"); tx_hash = record_value("signed_transaction_hash")
    if not all(isinstance(x, str) and x for x in (intent_id, submission_id, tx_hash)): raise ValueError("submission_record_identifiers_missing")
    tx_hash = _hash(tx_hash, "signed_transaction_hash")
    intent = journal.get_execution_intent(intent_id)
    if intent is None: return _empty_result(intent_id, submission_id, tx_hash, state="unavailable", blockers=("execution_intent_not_found",))
    if record_value("send_attempt_count") != 1 or record_value("submission_status") not in {"submitted", "submitted_recovered", "recovery_required", "submission_unknown"}:
        return _empty_result(intent_id, submission_id, tx_hash, state=intent.state, blockers=("submission_attempt_count_invalid",))
    if record_value("intent_id") != intent.intent_id or not record_value("verified_artifact_fingerprint") or record_value("local_hash_match_status") not in {"confirmed", None}:
        return _empty_result(intent_id, submission_id, tx_hash, state=intent.state, blockers=("confirmation_binding_mismatch",))
    if policy.required_chain_id is not None and intent.chain_id != policy.required_chain_id:
        return _empty_result(intent_id, submission_id, tx_hash, state=intent.state, blockers=("confirmation_chain_mismatch",))
    if policy.required_target_address is not None and intent.target_address != policy.required_target_address.lower():
        return _empty_result(intent_id, submission_id, tx_hash, state=intent.state, blockers=("confirmation_target_mismatch",))
    if expected_operation not in OPERATIONS: return _empty_result(intent_id, submission_id, tx_hash, state=intent.state, blockers=("expected_operation_invalid",))
    try:
        expected_pool = validate_evm_address(expected_pool, field="expected_pool")
    except ValueError:
        return _empty_result(intent_id, submission_id, tx_hash, state=intent.state, blockers=("expected_pool_invalid",))
    if expected_order_id is not None and (isinstance(expected_order_id, bool) or not isinstance(expected_order_id, int) or expected_order_id < 0):
        return _empty_result(intent_id, submission_id, tx_hash, state=intent.state, blockers=("expected_order_id_invalid",))
    if intent.state not in {JournalState.SUBMITTED.value, JournalState.PENDING.value, JournalState.RECOVERY_REQUIRED.value, JournalState.CONFIRMED_SUCCESS.value}: return _empty_result(intent_id, submission_id, tx_hash, state=intent.state, blockers=("confirmation_state_not_observable",))
    try: receipt_raw = rpc.get_transaction_receipt(tx_hash)
    except (DreamDexRpcError, ValueError, TypeError): return _empty_result(intent_id, submission_id, tx_hash, state=intent.state, blockers=("receipt_lookup_unavailable",))
    receipt_ev, raw_receipt, errors = _normalize_receipt(receipt_raw, tx_hash, expected_target=policy.required_target_address)
    prior = journal.get_transaction_confirmation(intent_id=intent_id)
    observation_count = int(prior.get("observation_count", 0)) + 1 if prior else 1
    if not receipt_ev.receipt_found:
        if prior and prior.get("receipt_block_hash"):
            prior_block = prior.get("receipt_block_number")
            prior_hash = prior.get("receipt_block_hash")
            block = DreamDexCanonicalBlockEvidence(receipt_block_number=prior_block, receipt_block_hash=prior_hash, canonical_block_found=False, block_hash_match=False, confirmation_count=int(prior.get("confirmation_count") or 0), required_confirmation_count=policy.minimum_confirmations, block_evidence_fingerprint=_fp({"missing_after_mined": True, "block": prior_hash}, "block"), source_status="blocked", validation_errors=("transaction_reorg_detected",))
            event = DreamDexOrderContractEventEvidence(expected_operation=expected_operation, expected_event_type={"place_order": "OrderPlaced", "cancel_order": "OrderCancelled", "reduce_order": "ReduceUnavailable"}[expected_operation], source_status="blocked", event_fingerprint=_fp({"missing_after_mined": True}, "event"), validation_errors=("transaction_reorg_detected",))
            return _persist_and_result(journal, intent, submission_id, tx_hash, receipt_ev, block, event, observation_count, policy=policy, blockers=("transaction_reorg_detected",))
        result = _empty_result(intent_id, submission_id, tx_hash, state=intent.state, blockers=("transaction_receipt_not_found",), observation_count=observation_count)
        if policy.persist_pending_observation:
            try: journal.persist_transaction_confirmation(confirmation_id=_fp({"submission": submission_id}, "confirmation_id"), submission_id=submission_id, intent_id=intent_id, transaction_hash=tx_hash, confirmation_status="pending", receipt_status=None, receipt_block_number=None, receipt_block_hash=None, canonical_block_hash=None, confirmation_count=0, required_confirmation_count=policy.minimum_confirmations, expected_event_type={"place_order": "OrderPlaced", "cancel_order": "OrderCancelled", "reduce_order": "ReduceUnavailable"}[expected_operation], expected_event_fingerprint=None, confirmed_order_id=None, observation_count=observation_count, receipt_stable=True, reorg_detected=False, confirmation_fingerprint=_fp(result.safe_dict(), "confirmation"))
            except Exception: pass
        return result
    if errors:
        return _persist_and_result(journal, intent, submission_id, tx_hash, receipt_ev, DreamDexCanonicalBlockEvidence(validation_errors=errors, block_evidence_fingerprint=_fp({"errors": errors}, "block")), _event_evidence(raw_receipt, receipt_ev, expected_operation, expected_pool, expected_order_id), observation_count, policy=policy, blockers=errors)
    try: canonical_raw = rpc.get_block_by_number(receipt_ev.block_number)  # type: ignore[arg-type]
    except (DreamDexRpcError, ValueError, TypeError):
        return _persist_and_result(journal, intent, submission_id, tx_hash, receipt_ev, DreamDexCanonicalBlockEvidence(receipt_block_number=receipt_ev.block_number, receipt_block_hash=receipt_ev.block_hash, validation_errors=("canonical_block_unavailable",), block_evidence_fingerprint=_fp({"errors": ["canonical_block_unavailable"]}, "block")), _event_evidence(raw_receipt, receipt_ev, expected_operation, expected_pool, expected_order_id), observation_count, policy=policy, blockers=("canonical_block_unavailable",))
    try:
        canonical_hash = _hash(canonical_raw.get("hash"), "canonical_block_hash") if canonical_raw else None
        latest = rpc.get_block_number()
        if latest < receipt_ev.block_number: raise ValueError("latest_block_before_receipt")
        depth = latest - receipt_ev.block_number + 1
        match = canonical_hash == receipt_ev.block_hash
        block = DreamDexCanonicalBlockEvidence(receipt_block_number=receipt_ev.block_number, receipt_block_hash=receipt_ev.block_hash, canonical_block_found=canonical_raw is not None, canonical_block_hash=canonical_hash, block_hash_match=match, latest_block_number=latest, confirmation_count=depth, required_confirmation_count=policy.minimum_confirmations, finality_reached=match and depth >= policy.minimum_confirmations, block_evidence_fingerprint=_fp({"receipt_block": receipt_ev.block_number, "receipt_hash": receipt_ev.block_hash, "canonical": canonical_hash, "latest": latest, "depth": depth}, "block"), source_status="source_confirmed" if match else "blocked", validation_errors=() if match else ("canonical_block_hash_mismatch",))
    except (ValueError, TypeError):
        block = DreamDexCanonicalBlockEvidence(receipt_block_number=receipt_ev.block_number, receipt_block_hash=receipt_ev.block_hash, validation_errors=("canonical_block_hash_mismatch",), block_evidence_fingerprint=_fp({"errors": ["canonical_block_hash_mismatch"]}, "block"))
    event = _event_evidence(raw_receipt, receipt_ev, expected_operation, expected_pool, expected_order_id)
    receipt_ev = replace(receipt_ev, expected_contract_log_count=event.matching_event_count)
    blockers = tuple(block.validation_errors) + tuple(event.validation_errors) + (("expected_contract_event_missing",) if receipt_ev.receipt_status == "success" and not event.event_found else ()) + (() if receipt_ev.receipt_status == "success" else ("transaction_receipt_reverted",))
    return _persist_and_result(journal, intent, submission_id, tx_hash, receipt_ev, block, event, observation_count, policy=policy, blockers=blockers)


def _result_from_evidence(intent: Any, submission_id: str, tx_hash: str, receipt: DreamDexTransactionReceiptEvidence, block: DreamDexCanonicalBlockEvidence, event: DreamDexOrderContractEventEvidence, observation_count: int, *, blockers: Sequence[str], force_reorg: bool = False) -> DreamDexTransactionConfirmationResult:
    reorg = bool(force_reorg or block.block_hash_match is False or (receipt.receipt_found is False and observation_count > 1))
    finality = block.finality_reached and not reorg
    expected = event.event_found and not event.validation_errors and (event.order_id_match is not False)
    complete = finality and receipt.receipt_status == "success" and expected and not reorg and not blockers
    reverted = finality and receipt.receipt_status == "reverted"
    status = "confirmed_success" if complete else "confirmed_reverted" if reverted else "reorg_detected" if reorg else "confirmed_missing_event" if finality and not expected else "pending"
    state_after = status
    if status == "pending": state_after = JournalState.PENDING.value
    elif status == "reorg_detected": state_after = JournalState.REORG_DETECTED.value
    elif status == "confirmed_missing_event": state_after = JournalState.CONFIRMED_MISSING_EVENT.value
    result = DreamDexTransactionConfirmationResult(SCHEMA_VERSION, status, intent.intent_id, submission_id, tx_hash, receipt, block, event, intent.state, state_after, observation_count, not reorg, reorg, not receipt.receipt_found, receipt.receipt_status == "reverted", expected, bool(event.order_id is not None and event.order_id_match is not False and expected), finality, complete, complete, False, _fp({"status": status, "receipt": receipt.receipt_fingerprint, "block": block.block_evidence_fingerprint, "event": event.event_fingerprint, "observation": observation_count}, "result"), False, tuple(dict.fromkeys(str(x) for x in blockers)), ())
    return result


def _persist_and_result(journal: DreamDexExecutionJournal, intent: Any, submission_id: str, tx_hash: str, receipt: DreamDexTransactionReceiptEvidence, block: DreamDexCanonicalBlockEvidence, event: DreamDexOrderContractEventEvidence, observation_count: int, *, policy: DreamDexTransactionConfirmationPolicy, blockers: Sequence[str]) -> DreamDexTransactionConfirmationResult:
    prior = journal.get_transaction_confirmation(intent_id=intent.intent_id)
    evidence_changed = bool(prior and prior.get("receipt_block_hash") and receipt.block_hash and prior.get("receipt_block_hash") != receipt.block_hash)
    event_changed = bool(prior and prior.get("expected_event_fingerprint") and prior.get("expected_event_fingerprint") != event.event_fingerprint and not event.event_found)
    forced_reorg = evidence_changed or event_changed
    effective_blockers = tuple(dict.fromkeys((*blockers, "transaction_reorg_detected" if forced_reorg else "")))
    effective_blockers = tuple(item for item in effective_blockers if item)
    result = _result_from_evidence(intent, submission_id, tx_hash, receipt, block, event, observation_count, blockers=effective_blockers, force_reorg=forced_reorg)
    state = result.journal_state_after if result.journal_state_after in {item.value for item in JournalState} else None
    persist_block_number = receipt.block_number if receipt.block_number is not None else (prior.get("receipt_block_number") if prior else None)
    persist_block_hash = receipt.block_hash if receipt.block_hash is not None else (prior.get("receipt_block_hash") if prior else None)
    try:
        persisted_status, _, persist_reasons = journal.persist_transaction_confirmation(confirmation_id=_fp({"submission": submission_id}, "confirmation_id"), submission_id=submission_id, intent_id=intent.intent_id, transaction_hash=tx_hash, confirmation_status=result.status, receipt_status=receipt.receipt_status if receipt.receipt_found else None, receipt_block_number=persist_block_number, receipt_block_hash=persist_block_hash, canonical_block_hash=block.canonical_block_hash or (prior.get("canonical_block_hash") if prior else None), confirmation_count=block.confirmation_count, required_confirmation_count=policy.minimum_confirmations, expected_event_type=event.expected_event_type, expected_event_fingerprint=event.event_fingerprint, confirmed_order_id=event.order_id if result.order_id_confirmed else None, observation_count=observation_count, receipt_stable=result.receipt_stable, reorg_detected=result.reorg_detected, confirmation_fingerprint=result.result_fingerprint, new_state=state, blockers=result.blockers)
        if persisted_status in {"blocked", "rejected"}:
            result = replace(result, status="recovery_required", journal_state_after=intent.state, confirmation_complete=False, ready_for_order_reconciliation=False, ready_for_new_execution_action=False, blockers=tuple(dict.fromkeys((*result.blockers, "confirmation_persistence_unavailable", *persist_reasons))))
        elif result.confirmation_complete:
            result = replace(result, authoritative=True, receipt_evidence=replace(result.receipt_evidence, authoritative=True), block_evidence=replace(result.block_evidence, authoritative=True), event_evidence=replace(result.event_evidence, authoritative=True))
    except Exception:
        result = replace(result, status="recovery_required", confirmation_complete=False, ready_for_order_reconciliation=False, ready_for_new_execution_action=False, blockers=tuple(dict.fromkeys((*result.blockers, "confirmation_persistence_unavailable"))))
    return result


def monitor_transaction_confirmation(*, journal: DreamDexExecutionJournal, submission_record: Mapping[str, Any], rpc: TransactionConfirmationRpc, policy: DreamDexTransactionConfirmationPolicy, expected_operation: str, expected_pool: str, expected_order_id: int | None = None, sleep_fn: Any = time.sleep, monotonic_fn: Any = time.monotonic) -> DreamDexTransactionConfirmationResult:
    start = monotonic_fn(); result = observe_transaction_confirmation_once(journal=journal, submission_record=submission_record, rpc=rpc, policy=policy, expected_operation=expected_operation, expected_pool=expected_pool, expected_order_id=expected_order_id)
    attempts = 1
    while result.status == "pending" and attempts < policy.maximum_observation_attempts and monotonic_fn() - start < policy.maximum_monitor_duration_ms / 1000:
        if policy.observation_interval_ms: sleep_fn(policy.observation_interval_ms / 1000)
        result = observe_transaction_confirmation_once(journal=journal, submission_record=submission_record, rpc=rpc, policy=policy, expected_operation=expected_operation, expected_pool=expected_pool, expected_order_id=expected_order_id); attempts += 1
    if result.status == "pending" and (attempts >= policy.maximum_observation_attempts or monotonic_fn() - start >= policy.maximum_monitor_duration_ms / 1000):
        result = replace(result, blockers=tuple(dict.fromkeys((*result.blockers, "confirmation_monitor_timeout"))))
    return result


def build_transaction_confirmation_preview() -> DreamDexTransactionConfirmationPreview:
    return DreamDexTransactionConfirmationPreview()


def normalize_transaction_receipt(receipt: Mapping[str, Any] | None, transaction_hash: str, *, expected_target: str | None = None) -> DreamDexTransactionReceiptEvidence:
    """Public structural normalizer; raw input is not retained."""
    return _normalize_receipt(receipt, _hash(transaction_hash, "transaction_hash"), expected_target=expected_target)[0]


def calculate_confirmation_depth(latest_block_number: int, receipt_block_number: int) -> int:
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in (latest_block_number, receipt_block_number)):
        raise ValueError("confirmation_block_number_invalid")
    if latest_block_number < receipt_block_number:
        raise ValueError("latest_block_before_receipt")
    return latest_block_number - receipt_block_number + 1


def validate_canonical_block(*, receipt_block_number: int, receipt_block_hash: str, canonical_block: Mapping[str, Any] | None, latest_block_number: int, required_confirmations: int) -> DreamDexCanonicalBlockEvidence:
    try:
        receipt_hash = _hash(receipt_block_hash, "receipt_block_hash")
        canonical_hash = _hash(canonical_block.get("hash"), "canonical_block_hash") if canonical_block else None
        depth = calculate_confirmation_depth(latest_block_number, receipt_block_number)
        match = canonical_hash == receipt_hash
        return DreamDexCanonicalBlockEvidence(receipt_block_number=receipt_block_number, receipt_block_hash=receipt_hash, canonical_block_found=canonical_block is not None, canonical_block_hash=canonical_hash, block_hash_match=match, latest_block_number=latest_block_number, confirmation_count=depth, required_confirmation_count=required_confirmations, finality_reached=match and depth >= required_confirmations, block_evidence_fingerprint=_fp({"receipt": receipt_hash, "canonical": canonical_hash, "depth": depth}, "block"), source_status="source_confirmed" if match else "blocked", validation_errors=() if match else ("canonical_block_hash_mismatch",))
    except (ValueError, TypeError):
        return DreamDexCanonicalBlockEvidence(receipt_block_number=receipt_block_number, receipt_block_hash=receipt_block_hash, latest_block_number=latest_block_number, required_confirmation_count=required_confirmations, block_evidence_fingerprint=_fp({"invalid": True}, "block"), source_status="blocked", validation_errors=("canonical_block_unavailable",))


def validate_order_placed_event(receipt: Mapping[str, Any] | None, *, expected_pool: str, transaction_hash: str, expected_order_id: int | None = None) -> DreamDexOrderContractEventEvidence:
    receipt_ev = normalize_transaction_receipt(receipt, transaction_hash, expected_target=None)
    return _event_evidence(receipt, receipt_ev, "place_order", expected_pool, expected_order_id)


def validate_order_cancelled_event(receipt: Mapping[str, Any] | None, *, expected_pool: str, transaction_hash: str, expected_order_id: int | None) -> DreamDexOrderContractEventEvidence:
    receipt_ev = normalize_transaction_receipt(receipt, transaction_hash, expected_target=None)
    return _event_evidence(receipt, receipt_ev, "cancel_order", expected_pool, expected_order_id)


def detect_transaction_reorg(*, previous: DreamDexTransactionConfirmationResult, current: DreamDexTransactionConfirmationResult) -> bool:
    return previous.transaction_hash == current.transaction_hash and (previous.receipt_evidence.receipt_fingerprint != current.receipt_evidence.receipt_fingerprint or previous.block_evidence.block_evidence_fingerprint != current.block_evidence.block_evidence_fingerprint or previous.event_evidence.event_fingerprint != current.event_evidence.event_fingerprint)


def serialize_transaction_confirmation_diagnostics(value: DreamDexTransactionConfirmationResult | DreamDexTransactionConfirmationPreview) -> dict[str, Any]:
    return value.safe_dict()


__all__ = ["SCHEMA_VERSION", "TransactionConfirmationRpc", "DreamDexTransactionConfirmationPolicy", "DreamDexTransactionReceiptEvidence", "DreamDexCanonicalBlockEvidence", "DreamDexOrderContractEventEvidence", "DreamDexTransactionConfirmationResult", "DreamDexTransactionConfirmationPreview", "normalize_transaction_receipt", "validate_canonical_block", "calculate_confirmation_depth", "validate_order_placed_event", "validate_order_cancelled_event", "detect_transaction_reorg", "observe_transaction_confirmation_once", "monitor_transaction_confirmation", "build_transaction_confirmation_preview", "serialize_transaction_confirmation_diagnostics"]
