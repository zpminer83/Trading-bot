"""Deterministic, offline reconciliation graph for DreamDEX order evidence.

This module is intentionally a graph/diagnostics layer only.  It accepts
already materialised evidence objects and never reads configuration, performs
I/O, authenticates, signs, submits, polls, or mutates trading state.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
import re
from typing import Any, Mapping, Sequence


SCHEMA_VERSION = "1"
NODE_TYPES = frozenset({
    "unsigned_request", "unsigned_envelope", "transaction_lifecycle", "transaction_receipt",
    "transaction_event", "order_identity", "order_metadata", "authenticated_order",
    "authenticated_open_order", "onchain_fill", "account_identity", "market_identity",
})
EDGE_TYPES = frozenset({
    "request_to_envelope", "envelope_to_transaction", "transaction_to_receipt",
    "transaction_to_event", "event_to_order_id", "order_id_to_metadata",
    "order_id_to_authenticated_order", "order_id_to_open_order", "order_id_to_fill",
    "account_to_order", "market_to_order", "replacement_of", "conflicts_with",
})
MATCH_STATUSES = frozenset({"confirmed", "partial", "mismatch", "unavailable", "not_applicable"})
RECONCILIATION_STATUSES = frozenset({"unavailable", "structurally_linked", "partially_reconciled", "conflicting", "complete"})
_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_HASH_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")
_MAX_UINT128 = (1 << 128) - 1


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)


def _fingerprint(value: Any) -> str:
    return sha256(_canonical(value).encode("utf-8")).hexdigest()


def _unique(values: Sequence[Any]) -> tuple[Any, ...]:
    result: list[Any] = []
    for value in values:
        if value not in result:
            result.append(value)
    return tuple(result)


def _pairs(value: Mapping[str, Any] | Sequence[tuple[str, Any]] | None) -> tuple[tuple[str, Any], ...]:
    if value is None:
        return ()
    items = value.items() if isinstance(value, Mapping) else value
    return tuple(sorted(((str(k), v) for k, v in items), key=lambda item: item[0]))


def _map(value: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    return {str(k): v for k, v in value}


def _get(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _mask(value: Any, *, kind: str | None = None) -> Any:
    if value is None:
        return None
    text = str(value)
    if kind == "address" or _ADDRESS_RE.fullmatch(text):
        return text[:4] + "..." + text[-4:]
    if kind == "hash" or _HASH_RE.fullmatch(text):
        return text[:6] + "..." + text[-4:]
    return text


def _safe_pairs(value: Sequence[tuple[str, Any]]) -> tuple[tuple[str, Any], ...]:
    result: list[tuple[str, Any]] = []
    secret_words = ("calldata", "topic", "data", "token", "cookie", "signature", "nonce", "balance", "price", "quantity", "address", "hash")
    for key, raw in value:
        lower = key.lower()
        if any(word in lower for word in secret_words):
            if "quantity" in lower and isinstance(raw, (int, float, Decimal)):
                result.append((key, str(raw)))
            elif lower.endswith("_status") or lower.endswith("_count"):
                result.append((key, raw))
            elif "address" in lower:
                result.append((key, _mask(raw, kind="address")))
            elif "hash" in lower:
                result.append((key, _mask(raw, kind="hash")))
            continue
        if isinstance(raw, (str, int, float, bool)) or raw is None:
            result.append((key, raw))
    return tuple(result)


@dataclass(frozen=True, repr=False)
class DreamDexReconciliationNode:
    node_id: str
    node_type: str
    source_status: str = "unavailable"
    source_fingerprint: str | None = None
    authoritative: bool = False
    identifiers: tuple[tuple[str, Any], ...] | Mapping[str, Any] = field(default_factory=tuple)
    safe_metadata: tuple[tuple[str, Any], ...] | Mapping[str, Any] = field(default_factory=tuple)
    conflicts: tuple[str, ...] = ()
    unresolved_reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.node_type not in NODE_TYPES:
            raise ValueError("unsupported_node_type")
        object.__setattr__(self, "identifiers", _pairs(self.identifiers))
        object.__setattr__(self, "safe_metadata", _pairs(self.safe_metadata))
        object.__setattr__(self, "conflicts", _unique(tuple(str(x) for x in self.conflicts)))
        object.__setattr__(self, "unresolved_reasons", _unique(tuple(str(x) for x in self.unresolved_reasons)))

    def safe_dict(self) -> dict[str, Any]:
        identifiers: dict[str, Any] = {}
        for key, value in self.identifiers:
            kind = "address" if "address" in key.lower() or key.lower() in {"owner", "market", "pool"} else "hash" if "hash" in key.lower() else None
            identifiers[key] = _mask(value, kind=kind)
        return {
            "node_id": self.node_id, "node_type": self.node_type, "source_status": self.source_status,
            "source_fingerprint": self.source_fingerprint, "authoritative": bool(self.authoritative),
            "identifiers": identifiers, "safe_metadata": dict(_safe_pairs(self.safe_metadata)),
            "conflicts": self.conflicts, "unresolved_reasons": self.unresolved_reasons,
        }

    def __repr__(self) -> str:
        return f"DreamDexReconciliationNode(node_id={self.node_id!r}, node_type={self.node_type!r}, source_status={self.source_status!r}, authoritative={self.authoritative!r})"


@dataclass(frozen=True, repr=False)
class DreamDexReconciliationEdge:
    edge_id: str
    edge_type: str
    from_node_id: str
    to_node_id: str
    match_status: str = "unavailable"
    match_fields: tuple[str, ...] = ()
    mismatch_fields: tuple[str, ...] = ()
    source_status: str = "unavailable"
    authoritative: bool = False
    conflicts: tuple[str, ...] = ()
    unresolved_reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.edge_type not in EDGE_TYPES:
            raise ValueError("unsupported_edge_type")
        if self.match_status not in MATCH_STATUSES:
            raise ValueError("unsupported_match_status")
        object.__setattr__(self, "match_fields", _unique(tuple(str(x) for x in self.match_fields)))
        object.__setattr__(self, "mismatch_fields", _unique(tuple(str(x) for x in self.mismatch_fields)))
        object.__setattr__(self, "conflicts", _unique(tuple(str(x) for x in self.conflicts)))
        object.__setattr__(self, "unresolved_reasons", _unique(tuple(str(x) for x in self.unresolved_reasons)))

    def safe_dict(self) -> dict[str, Any]:
        return {"edge_id": self.edge_id, "edge_type": self.edge_type, "from_node_id": self.from_node_id, "to_node_id": self.to_node_id, "match_status": self.match_status, "match_fields": self.match_fields, "mismatch_fields": self.mismatch_fields, "source_status": self.source_status, "authoritative": bool(self.authoritative), "conflicts": self.conflicts, "unresolved_reasons": self.unresolved_reasons}

    def __repr__(self) -> str:
        return f"DreamDexReconciliationEdge(edge_id={self.edge_id!r}, edge_type={self.edge_type!r}, match_status={self.match_status!r})"


@dataclass(frozen=True, repr=False)
class DreamDexOrderIdentityEvidence:
    order_id: int | None = None
    order_id_status: str = "unavailable"
    operation: str | None = None
    market_address: str | None = None
    owner_address: str | None = None
    transaction_hash: str | None = None
    request_fingerprint: str | None = None
    envelope_fingerprint: str | None = None
    lifecycle_fingerprint: str | None = None
    source_type: str = "unavailable"
    source_status: str = "unavailable"
    authoritative: bool = False
    conflicts: tuple[str, ...] = ()
    unresolved_reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if isinstance(self.order_id, str) and self.order_id.isdigit():
            object.__setattr__(self, "order_id", int(self.order_id))
        if self.order_id is not None and (isinstance(self.order_id, bool) or not isinstance(self.order_id, int) or not 0 <= self.order_id <= _MAX_UINT128):
            raise ValueError("order_id: invalid_uint128")
        if self.order_id_status not in {"confirmed", "unavailable", "conflicting", "partial"}:
            raise ValueError("order_id_status: unsupported")
        for field_name in ("market_address", "owner_address"):
            value = getattr(self, field_name)
            if value is not None and not _ADDRESS_RE.fullmatch(value):
                raise ValueError(f"{field_name}: invalid_address")
            if isinstance(value, str):
                object.__setattr__(self, field_name, value.lower())
        if self.transaction_hash is not None and not _HASH_RE.fullmatch(self.transaction_hash):
            raise ValueError("transaction_hash: invalid_hash")
        if isinstance(self.transaction_hash, str):
            object.__setattr__(self, "transaction_hash", self.transaction_hash.lower())
        object.__setattr__(self, "conflicts", _unique(tuple(str(x) for x in self.conflicts)))
        object.__setattr__(self, "unresolved_reasons", _unique(tuple(str(x) for x in self.unresolved_reasons)))

    def safe_dict(self) -> dict[str, Any]:
        return {"order_id": self.order_id, "order_id_status": self.order_id_status, "operation": self.operation, "market_address": _mask(self.market_address, kind="address"), "owner_address": _mask(self.owner_address, kind="address"), "transaction_hash": _mask(self.transaction_hash, kind="hash"), "request_fingerprint": self.request_fingerprint, "envelope_fingerprint": self.envelope_fingerprint, "lifecycle_fingerprint": self.lifecycle_fingerprint, "source_type": self.source_type, "source_status": self.source_status, "authoritative": bool(self.authoritative), "conflicts": self.conflicts, "unresolved_reasons": self.unresolved_reasons}

    def __repr__(self) -> str:
        return f"DreamDexOrderIdentityEvidence(order_id={self.order_id!r}, order_id_status={self.order_id_status!r}, transaction_hash={_mask(self.transaction_hash, kind='hash')!r}, authoritative={self.authoritative!r})"


@dataclass(frozen=True, repr=False)
class DreamDexOrderReconciliationGraph:
    schema_version: str
    graph_id: str
    nodes: tuple[DreamDexReconciliationNode, ...] = ()
    edges: tuple[DreamDexReconciliationEdge, ...] = ()
    root_order_id: int | None = None
    root_transaction_hash: str | None = None
    account_address: str | None = None
    market_address: str | None = None
    graph_fingerprint: str = ""
    graph_status: str = "unavailable"
    authoritative: bool = False
    reconciliation_complete: bool = False
    blockers: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()
    unresolved_reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.graph_status not in RECONCILIATION_STATUSES:
            raise ValueError("unsupported_graph_status")
        if isinstance(self.root_order_id, str) and self.root_order_id.isdigit():
            object.__setattr__(self, "root_order_id", int(self.root_order_id))
        if self.root_order_id is not None and (isinstance(self.root_order_id, bool) or not isinstance(self.root_order_id, int) or not 0 <= self.root_order_id <= _MAX_UINT128):
            raise ValueError("root_order_id: invalid_uint128")
        if self.root_transaction_hash is not None and not _HASH_RE.fullmatch(self.root_transaction_hash):
            raise ValueError("root_transaction_hash: invalid_hash")
        if self.account_address is not None and not _ADDRESS_RE.fullmatch(self.account_address):
            raise ValueError("account_address: invalid_address")
        if self.market_address is not None and not _ADDRESS_RE.fullmatch(self.market_address):
            raise ValueError("market_address: invalid_address")
        object.__setattr__(self, "nodes", tuple(self.nodes)); object.__setattr__(self, "edges", tuple(self.edges))
        object.__setattr__(self, "blockers", _unique(tuple(str(x) for x in self.blockers)))
        object.__setattr__(self, "conflicts", _unique(tuple(str(x) for x in self.conflicts)))
        object.__setattr__(self, "unresolved_reasons", _unique(tuple(str(x) for x in self.unresolved_reasons)))
        if self.authoritative or self.reconciliation_complete:
            if self.conflicts or self.blockers:
                raise ValueError("complete_graph_cannot_have_blockers")

    def safe_dict(self) -> dict[str, Any]:
        return serialize_order_reconciliation_diagnostics(self)

    def __repr__(self) -> str:
        return f"DreamDexOrderReconciliationGraph(graph_id={self.graph_id!r}, graph_status={self.graph_status!r}, nodes={len(self.nodes)}, edges={len(self.edges)}, authoritative=False)"


@dataclass(frozen=True, repr=False)
class DreamDexOrderReconciliationPreview:
    order_id_status: str
    transaction_hash_masked: str
    account_address_masked: str
    market_address_masked: str
    node_count: int
    edge_count: int
    confirmed_edges: int
    partial_edges: int
    mismatched_edges: int
    unavailable_edges: int
    lifecycle_status: str
    metadata_status: str
    authenticated_order_status: str
    open_order_status: str
    fills_status: str
    account_match_status: str
    market_match_status: str
    reconciliation_status: str
    authoritative: bool
    graph_fingerprint: str | None
    blockers: tuple[str, ...] = ()

    def safe_dict(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}

    def __repr__(self) -> str:
        return f"DreamDexOrderReconciliationPreview(reconciliation_status={self.reconciliation_status!r}, transaction_hash={self.transaction_hash_masked!r}, authoritative=False)"


@dataclass(frozen=True)
class OrderReconciliationValidationResult:
    valid: bool
    status: str
    errors: tuple[str, ...] = ()


def _source_status(value: Any) -> str:
    status = _get(value, "source_status", None)
    if isinstance(status, str):
        return status
    status = _get(value, "status", None)
    return status if isinstance(status, str) else "unavailable"


def _authoritative(value: Any) -> bool:
    return bool(_get(value, "authoritative", False))


def _source_fingerprint(value: Any, *names: str) -> str | None:
    for name in names:
        result = _get(value, name)
        if result:
            return str(result)
    return None


def _detail(value: Any, name: str, default: Any = None) -> Any:
    direct = _get(value, name, None)
    if direct is not None:
        return direct
    source = _get(value, "source_status", None)
    nested = _get(source, name, None)
    return default if nested is None else nested


def _identifiers_for(value: Any, node_type: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    fields = {
        "unsigned_request": ("operation", "chain_id", "from_address", "to_address", "calldata_sha256"),
        "unsigned_envelope": ("operation", "chain_id", "from_address", "to_address", "request_fingerprint", "envelope_fingerprint"),
        "transaction_lifecycle": ("operation", "transaction_hash", "request_fingerprint", "envelope_fingerprint", "lifecycle_fingerprint", "order_id"),
        "transaction_receipt": ("transaction_hash", "from_address", "to_address", "block_number"),
        "transaction_event": ("event_name", "transaction_hash", "contract_address", "order_id", "log_index"),
        "order_identity": ("order_id", "transaction_hash", "market_address", "owner_address"),
        "order_metadata": ("order_id", "symbol", "owner", "status"),
        "authenticated_order": ("order_id", "symbol", "account_identifier", "raw_status_name"),
        "authenticated_open_order": ("order_id", "symbol", "account_identifier", "raw_status_name"),
        "onchain_fill": ("fill_id", "order_id", "taker_order_id", "maker_order_id", "transaction_hash", "symbol", "quantity", "price", "notional"),
        "account_identity": ("account_address", "account_identifier", "subject"),
        "market_identity": ("market_address", "symbol", "pool_address"),
    }.get(node_type, ())
    for name in fields:
        current = _get(value, name)
        if current is not None:
            result[name] = current
    return result


def _node(node_type: str, value: Any, key: str, *, unresolved: Sequence[str] = ()) -> DreamDexReconciliationNode:
    ids = _identifiers_for(value, node_type)
    safe = {"status": _source_status(value)}
    for name in ("operation", "symbol", "raw_status_name", "status", "order_id", "fill_id", "block_number", "log_index", "quantity", "source_status"):
        current = _get(value, name)
        if current is not None:
            safe[name] = str(current) if isinstance(current, Decimal) else current
    node_id = f"{node_type}:{key}"
    return DreamDexReconciliationNode(node_id, node_type, _source_status(value), _source_fingerprint(value, "fingerprint", "lifecycle_fingerprint", "request_fingerprint", "envelope_fingerprint"), _authoritative(value), ids, safe, tuple(_get(value, "conflicts", ()) or ()), tuple(unresolved))


def _status_for_pair(left: Any, right: Any, fields: Sequence[tuple[str, str]]) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    if left is None or right is None:
        return "unavailable", (), ("evidence_unavailable",)
    matches: list[str] = []
    mismatches: list[str] = []
    for label, field_name in fields:
        a, b = _get(left, field_name), _get(right, field_name)
        if a is None or b is None:
            continue
        if str(a).lower() == str(b).lower():
            matches.append(label)
        else:
            mismatches.append(label)
    if mismatches:
        return "mismatch", tuple(matches), tuple(mismatches)
    if matches and len(matches) == len(fields):
        return "confirmed", tuple(matches), ()
    return "partial" if matches else "unavailable", tuple(matches), ("evidence_incomplete",) if not matches else ()


def _edge(edge_type: str, left: DreamDexReconciliationNode | None, right: DreamDexReconciliationNode | None, status: str, match: Sequence[str] = (), mismatch: Sequence[str] = (), *, reason: str | None = None, authoritative: bool = False) -> DreamDexReconciliationEdge:
    from_id = left.node_id if left else "<missing>"
    to_id = right.node_id if right else "<missing>"
    payload = {"type": edge_type, "from": from_id, "to": to_id}
    return DreamDexReconciliationEdge(f"{edge_type}:{_fingerprint(payload)[:16]}", edge_type, from_id, to_id, status, tuple(match), tuple(mismatch), "source_confirmed" if status == "confirmed" else "unavailable", authoritative or status == "confirmed", (reason,) if reason else (), (reason,) if reason else ())


def _as_sequence(value: Any) -> tuple[Any, ...]:
    if value is None:
        return ()
    if isinstance(value, Mapping):
        return (value,)
    if isinstance(value, (str, bytes)):
        return ()
    try:
        return tuple(value)
    except TypeError:
        return (value,)


def _stable_key(value: Any, fallback: str) -> str:
    for name in ("fill_id", "order_id", "orderId", "id", "client_order_id", "transaction_hash"):
        current = _get(value, name)
        if current is not None:
            return str(current)
    return fallback


def _records_from_source(value: Any, keys: Sequence[str]) -> tuple[Any, ...]:
    """Unwrap a source collection while retaining the caller's source object."""
    if isinstance(value, Mapping):
        for key in keys:
            rows = value.get(key)
            if isinstance(rows, (list, tuple)):
                return tuple(rows)
    else:
        for key in keys:
            rows = getattr(value, key, None)
            if isinstance(rows, (list, tuple)):
                return tuple(rows)
    return _as_sequence(value)


def _order_ids(value: Any) -> list[int]:
    result: list[int] = []
    for name in ("order_id", "taker_order_id", "maker_order_id"):
        raw = _get(value, name)
        if raw is None:
            continue
        try:
            parsed = int(raw)
        except (TypeError, ValueError):
            continue
        if 0 <= parsed <= _MAX_UINT128:
            result.append(parsed)
    return result


def _make_identity(order_id: int, *, source: Any, source_type: str, operation: str | None = None) -> DreamDexOrderIdentityEvidence:
    return DreamDexOrderIdentityEvidence(order_id=order_id, order_id_status="confirmed", operation=operation or _get(source, "operation"), market_address=_get(source, "market_address") or _get(source, "pool_address"), owner_address=_get(source, "owner_address") or _get(source, "owner"), transaction_hash=_get(source, "transaction_hash"), request_fingerprint=_get(source, "request_fingerprint"), envelope_fingerprint=_get(source, "envelope_fingerprint"), lifecycle_fingerprint=_get(source, "lifecycle_fingerprint") or _get(source, "fingerprint"), source_type=source_type, source_status=_source_status(source), authoritative=_authoritative(source))


def build_order_reconciliation_graph(*, unsigned_request: Any = None, unsigned_envelope: Any = None, lifecycle_record: Any = None, order_metadata_records: Sequence[Any] = (), authenticated_orders: Sequence[Any] = (), authenticated_open_orders: Sequence[Any] = (), onchain_fills: Sequence[Any] = (), order_identity_evidence: Sequence[Any] = (), expected_account_address: str | None = None, expected_market_address: str | None = None, expected_account: str | None = None, expected_market: str | None = None) -> DreamDexOrderReconciliationGraph:
    """Build a graph from already supplied evidence; never fetches anything."""
    expected_account_address = expected_account_address or expected_account
    expected_market_address = expected_market_address or expected_market
    nodes: list[DreamDexReconciliationNode] = []
    edges: list[DreamDexReconciliationEdge] = []
    conflicts: list[str] = []
    blockers: list[str] = []
    unresolved: list[str] = []
    by_id: dict[str, DreamDexReconciliationNode] = {}

    def add(node: DreamDexReconciliationNode) -> DreamDexReconciliationNode:
        by_id[node.node_id] = node; nodes.append(node); return node

    request_node = add(_node("unsigned_request", unsigned_request, "root")) if unsigned_request is not None else None
    envelope_node = add(_node("unsigned_envelope", unsigned_envelope, "root")) if unsigned_envelope is not None else None
    lifecycle_node = add(_node("transaction_lifecycle", lifecycle_record, "root")) if lifecycle_record is not None else None
    receipt_node = None
    event_nodes: list[DreamDexReconciliationNode] = []
    if lifecycle_record is not None and _get(lifecycle_record, "receipt_evidence") is not None:
        receipt_node = add(_node("transaction_receipt", _get(lifecycle_record, "receipt_evidence"), "root"))
    if lifecycle_record is not None:
        for index, event in enumerate(_as_sequence(_get(lifecycle_record, "event_evidence", ()) or ())):
            event_nodes.append(add(_node("transaction_event", event, str(index))))
    if request_node and envelope_node:
        status, match, mismatch = _status_for_pair(unsigned_request, unsigned_envelope, (("request_fingerprint", "request_fingerprint"), ("chain_id", "chain_id"), ("from_address", "from_address"), ("to_address", "to_address")))
        edges.append(_edge("request_to_envelope", request_node, envelope_node, status, match, mismatch, reason="request_envelope_conflict" if status == "mismatch" else None))
        if status == "mismatch": conflicts.append("request_envelope_conflict")
    if envelope_node and lifecycle_node:
        status, match, mismatch = _status_for_pair(unsigned_envelope, lifecycle_record, (("envelope_fingerprint", "envelope_fingerprint"),))
        edges.append(_edge("envelope_to_transaction", envelope_node, lifecycle_node, status, match, mismatch, reason="envelope_lifecycle_conflict" if status == "mismatch" else None))
        if status == "mismatch": conflicts.append("envelope_lifecycle_conflict")
    if lifecycle_node and receipt_node:
        status, match, mismatch = _status_for_pair(lifecycle_record, _get(lifecycle_record, "receipt_evidence"), (("transaction_hash", "transaction_hash"),))
        edges.append(_edge("transaction_to_receipt", lifecycle_node, receipt_node, status, match, mismatch, reason="transaction_receipt_conflict" if status == "mismatch" else None))
        if status == "mismatch": conflicts.append("transaction_receipt_conflict")
    for event_node, event in zip(event_nodes, _as_sequence(_get(lifecycle_record, "event_evidence", ()) or ())):
        status, match, mismatch = _status_for_pair(lifecycle_record, event, (("transaction_hash", "transaction_hash"),))
        edges.append(_edge("transaction_to_event", lifecycle_node, event_node, status, match, mismatch, reason="transaction_event_conflict" if status == "mismatch" else None))
        if status == "mismatch": conflicts.append("transaction_event_conflict")

    metadata_records = sorted(_records_from_source(order_metadata_records, ("records", "orders", "items", "data")), key=lambda value: _stable_key(_get(value, "metadata") or value, "metadata"))
    authenticated_records = sorted(_records_from_source(authenticated_orders, ("records", "orders", "items", "data")), key=lambda value: _stable_key(value, "authenticated"))
    open_records = sorted(_records_from_source(authenticated_open_orders, ("records", "orders", "items", "data")), key=lambda value: _stable_key(value, "open"))
    fill_records = sorted(_records_from_source(onchain_fills, ("fills", "records", "items", "data")), key=lambda value: _stable_key(value, "fill"))

    # A source-confirmed event is the preferred root identity.  Metadata and
    # authenticated records are deliberately never guessed into the root.
    identities: dict[int, list[DreamDexOrderIdentityEvidence]] = {}
    if lifecycle_record is not None:
        for event in _as_sequence(_get(lifecycle_record, "event_evidence", ()) or ()):
            for order_id in _order_ids(event):
                if _source_status(event) == "source_confirmed" and _get(event, "order_id") is not None:
                    identities.setdefault(order_id, []).append(_make_identity(order_id, source=event, source_type="transaction_event", operation=_get(lifecycle_record, "operation")))
    for record in authenticated_records + open_records:
        for order_id in _order_ids(record):
            identities.setdefault(order_id, []).append(_make_identity(order_id, source=record, source_type="authenticated_order"))
    for record in fill_records:
        for order_id in _order_ids(record):
            identities.setdefault(order_id, []).append(_make_identity(order_id, source=record, source_type="onchain_fill"))
    for record in _as_sequence(order_identity_evidence):
        for order_id in _order_ids(record):
            identities.setdefault(order_id, []).append(_make_identity(order_id, source=record, source_type="order_identity"))
    root_candidates = sorted(order_id for order_id, records in identities.items() if any(
        (item.source_type == "transaction_event" and item.source_status == "source_confirmed")
        or item.authoritative
        for item in records
    ))
    root_order_id = root_candidates[0] if len(root_candidates) == 1 else None
    if len(root_candidates) > 1:
        conflicts.append("multiple_order_ids")
    if identities and root_order_id is None:
        blockers.append("order_id_lifecycle_unconfirmed")
    if not identities:
        blockers.append("order_id_lifecycle_unconfirmed")

    # Materialise one identity node per observed order ID.  The root policy is
    # intentionally decided above; a metadata/fill ID remains a non-root
    # partial identity until a confirmed event or authoritative identity
    # source links it.
    identity_nodes: dict[int, DreamDexReconciliationNode] = {}
    for order_id in sorted(identities):
        evidence = identities[order_id][0]
        identity_nodes[order_id] = add(_node("order_identity", evidence, str(order_id)))
    for event_node, event in zip(event_nodes, _as_sequence(_get(lifecycle_record, "event_evidence", ()) or ())):
        for order_id in _order_ids(event):
            if order_id in identity_nodes:
                status = "confirmed" if _source_status(event) == "source_confirmed" else "partial"
                edges.append(_edge("event_to_order_id", event_node, identity_nodes[order_id], status, ("order_id",) if status == "confirmed" else ()))

    metadata_nodes: list[DreamDexReconciliationNode] = []
    for index, record in enumerate(metadata_records):
        metadata_node = add(_node("order_metadata", _get(record, "metadata") or record, _stable_key(_get(record, "metadata") or record, str(index)))); metadata_nodes.append(metadata_node)
        if _detail(record, "status", _source_status(record)) in {"unavailable", "unknown", "malformed", "conflicting"}:
            unresolved.append("order_metadata_unavailable")
        if not _authoritative(record) and not _authoritative(_get(record, "metadata")):
            unresolved.append("order_metadata_non_authoritative")
        for order_id in _order_ids(_get(record, "metadata") or record):
            identity_node = identity_nodes.get(order_id)
            if identity_node is None:
                identity_node = add(_node("order_identity", _make_identity(order_id, source=record, source_type="order_metadata"), str(order_id)))
                identity_nodes[order_id] = identity_node
            status = "confirmed" if root_order_id == order_id else "partial"
            edges.append(_edge("order_id_to_metadata", identity_node, metadata_node, status, ("order_id",) if status == "confirmed" else (), (), authoritative=False))
            if root_order_id is None and status == "partial": unresolved.append("metadata_order_id_not_authoritative")
    authenticated_nodes: list[DreamDexReconciliationNode] = []
    for index, record in enumerate(authenticated_records):
        authenticated_nodes.append(add(_node("authenticated_order", record, _stable_key(record, str(index)))) )
    open_nodes: list[DreamDexReconciliationNode] = []
    for index, record in enumerate(open_records):
        open_nodes.append(add(_node("authenticated_open_order", record, _stable_key(record, str(index)))) )
    fill_nodes: list[DreamDexReconciliationNode] = []
    for index, record in enumerate(fill_records):
        fill_nodes.append(add(_node("onchain_fill", record, _stable_key(record, str(index)))) )
    for node in authenticated_nodes:
        for order_id in _order_ids(next((record for record in authenticated_records if _node("authenticated_order", record, _stable_key(record, str(authenticated_records.index(record)))).node_id == node.node_id), None)):
            if order_id in identity_nodes: edges.append(_edge("order_id_to_authenticated_order", identity_nodes[order_id], node, "confirmed" if order_id == root_order_id else "partial", ("order_id",) if order_id == root_order_id else ()))
    for node in open_nodes:
        for record in open_records:
            if _node("authenticated_open_order", record, _stable_key(record, str(open_records.index(record)))).node_id != node.node_id: continue
            for order_id in _order_ids(record):
                if order_id in identity_nodes: edges.append(_edge("order_id_to_open_order", identity_nodes[order_id], node, "confirmed" if order_id == root_order_id else "partial", ("order_id",) if order_id == root_order_id else ()))
    for node, record in zip(fill_nodes, fill_records):
        for order_id in _order_ids(record):
            if order_id in identity_nodes: edges.append(_edge("order_id_to_fill", identity_nodes[order_id], node, "confirmed" if order_id == root_order_id else "partial", ("order_id",) if order_id == root_order_id else ()))

    # Authenticated sources remain non-authoritative unless their explicit
    # schema, pagination and authority metadata all say otherwise.
    for record in authenticated_records + open_records:
        if _detail(record, "pagination_complete", False) is not True:
            blockers.append("authenticated_pagination_incomplete")
        if _detail(record, "schema_status", "unknown") in {"unknown", "unavailable", "available_but_unverified_schema"}:
            unresolved.append("authenticated_schema_unverified")
        if _detail(record, "authority_status", "non_authoritative") != "authoritative":
            unresolved.append("authenticated_order_non_authoritative")
        subject = _get(record, "subject") or _get(record, "authenticated_subject")
        if subject is not None and expected_account_address and str(subject).lower() != expected_account_address.lower():
            conflicts.append("authenticated_subject_mismatch")

    # Cross-source identity conflicts are blockers, never an opportunity to
    # choose whichever record looks most convenient.
    transaction_hashes: set[str] = set()
    for value in (lifecycle_record, _get(lifecycle_record, "receipt_evidence") if lifecycle_record is not None else None):
        raw_hash = _get(value, "transaction_hash")
        if isinstance(raw_hash, str): transaction_hashes.add(raw_hash.lower())
    for event in _as_sequence(_get(lifecycle_record, "event_evidence", ()) if lifecycle_record is not None else ()):
        raw_hash = _get(event, "transaction_hash")
        if isinstance(raw_hash, str): transaction_hashes.add(raw_hash.lower())
    for fill in fill_records:
        raw_hash = _get(fill, "transaction_hash")
        if isinstance(raw_hash, str): transaction_hashes.add(raw_hash.lower())
    if len(transaction_hashes) > 1 and not _get(lifecycle_record, "replacement_evidence"):
        conflicts.append("multiple_transaction_hashes")
    # Stable fill IDs may be repeated only when the complete evidence agrees.
    seen_fills: dict[str, Any] = {}
    for fill in fill_records:
        fill_id = _get(fill, "fill_id")
        if fill_id is None: continue
        if str(fill_id) in seen_fills and _canonical(_identifiers_for(fill, "onchain_fill")) != _canonical(_identifiers_for(seen_fills[str(fill_id)], "onchain_fill")):
            conflicts.append("duplicate_fill_conflict")
        seen_fills[str(fill_id)] = fill
        if bool(_get(fill, "removed", False)) or _get(fill, "reorg_status") == "reorg_detected":
            blockers.append("reorg_status_unresolved")
    # Conflicting status observations for one order must not be collapsed.
    status_by_order: dict[int, set[str]] = {}
    for record in authenticated_records + open_records:
        raw_status = _get(record, "raw_status_name") or _get(record, "status")
        for order_id in _order_ids(record):
            if raw_status is not None: status_by_order.setdefault(order_id, set()).add(str(raw_status).lower())
    if any(len(values) > 1 for values in status_by_order.values()):
        conflicts.append("open_order_status_conflict")

    # Expected account/market are context, not inferred identity.
    if expected_account_address:
        if not _ADDRESS_RE.fullmatch(expected_account_address): raise ValueError("expected_account_address: invalid_address")
        account_node = add(DreamDexReconciliationNode("account:expected", "account_identity", "source_confirmed", None, False, {"account_address": expected_account_address}, {"status": "expected_context"}))
        for node in [n for n in nodes if n.node_type in {"order_metadata", "authenticated_order", "authenticated_open_order", "onchain_fill"}]:
            owner = _map(node.identifiers).get("owner_address") or _map(node.identifiers).get("account_identifier") or _map(node.identifiers).get("owner")
            status = "confirmed" if owner and str(owner).lower() == expected_account_address.lower() else "partial" if owner is None else "mismatch"
            edges.append(_edge("account_to_order", account_node, node, status, ("account",) if status == "confirmed" else (), ("account",) if status == "mismatch" else ()))
            if status == "mismatch": conflicts.append("account_identity_conflict")
    if expected_market_address:
        if not _ADDRESS_RE.fullmatch(expected_market_address): raise ValueError("expected_market_address: invalid_address")
        market_node = add(DreamDexReconciliationNode("market:expected", "market_identity", "source_confirmed", None, False, {"market_address": expected_market_address}, {"status": "expected_context"}))
        for node in [n for n in nodes if n.node_type in {"order_metadata", "authenticated_order", "authenticated_open_order", "onchain_fill"}]:
            market = _map(node.identifiers).get("market_address") or _map(node.identifiers).get("pool_address")
            status = "confirmed" if market and str(market).lower() == expected_market_address.lower() else "partial" if market is None else "mismatch"
            edges.append(_edge("market_to_order", market_node, node, status, ("market",) if status == "confirmed" else (), ("market",) if status == "mismatch" else ()))
            if status == "mismatch": conflicts.append("market_identity_conflict")

    if not nodes:
        blockers.append("reconciliation_graph_unavailable")
    if lifecycle_record is None: blockers.extend(("transaction_lifecycle_unavailable", "transaction_receipt_evidence_unavailable", "transaction_event_evidence_unavailable"))
    if lifecycle_record is not None and _get(lifecycle_record, "operation") == "reduce_order":
        blockers.append("reduce_event_semantics_unavailable")
    replacement_evidence = _get(lifecycle_record, "replacement_evidence") if lifecycle_record is not None else None
    if replacement_evidence is not None and lifecycle_node is not None:
        replacement_hash = _get(replacement_evidence, "replacement_transaction_hash")
        original_hash = _get(replacement_evidence, "original_transaction_hash") or _get(lifecycle_record, "transaction_hash")
        if replacement_hash and original_hash:
            replacement_node = add(DreamDexReconciliationNode("transaction_lifecycle:replacement", "transaction_lifecycle", _source_status(replacement_evidence), _source_fingerprint(replacement_evidence, "fingerprint"), _authoritative(replacement_evidence), {"transaction_hash": replacement_hash}, {"status": "replacement"}))
            edges.append(_edge("replacement_of", replacement_node, lifecycle_node, "confirmed" if str(original_hash).lower() != str(replacement_hash).lower() else "mismatch", ("replacement_hash",) if str(original_hash).lower() != str(replacement_hash).lower() else (), ("replacement_hash",) if str(original_hash).lower() == str(replacement_hash).lower() else (), reason="replacement_lineage_conflict" if str(original_hash).lower() == str(replacement_hash).lower() else None))
            if str(original_hash).lower() == str(replacement_hash).lower(): conflicts.append("replacement_lineage_conflict")
    if not order_metadata_records: blockers.append("order_metadata_unavailable")
    if not authenticated_orders: blockers.append("authenticated_order_state_unavailable")
    if not authenticated_open_orders: blockers.extend(("authenticated_pagination_incomplete",))
    if not onchain_fills: blockers.extend(("fill_coverage_unavailable", "fill_coverage_incomplete"))
    replacement_status = _get(_get(lifecycle_record, "evidence") if lifecycle_record is not None else None, "replacement_status") if lifecycle_record is not None else None
    if lifecycle_record is None or (_get(lifecycle_record, "replacement_evidence") is None and replacement_status not in {"observed", "resolved", "confirmed"}):
        blockers.append("replacement_lineage_unresolved")
    fill_reorg_statuses = []
    for fill in fill_records:
        fill_reorg_statuses.append(_get(fill, "reorg_status") or _get(_get(fill, "source_status"), "reorg_status"))
    if onchain_fills and not all(status in {"ok", "not_detected", "resolved", "confirmed"} for status in fill_reorg_statuses if status is not None):
        blockers.append("reorg_status_unresolved")
    elif not onchain_fills:
        blockers.append("reorg_status_unresolved")
    if conflicts: blockers.extend(conflicts)
    blockers = list(_unique(blockers))
    conflicts = list(_unique(conflicts))
    mismatch_edges = any(edge.match_status == "mismatch" for edge in edges)
    complete_requirements = bool(nodes and edges) and not blockers and not conflicts and all(node.authoritative for node in nodes) and all(edge.authoritative for edge in edges)
    if conflicts or mismatch_edges:
        graph_status = "conflicting"
    elif not nodes:
        graph_status = "unavailable"
    elif complete_requirements:
        graph_status = "complete"
    elif any(edge.match_status == "confirmed" for edge in edges):
        graph_status = "partially_reconciled" if (authenticated_orders or onchain_fills) else "structurally_linked"
    else:
        graph_status = "unavailable"
    tx_hash = _get(lifecycle_record, "transaction_hash") if lifecycle_record is not None else None
    if tx_hash is None and lifecycle_record is not None:
        tx_hash = _get(_get(lifecycle_record, "submission_evidence"), "transaction_hash")
    # The digest includes the canonical evidence identifiers/metadata so a
    # changed order ID, account, or fill quantity changes the graph identity;
    # only the resulting digest is exposed by diagnostics.
    graph_payload = {"schema_version": SCHEMA_VERSION, "nodes": [{"id": node.node_id, "type": node.node_type, "source_status": node.source_status, "authoritative": node.authoritative, "identifiers": node.identifiers, "safe_metadata": node.safe_metadata, "conflicts": node.conflicts, "unresolved": node.unresolved_reasons} for node in sorted(nodes, key=lambda item: item.node_id)], "edges": [edge.safe_dict() for edge in sorted(edges, key=lambda item: item.edge_id)], "root_order_id": root_order_id, "root_transaction_hash": tx_hash, "account_address": expected_account_address, "market_address": expected_market_address, "status": graph_status, "blockers": blockers, "conflicts": conflicts}
    graph_id = "graph:" + _fingerprint({"nodes": [node.node_id for node in nodes], "edges": [edge.edge_id for edge in edges]})[:16]
    return DreamDexOrderReconciliationGraph(SCHEMA_VERSION, graph_id, tuple(sorted(nodes, key=lambda item: item.node_id)), tuple(sorted(edges, key=lambda item: item.edge_id)), root_order_id, tx_hash, expected_account_address.lower() if expected_account_address else None, expected_market_address.lower() if expected_market_address else None, _fingerprint(graph_payload), graph_status, complete_requirements, complete_requirements, tuple(blockers), tuple(conflicts), tuple(_unique(unresolved)))


def validate_order_reconciliation_graph(graph: DreamDexOrderReconciliationGraph, *, operation: str | None = None) -> OrderReconciliationValidationResult:
    errors: list[str] = []
    if graph.graph_status not in RECONCILIATION_STATUSES: errors.append("graph_status_invalid")
    ids = {node.node_id for node in graph.nodes}
    for edge in graph.edges:
        if edge.from_node_id not in ids or edge.to_node_id not in ids: errors.append("edge_endpoint_missing")
        if edge.match_status == "mismatch": errors.extend(edge.mismatch_fields or ("edge_mismatch",))
    if graph.graph_status == "conflicting" or graph.conflicts: errors.extend(graph.conflicts or ("graph_conflict",))
    if operation in {"place_order", "cancel_order"}:
        if not any(edge.edge_type == "transaction_to_event" and edge.match_status == "confirmed" for edge in graph.edges):
            errors.append("required_event_unavailable")
        if graph.root_order_id is None:
            errors.append("order_id_unavailable")
    errors = list(_unique(errors))
    return OrderReconciliationValidationResult(not errors, "valid" if not errors else "blocked", tuple(errors))


def build_order_reconciliation_preview(graph: DreamDexOrderReconciliationGraph) -> DreamDexOrderReconciliationPreview:
    node_type = {node.node_type: node for node in graph.nodes}
    def status(name: str) -> str:
        return node_type[name].source_status if name in node_type else "unavailable"
    account_match = "mismatch" if "account_identity_conflict" in graph.conflicts else "unresolved" if "account_identity" not in node_type else "partial"
    market_match = "mismatch" if "market_identity_conflict" in graph.conflicts else "unresolved" if "market_identity" not in node_type else "partial"
    return DreamDexOrderReconciliationPreview("confirmed" if graph.root_order_id is not None else "unavailable", _mask(graph.root_transaction_hash, kind="hash") or "<missing>", _mask(graph.account_address, kind="address") or "<missing>", _mask(graph.market_address, kind="address") or "<missing>", len(graph.nodes), len(graph.edges), sum(e.match_status == "confirmed" for e in graph.edges), sum(e.match_status == "partial" for e in graph.edges), sum(e.match_status == "mismatch" for e in graph.edges), sum(e.match_status == "unavailable" for e in graph.edges), status("transaction_lifecycle"), status("order_metadata"), status("authenticated_order"), status("authenticated_open_order"), status("onchain_fill"), account_match, market_match, "linked" if graph.graph_status in {"structurally_linked", "partially_reconciled", "complete"} else "unavailable", graph.authoritative, graph.graph_fingerprint, graph.blockers)


def serialize_order_reconciliation_diagnostics(graph: DreamDexOrderReconciliationGraph) -> dict[str, Any]:
    preview = build_order_reconciliation_preview(graph)
    return {**preview.safe_dict(), "graph_id": graph.graph_id, "schema_version": graph.schema_version, "root_order_id": graph.root_order_id, "nodes": tuple(node.safe_dict() for node in graph.nodes), "edges": tuple(edge.safe_dict() for edge in graph.edges), "conflicts": graph.conflicts, "unresolved_reasons": graph.unresolved_reasons}


def describe_order_reconciliation_capabilities() -> Mapping[str, str]:
    return {
        "build_reconciliation_graph": "available_offline",
        "validate_reconciliation_graph": "available_offline",
        "correlate_request_envelope": "available_offline",
        "correlate_transaction_lifecycle": "available_offline",
        "correlate_order_metadata": "available_offline",
        "correlate_authenticated_orders": "available_offline",
        "correlate_onchain_fills": "available_offline",
        "build_reconciliation_preview": "available_offline",
        "serialize_safe_diagnostics": "available_offline",
        "fetch_authenticated_orders": "unavailable",
        "fetch_order_metadata_live": "unavailable",
        "fetch_fills_live": "unavailable",
        "resolve_account_identity_live": "unavailable",
        "submit_transaction": "unavailable",
    }


# Friendly aliases used by fixtures and external diagnostic callers.
ReconciliationNode = DreamDexReconciliationNode
ReconciliationEdge = DreamDexReconciliationEdge
OrderReconciliationGraph = DreamDexOrderReconciliationGraph
OrderReconciliationPreview = DreamDexOrderReconciliationPreview
OrderIdentityEvidence = DreamDexOrderIdentityEvidence
ReconciliationValidationResult = OrderReconciliationValidationResult
GraphValidationResult = OrderReconciliationValidationResult
build_reconciliation_graph = build_order_reconciliation_graph
validate_reconciliation_graph = validate_order_reconciliation_graph
build_reconciliation_preview = build_order_reconciliation_preview
serialize_safe_diagnostics = serialize_order_reconciliation_diagnostics

__all__ = [
    "SCHEMA_VERSION", "NODE_TYPES", "EDGE_TYPES", "MATCH_STATUSES", "RECONCILIATION_STATUSES",
    "DreamDexReconciliationNode", "DreamDexReconciliationEdge", "DreamDexOrderIdentityEvidence",
    "DreamDexOrderReconciliationGraph", "DreamDexOrderReconciliationPreview", "OrderReconciliationValidationResult",
    "build_order_reconciliation_graph", "validate_order_reconciliation_graph", "build_order_reconciliation_preview",
    "serialize_order_reconciliation_diagnostics", "describe_order_reconciliation_capabilities",
    "ReconciliationNode", "ReconciliationEdge", "OrderReconciliationGraph", "OrderReconciliationPreview", "OrderIdentityEvidence", "ReconciliationValidationResult", "GraphValidationResult",
    "build_reconciliation_graph", "validate_reconciliation_graph", "build_reconciliation_preview", "serialize_safe_diagnostics",
]
