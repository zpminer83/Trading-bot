"""Pure evidence bridge for the offline DreamDEX reconciliation graph.

This module deliberately accepts only materialised evidence.  It does not
construct transports, read configuration, inspect files, authenticate, poll,
sign, submit, or mutate any supplied object.  Existing parsers and indexers
remain the owners of source-specific schema and authority semantics; the
bridge only adapts their results into graph input and aggregates diagnostics.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from hashlib import sha256
import json
import re
from typing import Any, Mapping, Sequence

from bot.execution.dreamdex_order_reconciliation import (
    DreamDexOrderReconciliationGraph,
    build_order_reconciliation_graph,
    build_order_reconciliation_preview,
    serialize_order_reconciliation_diagnostics,
)
from bot.execution.dreamdex_execution_primitives import (
    ensure_no_raw_sensitive_fields,
    mask_evm_address,
    mask_hex_hash,
)


SCHEMA_VERSION = "1"
_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_HASH_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")
_KNOWN_OPERATIONS = frozenset({"place_order", "cancel_order", "reduce_order", "replace_order"})
_SUBMITTED_STATES = frozenset({
    "externally_submitted", "pending_external_confirmation", "confirmed_success",
    "confirmed_reverted", "confirmed_missing_required_event", "replaced_external",
    "dropped_external", "unknown_external_state",
})
_CONFLICT_WORDS = frozenset({"conflicting", "conflict", "mismatch", "replacement_lineage_conflict"})


def _get(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _seq(value: Any) -> tuple[Any, ...]:
    if value is None:
        return ()
    if isinstance(value, Mapping):
        for key in ("records", "orders", "fills", "items", "data"):
            if key in value and isinstance(value[key], (list, tuple)):
                return tuple(value[key])
        return (value,)
    for name in ("records", "orders", "fills", "items"):
        nested = getattr(value, name, None)
        if nested is not None and nested is not value:
            return _seq(nested)
    if isinstance(value, (str, bytes)):
        return ()
    try:
        return tuple(value)
    except TypeError:
        return (value,)


def _source(value: Any = None, explicit: Any = None) -> Any:
    return explicit if explicit is not None else _get(value, "source_status", value)


def _status(value: Any = None, explicit: Any = None) -> str:
    source = _source(value, explicit)
    status = _get(source, "status", None)
    if isinstance(source, str):
        status = source
    if status is None:
        status = _get(value, "status", None)
    if status is None and _get(source, "available", False) is True:
        status = "available"
    return str(status) if status is not None else "unavailable"


def _authority(value: Any = None, explicit: Any = None) -> bool:
    source = _source(value, explicit)
    if _get(value, "authoritative", None) is True:
        return True
    return _get(source, "authority_status", None) == "authoritative" or _get(source, "authoritative", False) is True


def _source_fields(value: Any = None, explicit: Any = None) -> dict[str, Any]:
    source = _source(value, explicit)
    return {
        "source_status": _status(value, source),
        "authoritative": _authority(value, source),
        "pagination_complete": bool(_get(source, "pagination_complete", _get(value, "pagination_complete", False))),
        "pagination_status": _get(source, "pagination_status", _get(value, "pagination_status", "unresolved")),
        "schema_status": _get(source, "schema_status", _get(value, "schema_status", "unknown")),
        "authority_status": _get(source, "authority_status", "authoritative" if _authority(value, source) else "non_authoritative"),
        "reorg_status": _get(source, "reorg_status", _get(value, "reorg_status", "unknown")),
        "duplicate_count": int(_get(source, "duplicate_count", _get(value, "duplicate_count", 0)) or 0),
        "conflict_count": int(_get(source, "conflict_count", _get(value, "conflict_count", 0)) or 0),
    }


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)


def _digest(value: Any) -> str:
    return sha256(_canonical(value).encode("utf-8")).hexdigest()


def _dedupe(items: Sequence[Mapping[str, Any]], *, key_fields: Sequence[str]) -> tuple[dict[str, Any], ...]:
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for item in items:
        key = _canonical({name: item.get(name) for name in key_fields})
        if key not in seen:
            seen.add(key)
            result.append(dict(item))
    return tuple(result)


def _auth_record(item: Any, source_status: Any = None, *, open_order: bool = False) -> dict[str, Any]:
    source = _source(item, source_status)
    result = {
        "order_id": _get(item, "order_id", _get(item, "orderId", _get(item, "id"))),
        "symbol": _get(item, "symbol", _get(item, "market")),
        "side": _get(item, "side"),
        "price": _get(item, "price"),
        "quantity": _get(item, "quantity", _get(item, "amount")),
        "remaining_quantity": _get(item, "remaining_quantity", _get(item, "remainingQuantity")),
        "raw_status_name": _get(item, "raw_status_name", _get(item, "status")),
        "account_identifier": _get(item, "account_identifier", _get(item, "owner")),
        "owner": _get(item, "owner", _get(item, "account_identifier")),
        "market_address": _get(item, "market_address", _get(item, "pool_address")),
        "subject": _get(item, "subject", _get(item, "authenticated_subject")),
        "authenticated_subject": _get(item, "authenticated_subject", _get(item, "subject")),
        "source_kind": "authenticated_open_order" if open_order else "authenticated_order",
    }
    result.update(_source_fields(item, source))
    return {key: value for key, value in result.items() if value is not None}


def adapt_authenticated_orders_for_reconciliation(records: Any = (), *, source_status: Any = None) -> tuple[Mapping[str, Any], ...]:
    """Adapt already parsed authenticated order records without HTTP data."""
    return _dedupe(tuple(_auth_record(item, source_status) for item in _seq(records)), key_fields=("order_id", "symbol", "raw_status_name", "price", "quantity"))


def adapt_authenticated_open_orders_for_reconciliation(records: Any = (), *, source_status: Any = None) -> tuple[Mapping[str, Any], ...]:
    return _dedupe(tuple(_auth_record(item, source_status, open_order=True) for item in _seq(records)), key_fields=("order_id", "symbol", "raw_status_name", "price", "quantity"))


def _metadata_record(item: Any, source_status: Any = None) -> dict[str, Any]:
    metadata = _get(item, "metadata", item)
    source = _source(item, source_status)
    result = {
        "order_id": _get(metadata, "order_id", _get(_get(metadata, "key"), "order_id")),
        "symbol": _get(metadata, "symbol", _get(_get(metadata, "key"), "symbol")),
        "owner": _get(metadata, "owner"),
        "market_address": _get(metadata, "market_address", _get(metadata, "pool_address")),
        "side": _get(metadata, "side"),
        "price": _get(metadata, "price"),
        "quantity": _get(metadata, "quantity"),
        "status": _get(metadata, "status", _get(metadata, "raw_status")),
        "transaction_hash": _get(metadata, "transaction_hash"),
        "conflicts": tuple(_get(item, "conflicts", ()) or ()),
    }
    result.update(_source_fields(item, source))
    return {key: value for key, value in result.items() if value is not None}


def adapt_order_metadata_for_reconciliation(records: Any = (), *, source_status: Any = None) -> tuple[Mapping[str, Any], ...]:
    return _dedupe(tuple(_metadata_record(item, source_status) for item in _seq(records)), key_fields=("order_id", "symbol", "owner", "side", "price", "quantity", "status", "transaction_hash"))


def _fill_record(item: Any, source_status: Any = None) -> dict[str, Any]:
    source = _source(item, source_status)
    result = {
        "fill_id": _get(item, "fill_id", _get(item, "id")),
        "order_id": _get(item, "order_id"),
        "taker_order_id": _get(item, "taker_order_id"),
        "maker_order_id": _get(item, "maker_order_id"),
        "transaction_hash": _get(item, "transaction_hash"),
        "symbol": _get(item, "symbol"),
        "pool_address": _get(item, "pool_address"),
        "owner": _get(item, "owner"),
        "quantity": _get(item, "quantity"),
        "price": _get(item, "price"),
        "notional": _get(item, "notional"),
        "block_number": _get(item, "block_number"),
        "block_hash": _get(item, "block_hash"),
        "log_index": _get(item, "log_index"),
        "removed": bool(_get(item, "removed", False)),
        "reorg_status": _get(item, "reorg_status", _get(source, "reorg_status", "unknown")),
        "duplicate": bool(_get(item, "duplicate", False)),
    }
    result.update(_source_fields(item, source))
    return {key: value for key, value in result.items() if value is not None}


def adapt_onchain_fills_for_reconciliation(records: Any = (), *, source_status: Any = None) -> tuple[Mapping[str, Any], ...]:
    """Adapt normalized fills; raw logs/topics are intentionally ignored."""
    page_source = source_status
    if page_source is None and _get(records, "source_status") is not None and not isinstance(records, (list, tuple)):
        page_source = _get(records, "source_status")
    items = _seq(_get(records, "fills", records))
    return _dedupe(tuple(_fill_record(item, page_source) for item in items), key_fields=("fill_id", "order_id", "transaction_hash", "block_number", "log_index", "quantity", "price"))


def _event_map(event: Any) -> dict[str, Any]:
    result = {name: _get(event, name) for name in ("event_name", "event_signature", "topic0", "transaction_hash", "block_number", "log_index", "contract_address", "order_id", "owner_address", "source_status")}
    return {key: value for key, value in result.items() if value is not None}


def adapt_lifecycle_records_for_reconciliation(records: Any = (), *, order_identity_evidence: Any = ()) -> tuple[Mapping[str, Any], ...]:
    identity_by_order = {
        str(_get(item, "order_id")): item for item in _seq(order_identity_evidence)
        if _get(item, "order_id") is not None and _authority(item)
    }
    adapted: list[Mapping[str, Any]] = []
    seen: set[str] = set()
    for item in _seq(records):
        evidence = _get(item, "evidence")
        events = tuple(_event_map(event) for event in _seq(_get(item, "event_evidence", ())))
        result: dict[str, Any] = {
            "schema_version": _get(item, "schema_version", SCHEMA_VERSION),
            "lifecycle_id": _get(item, "lifecycle_id"),
            "operation": _get(item, "operation"),
            "request_fingerprint": _get(item, "request_fingerprint"),
            "envelope_fingerprint": _get(item, "envelope_fingerprint"),
            "transaction_hash": _get(item, "transaction_hash"),
            "current_state": _get(item, "current_state"),
            "previous_state": _get(item, "previous_state"),
            "order_id": _get(item, "order_id"),
            "replacement_transaction_hash": _get(item, "replacement_transaction_hash"),
            "lifecycle_fingerprint": _get(item, "lifecycle_fingerprint"),
            "blockers": tuple(_get(item, "blockers", ()) or ()),
            "conflicts": tuple(_get(item, "conflicts", ()) or ()),
            "event_evidence": events,
            "receipt_evidence": _get(item, "receipt_evidence"),
            "replacement_evidence": _get(item, "replacement_evidence"),
            "authoritative": bool(_get(item, "authoritative", False)),
            "source_status": _status(evidence, evidence),
            "evidence": {
                "source_type": _get(evidence, "source_type", "unavailable"),
                "source_status": _status(evidence, evidence),
                "replacement_status": _get(evidence, "replacement_status", "unavailable"),
                "conflicts": tuple(_get(evidence, "conflicts", ()) or ()),
                "unresolved_reasons": tuple(_get(evidence, "unresolved_reasons", ()) or ()),
            },
        }
        order_key = str(result.get("order_id")) if result.get("order_id") is not None else None
        if order_key in identity_by_order:
            result["explicit_order_identity_authoritative"] = True
        compact = {key: value for key, value in result.items() if value is not None}
        identity = _canonical({name: compact.get(name) for name in ("lifecycle_fingerprint", "operation", "transaction_hash", "order_id", "current_state", "event_evidence")})
        if identity not in seen:
            seen.add(identity)
            adapted.append(compact)
    return tuple(sorted(adapted, key=lambda item: (str(item.get("order_id", "")), str(item.get("transaction_hash", "")), str(item.get("lifecycle_fingerprint", "")))))


def _status_from_collection(value: Any, records: Sequence[Any] = ()) -> str:
    status = _status(value)
    if status != "unavailable":
        return status
    return "available" if records else "unavailable"


def _conflict_count(records: Sequence[Any], identity_fields: Sequence[str]) -> int:
    by_key: dict[str, set[str]] = {}
    for item in records:
        key = tuple(str(_get(item, name, "")) for name in identity_fields)
        by_key.setdefault(repr(key), set()).add(_canonical({name: _get(item, name) for name in identity_fields + ("status", "raw_status_name", "price", "quantity", "transaction_hash")}))
    return sum(1 for values in by_key.values() if len(values) > 1)


@dataclass(frozen=True, repr=False)
class DreamDexReconciliationEvidenceInventory:
    authenticated_account_status: str = "unavailable"
    authenticated_order_status: str = "unavailable"
    authenticated_open_order_status: str = "unavailable"
    authenticated_pagination_status: str = "unavailable"
    order_metadata_status: str = "unavailable"
    order_metadata_record_count: int = 0
    order_metadata_conflict_count: int = 0
    onchain_fill_status: str = "unavailable"
    onchain_fill_record_count: int = 0
    onchain_fill_duplicate_count: int = 0
    onchain_fill_pagination_status: str = "unavailable"
    onchain_fill_reorg_status: str = "unavailable"
    lifecycle_status: str = "unavailable"
    lifecycle_record_count: int = 0
    account_identity_status: str = "unavailable"
    market_identity_status: str = "unavailable"
    source_authority_status: str = "non_authoritative"
    conflicts: tuple[str, ...] = ()
    unresolved_reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "conflicts", tuple(dict.fromkeys(str(x) for x in self.conflicts)))
        object.__setattr__(self, "unresolved_reasons", tuple(dict.fromkeys(str(x) for x in self.unresolved_reasons)))
        for name in ("order_metadata_record_count", "order_metadata_conflict_count", "onchain_fill_record_count", "onchain_fill_duplicate_count", "lifecycle_record_count"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name}: invalid_count")

    def safe_dict(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}

    def __repr__(self) -> str:
        return f"DreamDexReconciliationEvidenceInventory(lifecycle={self.lifecycle_record_count}, metadata={self.order_metadata_record_count}, fills={self.onchain_fill_record_count}, authoritative={self.source_authority_status!r})"


@dataclass(frozen=True, repr=False)
class DreamDexReconciliationEvidenceBundle:
    schema_version: str = SCHEMA_VERSION
    lifecycle_records: tuple[Any, ...] = ()
    order_metadata_records: tuple[Any, ...] = ()
    authenticated_orders: tuple[Any, ...] = ()
    authenticated_open_orders: tuple[Any, ...] = ()
    onchain_fills: tuple[Any, ...] = ()
    expected_account_address: str | None = None
    expected_market_address: str | None = None
    authenticated_subject: str | None = None
    authenticated_address_semantics: str = "unresolved"
    authenticated_pagination_complete: bool = False
    fill_pagination_complete: bool = False
    fill_reorg_status: str = "unavailable"
    inventory: DreamDexReconciliationEvidenceInventory = field(default_factory=DreamDexReconciliationEvidenceInventory)
    bundle_fingerprint: str | None = None
    authoritative: bool = False
    conflicts: tuple[str, ...] = ()
    unresolved_reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "lifecycle_records", tuple(self.lifecycle_records))
        object.__setattr__(self, "order_metadata_records", tuple(self.order_metadata_records))
        object.__setattr__(self, "authenticated_orders", tuple(self.authenticated_orders))
        object.__setattr__(self, "authenticated_open_orders", tuple(self.authenticated_open_orders))
        object.__setattr__(self, "onchain_fills", tuple(self.onchain_fills))
        object.__setattr__(self, "conflicts", tuple(dict.fromkeys(str(x) for x in self.conflicts)))
        object.__setattr__(self, "unresolved_reasons", tuple(dict.fromkeys(str(x) for x in self.unresolved_reasons)))
        if self.expected_account_address is not None and not _ADDRESS_RE.fullmatch(self.expected_account_address):
            raise ValueError("expected_account_address: invalid_address")
        if self.expected_market_address is not None and not _ADDRESS_RE.fullmatch(self.expected_market_address):
            raise ValueError("expected_market_address: invalid_address")

    def safe_dict(self) -> dict[str, Any]:
        return ensure_no_raw_sensitive_fields({
            "schema_version": self.schema_version,
            "lifecycle_record_count": len(self.lifecycle_records),
            "order_metadata_record_count": len(self.order_metadata_records),
            "authenticated_order_count": len(self.authenticated_orders),
            "authenticated_open_order_count": len(self.authenticated_open_orders),
            "onchain_fill_count": len(self.onchain_fills),
            "expected_account_address": mask_evm_address(self.expected_account_address),
            "expected_market_address": mask_evm_address(self.expected_market_address),
            "authenticated_subject": mask_evm_address(self.authenticated_subject),
            "authenticated_address_semantics": self.authenticated_address_semantics,
            "authenticated_pagination_complete": self.authenticated_pagination_complete,
            "fill_pagination_complete": self.fill_pagination_complete,
            "fill_reorg_status": self.fill_reorg_status,
            "inventory": self.inventory.safe_dict(),
            "bundle_fingerprint": self.bundle_fingerprint,
            "authoritative": self.authoritative,
            "conflicts": self.conflicts,
            "unresolved_reasons": self.unresolved_reasons,
        })

    def __repr__(self) -> str:
        return f"DreamDexReconciliationEvidenceBundle(lifecycle={len(self.lifecycle_records)}, fills={len(self.onchain_fills)}, authoritative=False)"


@dataclass(frozen=True, repr=False)
class DreamDexReconciliationBridgeResult:
    schema_version: str = SCHEMA_VERSION
    bridge_status: str = "unavailable"
    evidence_bundle: DreamDexReconciliationEvidenceBundle = field(default_factory=DreamDexReconciliationEvidenceBundle)
    root_lifecycle_count: int = 0
    eligible_root_count: int = 0
    graph_count: int = 0
    graphs: tuple[DreamDexOrderReconciliationGraph, ...] = ()
    unrelated_metadata_count: int = 0
    unrelated_authenticated_order_count: int = 0
    unrelated_open_order_count: int = 0
    unrelated_fill_count: int = 0
    conflicting_root_count: int = 0
    bridge_fingerprint: str | None = None
    authoritative: bool = False
    reconciliation_complete: bool = False
    blockers: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()
    unresolved_reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "graphs", tuple(self.graphs))
        object.__setattr__(self, "blockers", tuple(dict.fromkeys(str(x) for x in self.blockers)))
        object.__setattr__(self, "conflicts", tuple(dict.fromkeys(str(x) for x in self.conflicts)))
        object.__setattr__(self, "unresolved_reasons", tuple(dict.fromkeys(str(x) for x in self.unresolved_reasons)))

    def safe_dict(self) -> dict[str, Any]:
        return ensure_no_raw_sensitive_fields({
            "schema_version": self.schema_version,
            "bridge_status": self.bridge_status,
            "root_lifecycle_count": self.root_lifecycle_count,
            "eligible_root_count": self.eligible_root_count,
            "graph_count": self.graph_count,
            "unrelated_metadata_count": self.unrelated_metadata_count,
            "unrelated_authenticated_order_count": self.unrelated_authenticated_order_count,
            "unrelated_open_order_count": self.unrelated_open_order_count,
            "unrelated_fill_count": self.unrelated_fill_count,
            "conflicting_root_count": self.conflicting_root_count,
            "bridge_fingerprint": self.bridge_fingerprint,
            "authoritative": self.authoritative,
            "reconciliation_complete": self.reconciliation_complete,
            "blockers": self.blockers,
            "conflicts": self.conflicts,
            "unresolved_reasons": self.unresolved_reasons,
            "graphs": tuple({
                "graph_status": graph.graph_status,
                "graph_fingerprint": graph.graph_fingerprint,
                "authoritative": graph.authoritative,
                "reconciliation_complete": graph.reconciliation_complete,
                "blockers": graph.blockers,
                "conflicts": graph.conflicts,
            } for graph in self.graphs),
        })

    def __repr__(self) -> str:
        return f"DreamDexReconciliationBridgeResult(status={self.bridge_status!r}, roots={self.eligible_root_count}, graphs={self.graph_count}, authoritative=False)"


@dataclass(frozen=True, repr=False)
class DreamDexReconciliationBridgePreview:
    bridge_status: str
    lifecycle_evidence_status: str
    authenticated_order_evidence_status: str
    authenticated_open_order_evidence_status: str
    metadata_evidence_status: str
    fill_evidence_status: str
    authenticated_pagination_complete: bool
    fill_pagination_complete: bool
    fill_reorg_status: str
    root_lifecycle_count: int
    eligible_root_count: int
    graph_count: int
    authoritative_graph_count: int
    complete_graph_count: int
    conflicting_graph_count: int
    unrelated_evidence_count: int
    bundle_fingerprint: str | None
    bridge_fingerprint: str | None
    authoritative: bool
    reconciliation_complete: bool
    blockers: tuple[str, ...] = ()

    def safe_dict(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}

    def __repr__(self) -> str:
        return f"DreamDexReconciliationBridgePreview(status={self.bridge_status!r}, graphs={self.graph_count}, authoritative=False)"


@dataclass(frozen=True)
class DreamDexEvidenceProducerAudit:
    producer_module: str
    producer_type: str
    relevant_fields: tuple[str, ...]
    source_authority: str
    completeness_semantics: str
    sensitive_fields: tuple[str, ...]
    target_reconciliation_type: str
    adapter_required: bool
    conflicts_or_missing_semantics: tuple[str, ...]


AUDITED_EVIDENCE_PRODUCERS = (
    DreamDexEvidenceProducerAudit(
        "bot.integrations.dreamdex_auth_models", "AuthenticatedAccountSnapshot / AuthenticatedOrderSnapshot",
        ("balances_status", "open_orders_status", "fills_status", "pagination_complete", "account_identifier", "order_id", "symbol", "raw_status_name"),
        "authenticated source status; authoritative only after explicit complete checks",
        "incomplete pagination is not closed-state evidence",
        ("bearer token", "headers", "cookies", "raw HTTP body"), "authenticated_order", True,
        ("subject mismatch is conflict", "observed_non_authoritative never becomes authoritative"),
    ),
    DreamDexEvidenceProducerAudit(
        "bot.integrations.dreamdex_fill_events", "FillEventPage / NormalizedOrderFill / FillEventSourceStatus",
        ("fill_id", "order_id", "transaction_hash", "symbol", "pool_address", "quantity", "price", "duplicate_count", "pagination_complete", "reorg_status"),
        "on-chain normalized records; authoritative only with matched account, complete pagination and resolved reorg",
        "duplicate and removed/reorg records are not confirmed totals",
        ("raw logs", "raw topics", "raw data"), "onchain_fill", True,
        ("wrong order is unrelated", "wrong market/account is conflict", "bridge does not calculate PnL"),
    ),
    DreamDexEvidenceProducerAudit(
        "bot.integrations.dreamdex_order_metadata", "NormalizedOrderMetadata / OrderMetadataLookupResult",
        ("order_id", "symbol", "owner", "side", "price", "quantity", "status", "transaction_hash", "conflict_count"),
        "resolver/source status; non-authoritative by default",
        "metadata cannot create a root and unresolved fields remain unresolved",
        ("raw payload", "tokens", "headers"), "order_metadata", True,
        ("identical duplicates deduplicate", "conflicting identity records block authority"),
    ),
    DreamDexEvidenceProducerAudit(
        "bot.execution.dreamdex_transaction_lifecycle", "DreamDexTransactionLifecycleRecord",
        ("operation", "request_fingerprint", "envelope_fingerprint", "transaction_hash", "current_state", "order_id", "event_evidence", "replacement_evidence"),
        "explicit external lifecycle evidence; never authoritative locally",
        "root requires known operation, fingerprints, required hash and confirmed identity evidence",
        ("raw receipt", "raw event data", "raw topics", "full addresses/hashes"), "transaction_lifecycle", False,
        ("conflicting state and unresolved replacement lineage reject root"),
    ),
    DreamDexEvidenceProducerAudit(
        "bot.execution.dreamdex_order_reconciliation", "build_order_reconciliation_graph",
        ("nodes", "edges", "blockers", "conflicts", "graph_fingerprint"),
        "structural graph evidence only; non-authoritative in production default",
        "graph construction requires an eligible lifecycle root",
        ("raw identifiers in safe diagnostics", "full addresses/hashes"), "reconciliation_graph", False,
        ("existing graph fingerprints and APIs are preserved"),
    ),
)


def audit_evidence_producers() -> tuple[DreamDexEvidenceProducerAudit, ...]:
    return AUDITED_EVIDENCE_PRODUCERS


def build_reconciliation_evidence_inventory(
    *,
    authenticated_account: Any = None,
    authenticated_orders: Any = (),
    authenticated_open_orders: Any = (),
    order_metadata_records: Any = (),
    order_metadata_source_status: Any = None,
    onchain_fills: Any = (),
    onchain_fill_source_status: Any = None,
    authenticated_account_status: Any = None,
    authenticated_order_source_status: Any = None,
    authenticated_open_order_source_status: Any = None,
    lifecycle_source_status: Any = None,
    account_identity_status: Any = None,
    market_identity_status: Any = None,
    lifecycle_records: Any = (),
    account_identity: Any = None,
    market_identity: Any = None,
    source_authority_status: str | None = None,
    authenticated_pagination_complete: bool | None = None,
    onchain_fill_pagination_complete: bool | None = None,
    onchain_fill_reorg_status: str | None = None,
    conflicts: Sequence[str] = (),
    unresolved_reasons: Sequence[str] = (),
) -> DreamDexReconciliationEvidenceInventory:
    auth_orders = adapt_authenticated_orders_for_reconciliation(authenticated_orders)
    auth_open = adapt_authenticated_open_orders_for_reconciliation(authenticated_open_orders)
    metadata = adapt_order_metadata_for_reconciliation(order_metadata_records, source_status=order_metadata_source_status)
    fills = adapt_onchain_fills_for_reconciliation(onchain_fills, source_status=onchain_fill_source_status)
    raw_fill_items = _seq(_get(onchain_fills, "fills", onchain_fills))
    lifecycle = adapt_lifecycle_records_for_reconciliation(lifecycle_records)
    auth_status = _status(authenticated_account, authenticated_account_status)
    auth_order_status = _status(authenticated_orders, authenticated_order_source_status)
    auth_open_status = _status(authenticated_open_orders, authenticated_open_order_source_status)
    if auth_order_status == "unavailable" and auth_orders:
        auth_order_status = "available"
    if auth_open_status == "unavailable" and auth_open:
        auth_open_status = "available"
    auth_pagination = authenticated_pagination_complete
    if auth_pagination is None:
        auth_pagination = _get(_source(authenticated_open_orders, authenticated_open_order_source_status), "pagination_complete", None)
    if auth_pagination is None:
        auth_pagination = _get(_source(authenticated_account), "pagination_complete", False)
    fill_source = _source(onchain_fills, onchain_fill_source_status)
    fill_status = _status(onchain_fills, fill_source)
    if fill_status == "unavailable" and fills:
        fill_status = "available"
    fill_pagination = bool(onchain_fill_pagination_complete if onchain_fill_pagination_complete is not None else _get(fill_source, "pagination_complete", False))
    fill_reorg = str(onchain_fill_reorg_status or _get(fill_source, "reorg_status", "unknown"))
    metadata_status = _status(order_metadata_records, order_metadata_source_status)
    if metadata_status == "unavailable" and metadata:
        metadata_status = "available"
    lifecycle_status = _status(lifecycle[0] if lifecycle else None, lifecycle_source_status)
    if lifecycle_status == "unavailable" and lifecycle:
        lifecycle_status = "available"
    identity_status = _status(account_identity, account_identity_status)
    market_status = _status(market_identity, market_identity_status)
    if identity_status == "unavailable" and account_identity is not None:
        identity_status = "available"
    if market_status == "unavailable" and market_identity is not None:
        market_status = "available"
    conflicts_all = list(str(item) for item in conflicts)
    conflicts_all.extend(str(item) for item in (_get(authenticated_account, "conflicts", ()) or ()))
    conflicts_all.extend(str(item) for item in (_get(order_metadata_source_status, "conflicts", ()) or ()))
    conflicts_all.extend(str(item) for item in (_get(fill_source, "conflicts", ()) or ()))
    metadata_conflicts = _conflict_count(metadata, ("order_id",))
    fill_duplicates = int(_get(fill_source, "duplicate_count", 0) or 0)
    fill_duplicates += max(0, len(raw_fill_items) - len(fills))
    fill_duplicates += sum(1 for item in fills if item.get("duplicate"))
    unresolved = list(str(item) for item in unresolved_reasons)
    if not bool(auth_pagination):
        unresolved.append("authenticated_pagination_incomplete")
    if not fill_pagination:
        unresolved.append("fill_coverage_incomplete")
    if fill_reorg not in {"ok", "resolved", "confirmed", "not_detected"}:
        unresolved.append("reorg_status_unresolved")
    if metadata_conflicts:
        conflicts_all.append("order_metadata_conflict")
    if fill_duplicates and _get(fill_source, "status") == "conflicting":
        conflicts_all.append("duplicate_fill_conflict")
    fill_id_shapes: dict[str, set[str]] = {}
    for item in fills:
        if item.get("fill_id") is not None:
            fill_id_shapes.setdefault(str(item["fill_id"]), set()).add(_canonical({name: item.get(name) for name in ("order_id", "transaction_hash", "quantity", "price", "block_number", "log_index")}))
    if any(len(values) > 1 for values in fill_id_shapes.values()):
        conflicts_all.append("duplicate_fill_conflict")
    if source_authority_status is None:
        all_authoritative = bool(
            _authority(authenticated_account) and _authority(order_metadata_source_status) and
            _authority(fill_source) and _authority(account_identity) and _authority(market_identity)
        )
        source_authority_status = "authoritative" if all_authoritative else "non_authoritative"
    return DreamDexReconciliationEvidenceInventory(
        authenticated_account_status=auth_status,
        authenticated_order_status=auth_order_status,
        authenticated_open_order_status=auth_open_status,
        authenticated_pagination_status="complete" if auth_pagination else "incomplete",
        order_metadata_status=metadata_status,
        order_metadata_record_count=len(metadata),
        order_metadata_conflict_count=metadata_conflicts + int(_get(order_metadata_source_status, "conflict_count", 0) or 0),
        onchain_fill_status=fill_status,
        onchain_fill_record_count=len(fills),
        onchain_fill_duplicate_count=fill_duplicates,
        onchain_fill_pagination_status="complete" if fill_pagination else "incomplete",
        onchain_fill_reorg_status=fill_reorg,
        lifecycle_status=lifecycle_status,
        lifecycle_record_count=len(lifecycle),
        account_identity_status=identity_status,
        market_identity_status=market_status,
        source_authority_status=source_authority_status,
        conflicts=tuple(dict.fromkeys(conflicts_all)),
        unresolved_reasons=tuple(dict.fromkeys(unresolved)),
    )


def _bundle_payload(bundle_values: Mapping[str, Any]) -> dict[str, Any]:
    def identity(records: Sequence[Any], fields: Sequence[str]) -> list[dict[str, Any]]:
        return sorted(({name: _get(record, name) for name in fields} for record in records), key=_canonical)
    return {
        "schema_version": SCHEMA_VERSION,
        "lifecycle": sorted(str(_get(item, "lifecycle_fingerprint", "")) for item in bundle_values["lifecycle_records"]),
        "metadata": identity(bundle_values["order_metadata_records"], ("order_id", "symbol", "owner", "status", "transaction_hash")),
        "authenticated_orders": identity(bundle_values["authenticated_orders"], ("order_id", "symbol", "account_identifier", "raw_status_name")),
        "authenticated_open_orders": identity(bundle_values["authenticated_open_orders"], ("order_id", "symbol", "account_identifier", "raw_status_name")),
        "fills": identity(bundle_values["onchain_fills"], ("fill_id", "order_id", "transaction_hash", "symbol", "quantity", "price", "block_number", "log_index")),
        "expected_account_address": bundle_values["expected_account_address"],
        "expected_market_address": bundle_values["expected_market_address"],
        "pagination": (bundle_values["authenticated_pagination_complete"], bundle_values["fill_pagination_complete"]),
        "fill_reorg_status": bundle_values["fill_reorg_status"],
        "conflicts": bundle_values["conflicts"],
        "unresolved_reasons": bundle_values["unresolved_reasons"],
    }


def build_reconciliation_evidence_bundle(
    *,
    lifecycle_records: Any = (),
    order_metadata_records: Any = (),
    authenticated_orders: Any = (),
    authenticated_open_orders: Any = (),
    onchain_fills: Any = (),
    expected_account_address: str | None = None,
    expected_market_address: str | None = None,
    authenticated_subject: str | None = None,
    authenticated_address_semantics: str = "unresolved",
    authenticated_account: Any = None,
    authenticated_pagination_complete: bool | None = None,
    fill_pagination_complete: bool | None = None,
    fill_reorg_status: str | None = None,
    order_identity_evidence: Any = (),
    order_metadata_source_status: Any = None,
    onchain_fill_source_status: Any = None,
    authenticated_account_status: Any = None,
    authenticated_order_source_status: Any = None,
    authenticated_open_order_source_status: Any = None,
    lifecycle_source_status: Any = None,
    account_identity_status: Any = None,
    market_identity_status: Any = None,
    source_authority_status: str | None = None,
    account_identity_evidence: Any = None,
    market_identity_evidence: Any = None,
    conflicts: Sequence[str] = (),
    unresolved_reasons: Sequence[str] = (),
) -> DreamDexReconciliationEvidenceBundle:
    lifecycle_adapted = adapt_lifecycle_records_for_reconciliation(lifecycle_records, order_identity_evidence=order_identity_evidence)
    metadata_adapted = adapt_order_metadata_for_reconciliation(order_metadata_records, source_status=order_metadata_source_status)
    auth_adapted = adapt_authenticated_orders_for_reconciliation(authenticated_orders)
    open_adapted = adapt_authenticated_open_orders_for_reconciliation(authenticated_open_orders)
    fill_adapted = adapt_onchain_fills_for_reconciliation(onchain_fills, source_status=onchain_fill_source_status)
    inventory = build_reconciliation_evidence_inventory(
        authenticated_account=authenticated_account,
        authenticated_orders=authenticated_orders,
        authenticated_open_orders=authenticated_open_orders,
        order_metadata_records=order_metadata_records,
        order_metadata_source_status=order_metadata_source_status,
        onchain_fills=onchain_fills,
        onchain_fill_source_status=onchain_fill_source_status,
        lifecycle_records=lifecycle_records,
        account_identity=account_identity_evidence or {"status": "available" if expected_account_address else "unavailable", "authoritative": False},
        market_identity=market_identity_evidence or {"status": "available" if expected_market_address else "unavailable", "authoritative": False},
        source_authority_status=source_authority_status,
        authenticated_account_status=authenticated_account_status,
        authenticated_order_source_status=authenticated_order_source_status,
        authenticated_open_order_source_status=authenticated_open_order_source_status,
        authenticated_pagination_complete=authenticated_pagination_complete,
        onchain_fill_pagination_complete=fill_pagination_complete,
        onchain_fill_reorg_status=fill_reorg_status,
        lifecycle_source_status=lifecycle_source_status,
        account_identity_status=account_identity_status,
        market_identity_status=market_identity_status,
        conflicts=conflicts,
        unresolved_reasons=unresolved_reasons,
    )
    auth_complete = bool(_get(_source(authenticated_open_orders), "pagination_complete", False) if authenticated_pagination_complete is None else authenticated_pagination_complete)
    fill_complete = bool(_get(_source(onchain_fills, onchain_fill_source_status), "pagination_complete", False) if fill_pagination_complete is None else fill_pagination_complete)
    fill_reorg = str(fill_reorg_status or _get(_source(onchain_fills, onchain_fill_source_status), "reorg_status", "unknown"))
    all_conflicts = tuple(dict.fromkeys((*inventory.conflicts, *(str(x) for x in conflicts))))
    subject_conflicts = [
        "authenticated_subject_mismatch"
        for item in (*auth_adapted, *open_adapted)
        if authenticated_subject and item.get("subject") and str(item.get("subject")).lower() != str(authenticated_subject).lower()
    ]
    all_conflicts = tuple(dict.fromkeys((*all_conflicts, *subject_conflicts)))
    all_unresolved = tuple(dict.fromkeys((*inventory.unresolved_reasons, *(str(x) for x in unresolved_reasons))))
    values = {
        "lifecycle_records": lifecycle_adapted, "order_metadata_records": metadata_adapted,
        "authenticated_orders": auth_adapted, "authenticated_open_orders": open_adapted,
        "onchain_fills": fill_adapted, "expected_account_address": expected_account_address.lower() if expected_account_address else None,
        "expected_market_address": expected_market_address.lower() if expected_market_address else None,
        "authenticated_pagination_complete": auth_complete, "fill_pagination_complete": fill_complete,
        "fill_reorg_status": fill_reorg, "conflicts": all_conflicts, "unresolved_reasons": all_unresolved,
    }
    fingerprint = _digest(_bundle_payload(values))
    authoritative = bool(
        inventory.source_authority_status == "authoritative" and auth_complete and fill_complete and
        fill_reorg in {"ok", "resolved", "confirmed", "not_detected"} and not all_conflicts and not all_unresolved and
        authenticated_address_semantics == "resolved"
    )
    return DreamDexReconciliationEvidenceBundle(
        lifecycle_records=lifecycle_adapted, order_metadata_records=metadata_adapted,
        authenticated_orders=auth_adapted, authenticated_open_orders=open_adapted, onchain_fills=fill_adapted,
        expected_account_address=values["expected_account_address"], expected_market_address=values["expected_market_address"],
        authenticated_subject=authenticated_subject, authenticated_address_semantics=authenticated_address_semantics,
        authenticated_pagination_complete=auth_complete, fill_pagination_complete=fill_complete,
        fill_reorg_status=fill_reorg, inventory=inventory, bundle_fingerprint=fingerprint,
        authoritative=authoritative, conflicts=all_conflicts, unresolved_reasons=all_unresolved,
    )


def _event_confirms_order(record: Mapping[str, Any]) -> bool:
    operation = str(record.get("operation", ""))
    if operation not in {"place_order", "cancel_order"}:
        return False
    expected_event = "OrderPlaced" if operation == "place_order" else "OrderCancelled"
    for event in _seq(record.get("event_evidence", ())):
        if str(_get(event, "event_name", "")) != expected_event or _get(event, "order_id") is None:
            continue
        event_hash = _get(event, "transaction_hash")
        if event_hash and record.get("transaction_hash") and str(event_hash).lower() != str(record["transaction_hash"]).lower():
            continue
        status = str(_get(event, "source_status", "unavailable"))
        if status in {"source_confirmed", "confirmed"}:
            return True
    return False


def _eligible_root(record: Mapping[str, Any]) -> bool:
    operation = str(record.get("operation", ""))
    if operation not in _KNOWN_OPERATIONS:
        return False
    if not record.get("request_fingerprint") or not record.get("envelope_fingerprint"):
        return False
    state = str(record.get("current_state", ""))
    if state in _SUBMITTED_STATES and not record.get("transaction_hash"):
        return False
    blockers = {str(item).lower() for item in (*record.get("blockers", ()), *record.get("conflicts", ()), *record.get("evidence", {}).get("conflicts", ())) }
    if blockers & _CONFLICT_WORDS or any("conflict" in item or "mismatch" in item for item in blockers):
        return False
    replacement = record.get("replacement_evidence")
    if record.get("replacement_transaction_hash") and replacement is None:
        return False
    if replacement is not None:
        original_hash = _get(replacement, "original_transaction_hash")
        replacement_hash = _get(replacement, "replacement_transaction_hash") or record.get("replacement_transaction_hash")
        if original_hash and replacement_hash and str(original_hash).lower() == str(replacement_hash).lower():
            return False
        if _get(replacement, "conflicts", ()) or str(_get(replacement, "source_status", "")) in {"unavailable", "unknown", "conflicting"}:
            return False
    if operation in {"place_order", "cancel_order"}:
        return bool(_event_confirms_order(record) or record.get("explicit_order_identity_authoritative"))
    return bool(record.get("explicit_order_identity_authoritative"))


def _root_evidence_conflicts(bundle: DreamDexReconciliationEvidenceBundle, lifecycle: Mapping[str, Any]) -> tuple[str, ...]:
    order_id = lifecycle.get("order_id")
    related = [
        item for records in (bundle.order_metadata_records, bundle.authenticated_orders, bundle.authenticated_open_orders, bundle.onchain_fills)
        for item in records if order_id is not None and str(item.get("order_id")) == str(order_id)
    ]
    conflicts: list[str] = []
    for item in related:
        market = item.get("market_address") or item.get("pool_address")
        if market and bundle.expected_market_address and str(market).lower() != bundle.expected_market_address.lower():
            conflicts.append("market_identity_conflict")
        owner = item.get("owner") or item.get("owner_address") or item.get("account_identifier")
        if owner and bundle.expected_account_address and str(owner).lower() != bundle.expected_account_address.lower():
            conflicts.append("account_identity_conflict")
    fills_by_id: dict[str, str] = {}
    for item in bundle.onchain_fills:
        fill_id = item.get("fill_id")
        if fill_id is None or order_id is None or str(item.get("order_id")) != str(order_id):
            continue
        identity = _canonical({key: item.get(key) for key in ("order_id", "transaction_hash", "quantity", "price", "block_number", "log_index")})
        previous = fills_by_id.setdefault(str(fill_id), identity)
        if previous != identity:
            conflicts.append("duplicate_fill_conflict")
    return tuple(dict.fromkeys(conflicts))


def build_reconciliation_graphs_from_bundle(bundle: DreamDexReconciliationEvidenceBundle) -> tuple[DreamDexOrderReconciliationGraph, ...]:
    """Build one graph per eligible lifecycle root, in deterministic order."""
    graphs: list[DreamDexOrderReconciliationGraph] = []
    root_hashes: dict[str, set[str]] = {}
    root_lineage: dict[str, bool] = {}
    for lifecycle in bundle.lifecycle_records:
        if _eligible_root(lifecycle) and lifecycle.get("order_id") is not None:
            root_hashes.setdefault(str(lifecycle["order_id"]), set()).add(str(lifecycle.get("transaction_hash", "")))
            root_lineage[str(lifecycle["order_id"])] = root_lineage.get(str(lifecycle["order_id"]), True) and lifecycle.get("replacement_evidence") is not None
    for lifecycle in bundle.lifecycle_records:
        if not _eligible_root(lifecycle):
            continue
        order_id = lifecycle.get("order_id")
        tx_hash = lifecycle.get("transaction_hash")
        related_metadata = tuple(item for item in bundle.order_metadata_records if order_id is not None and str(item.get("order_id")) == str(order_id))
        related_auth = tuple(item for item in bundle.authenticated_orders if order_id is not None and str(item.get("order_id")) == str(order_id))
        related_open = tuple(item for item in bundle.authenticated_open_orders if order_id is not None and str(item.get("order_id")) == str(order_id))
        related_fills = tuple(item for item in bundle.onchain_fills if order_id is not None and str(item.get("order_id")) == str(order_id))
        graph = build_order_reconciliation_graph(
            lifecycle_record=lifecycle,
            order_metadata_records=related_metadata,
            authenticated_orders=related_auth,
            authenticated_open_orders=related_open,
            onchain_fills=related_fills,
            expected_account_address=bundle.expected_account_address,
            expected_market_address=bundle.expected_market_address,
        )
        root_conflicts = list(_root_evidence_conflicts(bundle, lifecycle))
        order_key = str(order_id) if order_id is not None else None
        if order_key is not None and len(root_hashes.get(order_key, ())) > 1 and not root_lineage.get(order_key, False):
            root_conflicts.append("order_id_transaction_conflict")
        if root_conflicts:
            graph = replace(
                graph,
                graph_status="conflicting",
                authoritative=False,
                reconciliation_complete=False,
                blockers=tuple(dict.fromkeys((*graph.blockers, *root_conflicts))),
                conflicts=tuple(dict.fromkeys((*graph.conflicts, *root_conflicts))),
            )
        graphs.append(graph)
    return tuple(sorted(graphs, key=lambda graph: (str(graph.root_order_id or ""), str(graph.root_transaction_hash or ""), graph.graph_fingerprint)))


def _related_count(records: Sequence[Any], graphs: Sequence[DreamDexOrderReconciliationGraph], field: str) -> int:
    linked: set[str] = set()
    for graph in graphs:
        if graph.root_order_id is not None:
            linked.add(str(graph.root_order_id))
    return sum(1 for item in records if _get(item, "order_id") is not None and str(_get(item, "order_id")) not in linked)


def build_reconciliation_bridge_result(bundle: DreamDexReconciliationEvidenceBundle) -> DreamDexReconciliationBridgeResult:
    graphs = build_reconciliation_graphs_from_bundle(bundle)
    root_count = len(bundle.lifecycle_records)
    eligible_count = len(graphs)
    conflicts = list(bundle.conflicts)
    conflicts.extend(graph.conflicts for graph in graphs)
    flattened_conflicts: list[str] = []
    for value in conflicts:
        flattened_conflicts.extend(value if isinstance(value, tuple) else (value,))
    blockers = list(bundle.inventory.unresolved_reasons)
    blockers.extend(graph.blockers for graph in graphs)
    flattened_blockers: list[str] = []
    for value in blockers:
        flattened_blockers.extend(value if isinstance(value, tuple) else (value,))
    if not bundle.lifecycle_records:
        flattened_blockers.append("transaction_lifecycle_unavailable")
    if not graphs:
        flattened_blockers.append("reconciliation_graph_unavailable")
    if not bundle.order_metadata_records:
        flattened_blockers.append("order_metadata_unavailable")
    if not bundle.authenticated_orders:
        flattened_blockers.append("authenticated_order_state_unavailable")
    if not bundle.onchain_fills:
        flattened_blockers.extend(("fill_coverage_unavailable", "fill_coverage_incomplete"))
    flattened_blockers = list(dict.fromkeys(str(item) for item in flattened_blockers))
    flattened_conflicts = list(dict.fromkeys(str(item) for item in flattened_conflicts))
    complete_graphs = sum(1 for graph in graphs if graph.reconciliation_complete)
    authoritative_graphs = sum(1 for graph in graphs if graph.authoritative)
    bridge_authoritative = bool(bundle.authoritative and graphs and complete_graphs == len(graphs) and not flattened_blockers and not flattened_conflicts)
    payload = {
        "schema_version": SCHEMA_VERSION, "bundle_fingerprint": bundle.bundle_fingerprint,
        "graphs": sorted(graph.graph_fingerprint for graph in graphs), "root_lifecycle_count": root_count,
        "eligible_root_count": eligible_count, "unrelated_metadata_count": _related_count(bundle.order_metadata_records, graphs, "order_id"),
        "unrelated_authenticated_order_count": _related_count(bundle.authenticated_orders, graphs, "order_id"),
        "unrelated_open_order_count": _related_count(bundle.authenticated_open_orders, graphs, "order_id"),
        "unrelated_fill_count": _related_count(bundle.onchain_fills, graphs, "order_id"),
        "status": "complete" if bridge_authoritative else "partially_reconciled" if graphs else "unavailable",
        "blockers": flattened_blockers, "conflicts": flattened_conflicts,
    }
    return DreamDexReconciliationBridgeResult(
        bridge_status="complete" if bridge_authoritative else "partially_reconciled" if graphs else "unavailable",
        evidence_bundle=bundle, root_lifecycle_count=root_count, eligible_root_count=eligible_count,
        graph_count=len(graphs), graphs=graphs,
        unrelated_metadata_count=payload["unrelated_metadata_count"], unrelated_authenticated_order_count=payload["unrelated_authenticated_order_count"],
        unrelated_open_order_count=payload["unrelated_open_order_count"], unrelated_fill_count=payload["unrelated_fill_count"],
        conflicting_root_count=sum(1 for graph in graphs if graph.conflicts), bridge_fingerprint=_digest(payload),
        authoritative=bridge_authoritative, reconciliation_complete=bridge_authoritative,
        blockers=tuple(flattened_blockers), conflicts=tuple(flattened_conflicts), unresolved_reasons=bundle.unresolved_reasons,
    )


def build_reconciliation_bridge_preview(result: DreamDexReconciliationBridgeResult | DreamDexReconciliationEvidenceBundle) -> DreamDexReconciliationBridgePreview:
    if isinstance(result, DreamDexReconciliationEvidenceBundle):
        result = build_reconciliation_bridge_result(result)
    inventory = result.evidence_bundle.inventory
    return DreamDexReconciliationBridgePreview(
        bridge_status=result.bridge_status,
        lifecycle_evidence_status=inventory.lifecycle_status,
        authenticated_order_evidence_status=inventory.authenticated_order_status,
        authenticated_open_order_evidence_status=inventory.authenticated_open_order_status,
        metadata_evidence_status=inventory.order_metadata_status,
        fill_evidence_status=inventory.onchain_fill_status,
        authenticated_pagination_complete=result.evidence_bundle.authenticated_pagination_complete,
        fill_pagination_complete=result.evidence_bundle.fill_pagination_complete,
        fill_reorg_status=result.evidence_bundle.fill_reorg_status,
        root_lifecycle_count=result.root_lifecycle_count,
        eligible_root_count=result.eligible_root_count,
        graph_count=result.graph_count,
        authoritative_graph_count=sum(1 for graph in result.graphs if graph.authoritative),
        complete_graph_count=sum(1 for graph in result.graphs if graph.reconciliation_complete),
        conflicting_graph_count=sum(1 for graph in result.graphs if graph.conflicts),
        unrelated_evidence_count=result.unrelated_metadata_count + result.unrelated_authenticated_order_count + result.unrelated_open_order_count + result.unrelated_fill_count,
        bundle_fingerprint=result.evidence_bundle.bundle_fingerprint,
        bridge_fingerprint=result.bridge_fingerprint,
        authoritative=result.authoritative,
        reconciliation_complete=result.reconciliation_complete,
        blockers=result.blockers,
    )


def serialize_reconciliation_bridge_diagnostics(result: DreamDexReconciliationBridgeResult | DreamDexReconciliationBridgePreview) -> dict[str, Any]:
    if isinstance(result, DreamDexReconciliationBridgePreview):
        return ensure_no_raw_sensitive_fields(result.safe_dict())
    return result.safe_dict()


def describe_reconciliation_bridge_capabilities() -> dict[str, str]:
    return {
        "build_evidence_inventory": "available_offline",
        "adapt_authenticated_orders": "available_offline",
        "adapt_order_metadata": "available_offline",
        "adapt_onchain_fills": "available_offline",
        "adapt_lifecycle_records": "available_offline",
        "build_evidence_bundle": "available_offline",
        "build_graphs_from_bundle": "available_offline",
        "build_bridge_preview": "available_offline",
        "serialize_bridge_diagnostics": "available_offline",
        "fetch_authenticated_orders": "unavailable",
        "fetch_order_metadata_live": "unavailable",
        "fetch_onchain_fills_live": "unavailable",
        "fetch_lifecycle_live": "unavailable",
        "resolve_identity_live": "unavailable",
        "sign_transaction": "unavailable",
        "submit_transaction": "unavailable",
    }


def build_reconciliation_bridge_from_evidence(**kwargs: Any) -> DreamDexReconciliationBridgeResult:
    return build_reconciliation_bridge_result(build_reconciliation_evidence_bundle(**kwargs))


build_evidence_inventory = build_reconciliation_evidence_inventory
build_evidence_bundle = build_reconciliation_evidence_bundle
build_graphs_from_bundle = build_reconciliation_graphs_from_bundle
build_bridge_preview = build_reconciliation_bridge_preview
serialize_bridge_diagnostics = serialize_reconciliation_bridge_diagnostics
build_reconciliation_bridge = build_reconciliation_bridge_from_evidence


__all__ = [
    "SCHEMA_VERSION", "DreamDexReconciliationEvidenceInventory", "DreamDexReconciliationEvidenceBundle",
    "DreamDexReconciliationBridgeResult", "DreamDexReconciliationBridgePreview",
    "DreamDexEvidenceProducerAudit", "AUDITED_EVIDENCE_PRODUCERS", "audit_evidence_producers",
    "adapt_authenticated_orders_for_reconciliation", "adapt_authenticated_open_orders_for_reconciliation",
    "adapt_order_metadata_for_reconciliation", "adapt_onchain_fills_for_reconciliation",
    "adapt_lifecycle_records_for_reconciliation", "build_reconciliation_evidence_inventory",
    "build_reconciliation_evidence_bundle", "build_reconciliation_graphs_from_bundle",
    "build_reconciliation_bridge_result", "build_reconciliation_bridge_from_evidence",
    "build_reconciliation_bridge_preview", "serialize_reconciliation_bridge_diagnostics",
    "describe_reconciliation_bridge_capabilities",
    "build_evidence_inventory", "build_evidence_bundle", "build_graphs_from_bundle",
    "build_bridge_preview", "serialize_bridge_diagnostics",
    "build_reconciliation_bridge",
]
