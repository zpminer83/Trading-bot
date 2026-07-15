"""Offline order-metadata resolution and OrderFilled correlation.

The production transport is intentionally unconfigured.  This module records
what the Bot Kit actually confirms about the order endpoints and provides a
fixture-only resolver for deterministic reconciliation tests.  It never logs
credentials, sends authenticated requests, or exposes mutation methods.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import re
from typing import Any, Callable, Mapping, Protocol, Sequence

from bot.integrations.dreamdex_fill_events import NormalizedOrderFill


ORDER_ENDPOINT_DESCRIPTORS = (
    {
        "name": "order_by_id",
        "method": "GET",
        "path": "/markets/{symbol}/orders/{orderId}",
        "requires_authorization": True,
        "response_shape": "unknown raw order object",
        "pagination": "not_confirmed",
        "source_file": "packages/core/src/rest.ts",
        "source_lines": "94-96",
    },
    {
        "name": "orders_page",
        "method": "GET",
        "path": "/markets/{symbol}/orders",
        "requires_authorization": True,
        "response_shape": "list or {orders: [...]}",
        "pagination": "not_confirmed",
        "source_file": "examples/05-production-async/src/dreamdex_bot/core/rest_client.py",
        "source_lines": "280-296",
    },
)

CONFIRMED_ORDER_ALIASES = {
    "symbol": ("symbol", "market"),
    "order_id": ("orderId", "id"),
    "quantity": ("quantity", "amount"),
    "remaining_quantity": ("remainingQuantity", "remaining"),
}
CONFIRMED_STATUS_ALIASES = {"canceled": "cancelled", "cancelled": "cancelled"}
KNOWN_STATUS_ALIASES = {
    **CONFIRMED_STATUS_ALIASES,
    "open": "open",
    "filled": "filled",
    "partially_filled": "partially_filled",
    "partial": "partially_filled",
    "expired": "expired",
    "rejected": "rejected",
    "pending": "pending",
}


def _utc(value: Any = None, fallback: datetime | None = None) -> datetime:
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, (int, float)):
        result = datetime.fromtimestamp(float(value) / (1000 if value > 10_000_000_000 else 1), tz=timezone.utc)
    elif value:
        try:
            result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            result = fallback or datetime.now(timezone.utc)
    else:
        result = fallback or datetime.now(timezone.utc)
    return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result.astimezone(timezone.utc)


def _decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _address(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.lower()
    if not text.startswith("0x") or len(text) != 42 or any(c not in "0123456789abcdef" for c in text[2:]):
        return None
    return text


def _mask(value: Any) -> str:
    if not value:
        return "<missing>"
    text = str(value)
    return text if text.startswith("<") else ("***" if len(text) <= 8 else f"{text[:4]}...{text[-4:]}")


def _first(row: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        if name in row and row[name] is not None:
            return row[name]
    return None


@dataclass(frozen=True)
class OrderMetadataKey:
    symbol: str
    order_id: str


@dataclass(frozen=True)
class RawOrderMetadata:
    key: OrderMetadataKey
    payload: Mapping[str, Any]
    source_endpoint: str
    observed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(frozen=True)
class NormalizedOrderMetadata:
    key: OrderMetadataKey
    symbol: str | None
    order_id: str | None
    owner: str | None
    side: str | None
    is_bid: bool | None
    raw_price: str | None
    price: Decimal | None
    raw_quantity: str | None
    quantity: Decimal | None
    filled_quantity: Decimal | None
    remaining_quantity: Decimal | None
    raw_status: str | None
    status: str
    created_at: datetime | None
    updated_at: datetime | None
    client_order_id: str | None
    user_data: str | None
    source_endpoint: str
    observed_at: datetime
    source_available: bool
    owner_field_confirmed: bool
    malformed_fields: tuple[str, ...] = ()

    @property
    def malformed(self) -> bool:
        return bool(self.malformed_fields)

    @property
    def account_identifier(self) -> str:
        return _mask(self.owner)


@dataclass(frozen=True)
class OrderMetadataSourceStatus:
    status: str
    endpoint_name: str
    observed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    raw_status_name: str | None = None
    pagination_complete: bool = True
    next_cursor: str | None = None
    duplicate_count: int = 0
    conflict_count: int = 0
    malformed_count: int = 0
    reason: str | None = None
    error_code: str | None = None
    schema_fingerprint: Any | None = None
    response_body_status: str = "unknown"
    schema_status: str = "unknown"
    records_status: str = "unknown"
    pagination_status: str = "unresolved"
    authority_status: str = "non_authoritative"

    @property
    def available(self) -> bool:
        return self.status in {"available", "valid_confirmed_schema", "available_empty"}


@dataclass(frozen=True)
class OrderMetadataLookupResult:
    key: OrderMetadataKey
    metadata: NormalizedOrderMetadata | None
    source_status: OrderMetadataSourceStatus

    @property
    def status(self) -> str:
        return self.source_status.status

    @property
    def schema_fingerprint(self) -> Any | None:
        return self.source_status.schema_fingerprint


@dataclass(frozen=True)
class OrderFillMetadataCorrelation:
    fill_id: str
    taker: OrderMetadataLookupResult
    maker: OrderMetadataLookupResult
    status: str
    owner_match: bool | None
    side_match: bool | None
    market_match: bool | None
    quantity_valid: bool | None
    price_valid: bool | None
    remaining_quantity_consistent: bool | None
    reason: str | None = None


@dataclass(frozen=True)
class OrderMetadataResolverReport:
    status: str
    resolved_count: int = 0
    conflict_count: int = 0
    malformed_count: int = 0
    account_match: bool | None = None
    owner_match_count: int = 0
    correlations: tuple[OrderFillMetadataCorrelation, ...] = ()
    reason: str | None = None

    @classmethod
    def unavailable(cls, reason: str = "authenticated_transport_unconfigured") -> "OrderMetadataResolverReport":
        return cls("unavailable", reason=reason)

    @property
    def authoritative(self) -> bool:
        return self.status in {"matched", "partial_match"} and self.account_match is True and self.conflict_count == 0 and self.malformed_count == 0


class OrderMetadataReadOnlyTransport(Protocol):
    def fetch_order_by_id(self, symbol: str, order_id: str) -> OrderMetadataLookupResult: ...
    def fetch_orders_page(self, symbol: str, status: str | None = None, cursor: str | None = None) -> tuple[tuple[RawOrderMetadata, ...], OrderMetadataSourceStatus]: ...


class UnconfiguredOrderMetadataTransport:
    """Production default: no authenticated network request is attempted."""

    def fetch_order_by_id(self, symbol: str, order_id: str) -> OrderMetadataLookupResult:
        key = OrderMetadataKey(symbol, str(order_id))
        return OrderMetadataLookupResult(key, None, OrderMetadataSourceStatus("unconfigured", "order_by_id", reason="authenticated_transport_unconfigured", error_code="authenticated_transport_unconfigured"))

    def fetch_orders_page(self, symbol: str, status: str | None = None, cursor: str | None = None) -> tuple[tuple[RawOrderMetadata, ...], OrderMetadataSourceStatus]:
        return (), OrderMetadataSourceStatus("unconfigured", "orders_page", reason="authenticated_transport_unconfigured", error_code="authenticated_transport_unconfigured")


def _rows(payload: Any) -> tuple[Mapping[str, Any], ...]:
    if isinstance(payload, list):
        return tuple(row for row in payload if isinstance(row, Mapping))
    if isinstance(payload, Mapping):
        if isinstance(payload.get("orders"), list):
            return tuple(row for row in payload["orders"] if isinstance(row, Mapping))
        if isinstance(payload.get("records"), list):
            return tuple(row for row in payload["records"] if isinstance(row, Mapping))
        if isinstance(payload.get("data"), list):
            return tuple(row for row in payload["data"] if isinstance(row, Mapping))
        if any(key in payload for key in ("id", "orderId", "symbol", "market")):
            return (payload,)
    return ()


class FixtureOrderMetadataTransport:
    """Offline parser for explicitly supplied order metadata fixtures."""

    def __init__(self, fixture: Mapping[str, Any], *, now: datetime | None = None) -> None:
        self.fixture = fixture
        self.now = now or datetime.now(timezone.utc)

    def _section(self, symbol: str) -> Mapping[str, Any]:
        root = self.fixture.get("order_metadata", self.fixture.get("orders", self.fixture))
        if isinstance(root, Mapping):
            section = root.get(symbol, root)
            return section if isinstance(section, Mapping) else {"orders": section}
        return {"orders": root}

    def _status(self, endpoint: str, section: Mapping[str, Any], *, default_available: bool) -> OrderMetadataSourceStatus:
        raw = str(section.get("status", "available" if default_available else "unavailable"))
        normalized = raw.lower()
        if normalized in {"ok", "confirmed", "complete"}:
            normalized = "available"
        pagination = bool(section.get("pagination_complete", section.get("complete", normalized == "available")))
        return OrderMetadataSourceStatus(
            normalized, endpoint, _utc(section.get("observed_at"), self.now), raw,
            pagination_complete=pagination, next_cursor=section.get("next_cursor"),
            reason=section.get("reason"), error_code=section.get("error_code"),
        )

    def fetch_order_by_id(self, symbol: str, order_id: str) -> OrderMetadataLookupResult:
        key = OrderMetadataKey(symbol, str(order_id))
        section = self._section(symbol)
        by_id = section.get("by_id")
        if isinstance(by_id, Mapping) and order_id in by_id and isinstance(by_id[order_id], Mapping):
            rows = (by_id[order_id],)
        else:
            rows = _rows(by_id if by_id is not None else section.get("orders", section))
        matches = [row for row in rows if str(_first(row, "orderId", "id")) == str(order_id)]
        if not matches:
            if any(_first(row, "orderId", "id") is None for row in rows):
                return OrderMetadataLookupResult(key, None, replace(self._status("order_by_id", section, default_available=True), status="malformed", malformed_count=1, reason="malformed_order_id", error_code="malformed_order"))
            return OrderMetadataLookupResult(key, None, self._status("order_by_id", section, default_available=False))
        if len(matches) > 1 and matches[0] != matches[1]:
            return OrderMetadataLookupResult(key, None, replace(self._status("order_by_id", section, default_available=True), status="conflicting", conflict_count=len(matches) - 1, reason="conflicting_duplicate_order", error_code="conflicting_duplicate"))
        raw = RawOrderMetadata(key, matches[0], "GET /markets/{symbol}/orders/{orderId}", _utc(section.get("observed_at"), self.now))
        return OrderMetadataLookupResult(key, normalize_order_metadata(raw), self._status("order_by_id", section, default_available=True))

    def fetch_orders_page(self, symbol: str, status: str | None = None, cursor: str | None = None) -> tuple[tuple[RawOrderMetadata, ...], OrderMetadataSourceStatus]:
        section = self._section(symbol)
        source = self._status("orders_page", section, default_available="orders" in section or isinstance(section, list))
        rows = _rows(section.get("orders", section))
        records: list[RawOrderMetadata] = []
        seen: dict[str, Mapping[str, Any]] = {}
        duplicate = 0
        conflict = 0
        for row in rows:
            order_id = _first(row, "orderId", "id")
            if order_id is None:
                records.append(RawOrderMetadata(OrderMetadataKey(symbol, "<missing>"), row, "GET /markets/{symbol}/orders", source.observed_at))
                continue
            order_id = str(order_id)
            if order_id in seen:
                if seen[order_id] == row:
                    duplicate += 1
                else:
                    conflict += 1
                continue
            seen[order_id] = row
            records.append(RawOrderMetadata(OrderMetadataKey(symbol, order_id), row, "GET /markets/{symbol}/orders", source.observed_at))
        source = replace(source, duplicate_count=duplicate, conflict_count=conflict, malformed_count=sum(1 for row in records if row.key.order_id == "<missing>"), status="conflicting" if conflict else source.status, reason="conflicting_duplicate_order" if conflict else source.reason, error_code="conflicting_duplicate" if conflict else source.error_code)
        return tuple(records), source


def normalize_order_metadata(raw: RawOrderMetadata) -> NormalizedOrderMetadata:
    row = raw.payload
    malformed: list[str] = []
    symbol_value = _first(row, "symbol", "market")
    symbol = str(symbol_value) if symbol_value is not None else None
    order_id_value = _first(row, "orderId", "id")
    order_id = str(order_id_value) if order_id_value is not None else None
    if not order_id:
        malformed.append("order_id")
    owner_value = _first(row, "owner", "wallet", "account", "accountAddress", "walletAddress")
    owner = _address(owner_value)
    if owner_value is not None and owner is None:
        malformed.append("owner")
    is_bid_value = _first(row, "isBid", "is_bid")
    is_bid = bool(is_bid_value) if is_bid_value is not None else None
    side_value = _first(row, "side", "orderSide")
    side = str(side_value).lower() if side_value is not None else ("buy" if is_bid is True else "sell" if is_bid is False else None)
    raw_price_value = _first(row, "price", "limitPrice")
    raw_quantity_value = _first(row, "quantity", "amount")
    price = _decimal(raw_price_value)
    quantity = _decimal(raw_quantity_value)
    if raw_price_value is not None and price is None:
        malformed.append("price")
    if raw_quantity_value is not None and quantity is None:
        malformed.append("quantity")
    filled = _decimal(_first(row, "filledQuantity", "filled", "executedQuantity"))
    remaining = _decimal(_first(row, "remainingQuantity", "remaining"))
    if _first(row, "filledQuantity", "filled", "executedQuantity") is not None and filled is None:
        malformed.append("filled_quantity")
    if _first(row, "remainingQuantity", "remaining") is not None and remaining is None:
        malformed.append("remaining_quantity")
    raw_status_value = _first(row, "status")
    raw_status = str(raw_status_value) if raw_status_value is not None else None
    normalized_status = KNOWN_STATUS_ALIASES.get(raw_status.lower(), "unknown") if raw_status else "unknown"
    created = _utc(_first(row, "createdAt", "created_at"), raw.observed_at) if _first(row, "createdAt", "created_at") is not None else None
    updated = _utc(_first(row, "updatedAt", "updated_at", "timestamp"), raw.observed_at) if _first(row, "updatedAt", "updated_at", "timestamp") is not None else None
    confirmed_fields = row.get("confirmed_fields", ())
    if isinstance(confirmed_fields, str):
        confirmed_fields = (confirmed_fields,)
    owner_confirmed = "owner" in confirmed_fields or "account" in confirmed_fields or "wallet" in confirmed_fields
    return NormalizedOrderMetadata(
        raw.key, symbol, order_id, owner, side, is_bid,
        None if raw_price_value is None else str(raw_price_value), price,
        None if raw_quantity_value is None else str(raw_quantity_value), quantity,
        filled, remaining, raw_status, normalized_status, created, updated,
        None if _first(row, "clientOrderId", "client_order_id") is None else str(_first(row, "clientOrderId", "client_order_id")),
        None if _first(row, "userData", "user_data") is None else str(_first(row, "userData", "user_data")),
        raw.source_endpoint, raw.observed_at, True, owner_confirmed, tuple(malformed),
    )


def _lookup_unavailable(key: OrderMetadataKey) -> OrderMetadataLookupResult:
    return OrderMetadataLookupResult(key, None, OrderMetadataSourceStatus("unavailable", "order_by_id", reason="order_metadata_unavailable", error_code="unavailable"))


class OrderFillMetadataCorrelator:
    """Correlate one chain fill with explicit taker/maker order metadata."""

    def correlate(
        self,
        fill: NormalizedOrderFill,
        taker: OrderMetadataLookupResult,
        maker: OrderMetadataLookupResult,
        *,
        expected_account: str | None = None,
        symbol: str | None = None,
    ) -> OrderFillMetadataCorrelation:
        taker_meta, maker_meta = taker.metadata, maker.metadata
        if taker.source_status.status == "conflicting" or maker.source_status.status == "conflicting":
            return OrderFillMetadataCorrelation(fill.fill_id, taker, maker, "conflicting", None, None, None, None, None, None, "conflicting_order_metadata")
        if taker_meta is None and maker_meta is None:
            return OrderFillMetadataCorrelation(fill.fill_id, taker, maker, "unavailable", None, None, None, None, None, None, "order_metadata_unavailable")
        malformed = any(meta is not None and meta.malformed for meta in (taker_meta, maker_meta))
        if malformed:
            return OrderFillMetadataCorrelation(fill.fill_id, taker, maker, "malformed", None, None, None, False, False, False, "malformed_order_metadata")
        metas = [meta for meta in (taker_meta, maker_meta) if meta is not None]
        market_match = all(meta.symbol == symbol for meta in metas) if symbol else True
        if not market_match:
            return OrderFillMetadataCorrelation(fill.fill_id, taker, maker, "partial_match", None, None, False, None, None, None, "market_mismatch")
        account = _address(expected_account)
        owners = [meta.owner for meta in metas if meta.owner]
        owner_confirmed = all(meta.owner_field_confirmed for meta in metas if meta.owner)
        if account is None:
            owner_match = None
        elif not owners or not owner_confirmed:
            owner_match = None
        else:
            owner_match = account in owners
        sides = [meta.side for meta in metas if meta.side]
        side_match = None if fill.side is None or not sides else all(side == fill.side for side in sides)
        quantity_valid = all(meta.quantity is None or fill.quantity <= meta.quantity for meta in metas)
        price_valid = None
        if maker_meta and maker_meta.price is not None:
            price_valid = maker_meta.price == fill.price
        elif taker_meta and taker_meta.price is not None:
            price_valid = taker_meta.price == fill.price
        remaining_consistent = all(meta.quantity is None or meta.remaining_quantity is None or meta.filled_quantity is None or meta.quantity == meta.remaining_quantity + meta.filled_quantity for meta in metas)
        checks = (market_match, quantity_valid, price_valid, remaining_consistent)
        if owner_match is False or any(value is False for value in checks):
            return OrderFillMetadataCorrelation(fill.fill_id, taker, maker, "partial_match", owner_match, side_match, market_match, quantity_valid, price_valid, remaining_consistent, "metadata_validation_failed")
        if owner_match is True and all(value is not None and value is True for value in (market_match, quantity_valid, price_valid, remaining_consistent)):
            return OrderFillMetadataCorrelation(fill.fill_id, taker, maker, "matched", owner_match, side_match, market_match, quantity_valid, price_valid, remaining_consistent)
        return OrderFillMetadataCorrelation(fill.fill_id, taker, maker, "partial_match", owner_match, side_match, market_match, quantity_valid, price_valid, remaining_consistent, "metadata_incomplete")


class OrderMetadataResolver:
    def __init__(self, transport: OrderMetadataReadOnlyTransport | None = None, *, symbol: str, expected_account: str | None = None) -> None:
        self.transport = transport or UnconfiguredOrderMetadataTransport()
        self.symbol = symbol
        self.expected_account = _address(expected_account)
        self.correlator = OrderFillMetadataCorrelator()

    def lookup(self, order_id: int | str) -> OrderMetadataLookupResult:
        key = OrderMetadataKey(self.symbol, str(order_id))
        return self.transport.fetch_order_by_id(self.symbol, str(order_id))

    def resolve_fill(self, fill: NormalizedOrderFill) -> OrderFillMetadataCorrelation:
        return self.correlator.correlate(fill, self.lookup(fill.taker_order_id), self.lookup(fill.maker_order_id), expected_account=self.expected_account, symbol=self.symbol)

    def resolve_fills(self, fills: Sequence[NormalizedOrderFill]) -> OrderMetadataResolverReport:
        if not fills:
            return OrderMetadataResolverReport("unavailable", reason="no_fills")
        correlations = tuple(self.resolve_fill(fill) for fill in fills)
        counts = {status: sum(1 for item in correlations if item.status == status) for status in ("matched", "partial_match", "unavailable", "conflicting", "malformed")}
        owner_matches = sum(1 for item in correlations if item.owner_match is True)
        account_match = True if correlations and all(item.owner_match is True for item in correlations) else False if any(item.owner_match is False for item in correlations) else None
        status = "matched" if counts["matched"] == len(correlations) else "conflicting" if counts["conflicting"] else "malformed" if counts["malformed"] else "partial_match" if counts["partial_match"] else "unavailable"
        return OrderMetadataResolverReport(status, resolved_count=counts["matched"] + counts["partial_match"], conflict_count=counts["conflicting"], malformed_count=counts["malformed"], account_match=account_match, owner_match_count=owner_matches, correlations=correlations, reason=None if status == "matched" else "order_metadata_not_fully_matched")


__all__ = [
    "ORDER_ENDPOINT_DESCRIPTORS", "CONFIRMED_ORDER_ALIASES", "CONFIRMED_STATUS_ALIASES",
    "OrderMetadataKey", "RawOrderMetadata", "NormalizedOrderMetadata", "OrderMetadataSourceStatus",
    "OrderMetadataLookupResult", "OrderMetadataResolverReport", "OrderFillMetadataCorrelation",
    "OrderMetadataReadOnlyTransport", "UnconfiguredOrderMetadataTransport", "FixtureOrderMetadataTransport",
    "normalize_order_metadata", "OrderFillMetadataCorrelator", "OrderMetadataResolver",
]
