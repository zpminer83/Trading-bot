"""Offline models for the documented DreamDEX authenticated account flow.

This module deliberately contains data structures and fixture/unconfigured
read-only sources only.  It does not sign SIWE messages, create JWTs, send
HTTP requests, or expose mutation endpoints.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Generic, Mapping, Protocol, Sequence, TypeVar


def _utc(value: Any = None) -> datetime:
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, (int, float)):
        number = float(value) / (1000 if value > 10_000_000_000 else 1)
        result = datetime.fromtimestamp(number, tz=timezone.utc)
    elif value:
        try:
            result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            result = datetime.now(timezone.utc)
    else:
        result = datetime.now(timezone.utc)
    return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result.astimezone(timezone.utc)


def _decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _mask(value: Any) -> str:
    if not value:
        return "<missing>"
    text = str(value)
    if text.startswith("<") and text.endswith(">"):
        return text
    return "***" if len(text) <= 8 else f"{text[:4]}...{text[-4:]}"


@dataclass(frozen=True)
class AuthEndpointDescriptor:
    name: str
    method: str
    path: str
    authenticated: bool
    required_query_params: tuple[str, ...] = ()
    required_body_fields: tuple[str, ...] = ()
    response_shape: str = "unknown"
    pagination: str = "not_applicable"
    confirmation: str = "confirmed"
    read_only: bool = True
    mutation: bool = False
    source_file: str = ""
    source_lines: str = ""


AUTH_ENDPOINT_DESCRIPTORS: tuple[AuthEndpointDescriptor, ...] = (
    AuthEndpointDescriptor(
        "auth_nonce", "GET", "/auth/nonce", False,
        response_shape="{nonce, message?}", pagination="not_applicable",
        confirmation="confirmed", read_only=True,
        source_file="packages/core/src/rest.ts", source_lines="98-103",
    ),
    AuthEndpointDescriptor(
        "auth_login", "POST", "/auth/login", False,
        required_body_fields=("message", "signature"),
        response_shape="{token|jwt, expiresAt}", pagination="not_applicable",
        confirmation="confirmed_authentication", read_only=False,
        source_file="packages/core/src/rest.ts", source_lines="116-125",
    ),
    AuthEndpointDescriptor(
        "account_vault_balances", "GET", "/markets/{symbol}/vault/balance", True,
        required_query_params=("walletAddress",),
        response_shape="{balances:[{currency, amount}]}", pagination="unknown",
        confirmation="confirmed_implementation", read_only=True,
        source_file="examples/05-production-async/src/dreamdex_bot/core/rest_client.py", source_lines="298-330",
    ),
    AuthEndpointDescriptor(
        "open_orders_by_market", "GET", "/markets/{symbol}/orders", True,
        required_query_params=("status",), response_shape="orders[] or {orders:[]}",
        pagination="undocumented", confirmation="hypothetical", read_only=True,
        source_file="examples/05-production-async/src/dreamdex_bot/core/rest_client.py", source_lines="280-297",
    ),
    AuthEndpointDescriptor(
        "order_by_id", "GET", "/markets/{symbol}/orders/{orderId}", True,
        response_shape="order", pagination="not_applicable", confirmation="confirmed_implementation",
        read_only=True, source_file="packages/core/src/rest.ts", source_lines="94-96",
    ),
    AuthEndpointDescriptor(
        "historical_orders", "GET", "/markets/{symbol}/orders", True,
        response_shape="orders[] or {orders:[]}", pagination="undocumented",
        confirmation="hypothetical", read_only=True,
        source_file="examples/05-production-async/src/dreamdex_bot/core/rest_client.py", source_lines="280-297",
    ),
    AuthEndpointDescriptor(
        "market_trades_feed", "GET", "/markets/{symbol}/trades", False,
        required_query_params=("limit",), response_shape="trade[]",
        pagination="unknown", confirmation="confirmed_public_not_account_fills", read_only=True,
        source_file="examples/04-python-ops/backend/trading/dreamdex.py", source_lines="195-203",
    ),
    AuthEndpointDescriptor(
        "account_fills", "GET", "/markets/{symbol}/trades", False,
        required_query_params=("walletAddress",), response_shape="unknown",
        pagination="unknown", confirmation="hypothetical_account_filter", read_only=True,
        source_file="docs/architecture.md", source_lines="63-66",
    ),
    AuthEndpointDescriptor(
        "account_commissions", "GET", "/commissions", True,
        response_shape="unknown", pagination="unknown", confirmation="hypothetical",
        read_only=True, source_file="docs", source_lines="not found",
    ),
)


@dataclass(frozen=True)
class AuthNonceRequest:
    endpoint: str = "/auth/nonce"
    method: str = "GET"


@dataclass(frozen=True)
class AuthNonceResponse:
    nonce: str | None
    message_template: str | None = None
    observed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    source_status: str = "available"


@dataclass(frozen=True)
class SiweLoginRequest:
    message: str
    signature: str
    endpoint: str = "/auth/login"
    method: str = "POST"


@dataclass(frozen=True)
class SiweLoginResponse:
    token_present: bool
    expires_at: datetime | None
    observed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    source_status: str = "available"


@dataclass(frozen=True)
class AuthTokenMetadata:
    token_present: bool = False
    expires_at: datetime | None = None
    authorization_scheme: str = "Bearer"
    source_status: str = "unconfigured"


@dataclass(frozen=True)
class AuthenticatedRequestDescriptor:
    endpoint: AuthEndpointDescriptor
    account_identifier: str
    query_params: tuple[str, ...] = ()
    body_fields: tuple[str, ...] = ()
    authorization_header_present: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "account_identifier", _mask(self.account_identifier))


@dataclass(frozen=True)
class AuthenticatedSourceStatus:
    status: str
    endpoint_name: str
    observed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    raw_status_name: str | None = None
    reason: str | None = None
    error_code: str | None = None
    pagination_complete: bool = False
    duplicate_count: int = 0
    malformed_count: int = 0

    @property
    def available(self) -> bool:
        return self.status == "available"

    def is_fresh(self, *, now: datetime | None = None, max_age_seconds: Decimal = Decimal("30")) -> bool:
        current = _utc(now)
        age = max(Decimal("0"), Decimal(str((current - self.observed_at).total_seconds())))
        return self.available and age <= max_age_seconds


@dataclass(frozen=True)
class AuthenticatedBalanceSnapshot:
    asset: str
    total: Decimal | None
    available: Decimal | None
    locked: Decimal | None
    account_identifier: str
    source_status: AuthenticatedSourceStatus

    def __post_init__(self) -> None:
        object.__setattr__(self, "account_identifier", _mask(self.account_identifier))


@dataclass(frozen=True)
class AuthenticatedOrderSnapshot:
    order_id: str
    symbol: str
    side: str | None
    price: Decimal | None
    quantity: Decimal | None
    remaining_quantity: Decimal | None
    raw_status_name: str | None
    observed_at: datetime
    account_identifier: str
    source_status: AuthenticatedSourceStatus

    def __post_init__(self) -> None:
        object.__setattr__(self, "account_identifier", _mask(self.account_identifier))


@dataclass(frozen=True)
class AuthenticatedFillSnapshot:
    fill_id: str
    order_id: str | None
    symbol: str
    side: str | None
    price: Decimal | None
    quantity: Decimal | None
    fee: Decimal | None
    observed_at: datetime
    account_identifier: str
    source_status: AuthenticatedSourceStatus

    def __post_init__(self) -> None:
        object.__setattr__(self, "account_identifier", _mask(self.account_identifier))


@dataclass(frozen=True)
class AuthenticatedCommissionSnapshot:
    commission_id: str
    asset: str | None
    amount: Decimal | None
    observed_at: datetime
    account_identifier: str
    source_status: AuthenticatedSourceStatus

    def __post_init__(self) -> None:
        object.__setattr__(self, "account_identifier", _mask(self.account_identifier))


T = TypeVar("T")


@dataclass(frozen=True)
class AuthenticatedSourceCollection(Generic[T]):
    records: tuple[T, ...]
    source_status: AuthenticatedSourceStatus

    @property
    def status(self) -> str:
        return self.source_status.status

    @property
    def pagination_complete(self) -> bool:
        return self.source_status.pagination_complete


def _unavailable_status(endpoint: str, reason: str = "authenticated_transport_unconfigured") -> AuthenticatedSourceStatus:
    return AuthenticatedSourceStatus(
        status="unavailable", endpoint_name=endpoint, reason=reason,
        error_code="authenticated_transport_unconfigured", pagination_complete=False,
    )


@dataclass(frozen=True)
class AuthenticatedAccountSnapshot:
    account_identifier: str
    balances: tuple[AuthenticatedBalanceSnapshot, ...] = ()
    open_orders: tuple[AuthenticatedOrderSnapshot, ...] = ()
    recent_orders: tuple[AuthenticatedOrderSnapshot, ...] = ()
    fills: tuple[AuthenticatedFillSnapshot, ...] = ()
    commissions: tuple[AuthenticatedCommissionSnapshot, ...] = ()
    balances_status: AuthenticatedSourceStatus = field(default_factory=lambda: _unavailable_status("account_vault_balances"))
    open_orders_status: AuthenticatedSourceStatus = field(default_factory=lambda: _unavailable_status("open_orders_by_market"))
    recent_orders_status: AuthenticatedSourceStatus = field(default_factory=lambda: _unavailable_status("historical_orders"))
    fills_status: AuthenticatedSourceStatus = field(default_factory=lambda: _unavailable_status("account_fills"))
    commissions_status: AuthenticatedSourceStatus = field(default_factory=lambda: _unavailable_status("account_commissions"))
    observed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        object.__setattr__(self, "account_identifier", _mask(self.account_identifier))

    @classmethod
    def unavailable(cls, account_identifier: str = "<unresolved>") -> "AuthenticatedAccountSnapshot":
        return cls(account_identifier=_mask(account_identifier))

    @property
    def pagination_complete(self) -> bool:
        statuses = (self.balances_status, self.open_orders_status, self.fills_status)
        return all(status.pagination_complete for status in statuses)

    @property
    def available(self) -> bool:
        return all(status.available for status in (self.balances_status, self.open_orders_status, self.fills_status))

    def authoritative_for(self, expected_account: str | None, *, now: datetime | None = None, max_age_seconds: Decimal = Decimal("30")) -> bool:
        if not expected_account or self.account_identifier != _mask(expected_account):
            return False
        if not self.available or not self.pagination_complete:
            return False
        statuses = (self.balances_status, self.open_orders_status, self.fills_status)
        current = _utc(now)
        records = (*self.balances, *self.open_orders, *self.recent_orders, *self.fills, *self.commissions)
        return all(status.is_fresh(now=current, max_age_seconds=max_age_seconds) for status in statuses) and all(
            record.source_status.status != "malformed" and
            max(Decimal("0"), Decimal(str((current - _utc(getattr(record, "observed_at", record.source_status.observed_at))).total_seconds()))) <= max_age_seconds
            for record in records
        )


class AuthenticatedReadOnlyTransport(Protocol):
    def fetch_account_balances(self, account_identifier: str, markets: Sequence[str] = ()) -> AuthenticatedSourceCollection[AuthenticatedBalanceSnapshot]: ...
    def fetch_open_orders(self, account_identifier: str, market: str) -> AuthenticatedSourceCollection[AuthenticatedOrderSnapshot]: ...
    def fetch_recent_orders(self, account_identifier: str, market: str) -> AuthenticatedSourceCollection[AuthenticatedOrderSnapshot]: ...
    def fetch_fills(self, account_identifier: str, market: str) -> AuthenticatedSourceCollection[AuthenticatedFillSnapshot]: ...
    def fetch_commissions(self, account_identifier: str, market: str) -> AuthenticatedSourceCollection[AuthenticatedCommissionSnapshot]: ...


class UnconfiguredAuthenticatedReadOnlyTransport:
    """Production-safe placeholder: never performs auth or network I/O."""

    def fetch_account_balances(self, account_identifier: str, markets: Sequence[str] = ()) -> AuthenticatedSourceCollection[AuthenticatedBalanceSnapshot]:
        return AuthenticatedSourceCollection((), _unavailable_status("account_vault_balances"))

    def fetch_open_orders(self, account_identifier: str, market: str) -> AuthenticatedSourceCollection[AuthenticatedOrderSnapshot]:
        return AuthenticatedSourceCollection((), _unavailable_status("open_orders_by_market"))

    def fetch_recent_orders(self, account_identifier: str, market: str) -> AuthenticatedSourceCollection[AuthenticatedOrderSnapshot]:
        return AuthenticatedSourceCollection((), _unavailable_status("historical_orders"))

    def fetch_fills(self, account_identifier: str, market: str) -> AuthenticatedSourceCollection[AuthenticatedFillSnapshot]:
        return AuthenticatedSourceCollection((), _unavailable_status("account_fills"))

    def fetch_commissions(self, account_identifier: str, market: str) -> AuthenticatedSourceCollection[AuthenticatedCommissionSnapshot]:
        return AuthenticatedSourceCollection((), _unavailable_status("account_commissions"))


def _section(payload: Any, name: str) -> tuple[Any, Mapping[str, Any]]:
    if not isinstance(payload, Mapping):
        return [], {}
    aliases = {
        "open_orders": "openOrders",
        "recent_orders": "recentOrders",
    }
    value = payload.get(name, payload.get(aliases.get(name, name.replace("_", "")), []))
    if isinstance(value, Mapping):
        return value.get("records", value.get("items", value.get(name, value.get("data", [])))), value
    return value, payload


def _records(value: Any) -> list[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        value = value.get("records", value.get("items", value.get("data", value.get("balances", value.get("orders", value.get("fills", []))))))
    if isinstance(value, list):
        return [row for row in value if isinstance(row, Mapping)]
    return []


class FixtureAuthenticatedReadOnlyTransport:
    """Deterministic parser for explicitly marked ``authenticated_account`` fixtures."""

    def __init__(self, fixture: Mapping[str, Any], *, now: datetime | None = None) -> None:
        self.fixture = fixture
        self.now = now or datetime.now(timezone.utc)

    def _account(self) -> Mapping[str, Any]:
        value = self.fixture.get("authenticated_account", self.fixture.get("auth_account", self.fixture.get("authenticated", {})))
        return value if isinstance(value, Mapping) else {}

    def _collection(self, name: str, endpoint: str, account_identifier: str) -> tuple[list[Mapping[str, Any]], AuthenticatedSourceStatus]:
        value, wrapper = _section(self._account(), name)
        rows = _records(value)
        observed = _utc(wrapper.get("observed_at", self._account().get("observed_at", self.now)))
        raw_status = str(wrapper.get("status", "available" if name in self._account() else "unavailable"))
        status = raw_status.lower()
        if status in {"ok", "confirmed", "complete"}:
            status = "available"
        pagination = bool(wrapper.get("pagination_complete", wrapper.get("complete", status == "available")))
        error_code = wrapper.get("error_code")
        reason = wrapper.get("reason")
        if status != "available" and not error_code:
            error_code = "authenticated_source_unavailable"
        source = AuthenticatedSourceStatus(status, endpoint, observed, raw_status, reason, error_code, pagination)
        return rows, source

    def fetch_account_balances(self, account_identifier: str, markets: Sequence[str] = ()) -> AuthenticatedSourceCollection[AuthenticatedBalanceSnapshot]:
        rows, source = self._collection("balances", "account_vault_balances", account_identifier)
        masked = _mask(account_identifier)
        out: list[AuthenticatedBalanceSnapshot] = []
        malformed = False
        for row in rows:
            asset = str(row.get("asset", row.get("currency", row.get("symbol", ""))))
            total = _decimal(row.get("total", row.get("amount", row.get("balance"))))
            available = _decimal(row.get("available", total))
            locked = _decimal(row.get("locked"))
            if not asset or total is None:
                malformed = True
                continue
            out.append(AuthenticatedBalanceSnapshot(asset, total, available, locked, masked, source))
        if malformed:
            source = AuthenticatedSourceStatus("malformed", source.endpoint_name, source.observed_at, source.raw_status_name, "malformed_balance", "malformed_record", source.pagination_complete, source.duplicate_count)
        return AuthenticatedSourceCollection(tuple(out), source)

    def fetch_open_orders(self, account_identifier: str, market: str) -> AuthenticatedSourceCollection[AuthenticatedOrderSnapshot]:
        return self._orders(account_identifier, market, "open_orders", "open_orders_by_market")

    def fetch_recent_orders(self, account_identifier: str, market: str) -> AuthenticatedSourceCollection[AuthenticatedOrderSnapshot]:
        return self._orders(account_identifier, market, "recent_orders", "historical_orders")

    def _orders(self, account_identifier: str, market: str, name: str, endpoint: str) -> AuthenticatedSourceCollection[AuthenticatedOrderSnapshot]:
        rows, source = self._collection(name, endpoint, account_identifier)
        masked = _mask(account_identifier)
        out: list[AuthenticatedOrderSnapshot] = []
        malformed = False
        for row in rows:
            order_id = row.get("orderId", row.get("id"))
            symbol = str(row.get("symbol", row.get("market", market)))
            if order_id is None or not symbol:
                malformed = True
                continue
            out.append(AuthenticatedOrderSnapshot(
                str(order_id), symbol, row.get("side"), _decimal(row.get("price")),
                _decimal(row.get("quantity", row.get("amount"))),
                _decimal(row.get("remainingQuantity", row.get("remaining"))),
                str(row.get("status")) if row.get("status") is not None else None,
                _utc(row.get("timestamp", row.get("createdAt", source.observed_at))), masked, source,
            ))
        if malformed:
            source = AuthenticatedSourceStatus("malformed", source.endpoint_name, source.observed_at, source.raw_status_name, "malformed_order", "malformed_record", source.pagination_complete, source.duplicate_count)
        return AuthenticatedSourceCollection(tuple(out), source)

    def fetch_fills(self, account_identifier: str, market: str) -> AuthenticatedSourceCollection[AuthenticatedFillSnapshot]:
        rows, source = self._collection("fills", "account_fills", account_identifier)
        masked = _mask(account_identifier)
        out: list[AuthenticatedFillSnapshot] = []
        seen: set[str] = set()
        duplicates = 0
        for row in rows:
            fill_id = row.get("fillId", row.get("tradeId", row.get("id")))
            if fill_id is None:
                continue
            fill_id = str(fill_id)
            if fill_id in seen:
                duplicates += 1
                continue
            seen.add(fill_id)
            out.append(AuthenticatedFillSnapshot(
                fill_id, str(row.get("orderId")) if row.get("orderId") is not None else None,
                str(row.get("symbol", row.get("market", market))), row.get("side"),
                _decimal(row.get("price", row.get("fillPrice"))), _decimal(row.get("quantity", row.get("amount"))),
                _decimal(row.get("fee", row.get("commission"))),
                _utc(row.get("timestamp", row.get("filledAt", source.observed_at))), masked, source,
            ))
        if duplicates:
            source = AuthenticatedSourceStatus(source.status, source.endpoint_name, source.observed_at, source.raw_status_name, source.reason, source.error_code, source.pagination_complete, duplicates)
            out = [AuthenticatedFillSnapshot(r.fill_id, r.order_id, r.symbol, r.side, r.price, r.quantity, r.fee, r.observed_at, r.account_identifier, source) for r in out]
        return AuthenticatedSourceCollection(tuple(out), source)

    def fetch_commissions(self, account_identifier: str, market: str) -> AuthenticatedSourceCollection[AuthenticatedCommissionSnapshot]:
        rows, source = self._collection("commissions", "account_commissions", account_identifier)
        masked = _mask(account_identifier)
        out: list[AuthenticatedCommissionSnapshot] = []
        for row in rows:
            commission_id = row.get("commissionId", row.get("id"))
            if commission_id is None:
                continue
            out.append(AuthenticatedCommissionSnapshot(
                str(commission_id), row.get("asset", row.get("currency")),
                _decimal(row.get("amount", row.get("fee"))),
                _utc(row.get("timestamp", source.observed_at)), masked, source,
            ))
        return AuthenticatedSourceCollection(tuple(out), source)

    def fetch_account_snapshot(self, account_identifier: str, market: str) -> AuthenticatedAccountSnapshot:
        balances = self.fetch_account_balances(account_identifier, (market,))
        open_orders = self.fetch_open_orders(account_identifier, market)
        recent_orders = self.fetch_recent_orders(account_identifier, market)
        fills = self.fetch_fills(account_identifier, market)
        commissions = self.fetch_commissions(account_identifier, market)
        observed = max((s.source_status.observed_at for s in (balances, open_orders, recent_orders, fills, commissions)), default=self.now)
        return AuthenticatedAccountSnapshot(
            _mask(account_identifier), balances.records, open_orders.records, recent_orders.records,
            fills.records, commissions.records, balances.source_status, open_orders.source_status,
            recent_orders.source_status, fills.source_status, commissions.source_status, observed,
        )


__all__ = [
    "AUTH_ENDPOINT_DESCRIPTORS", "AuthEndpointDescriptor", "AuthNonceRequest", "AuthNonceResponse",
    "SiweLoginRequest", "SiweLoginResponse", "AuthTokenMetadata", "AuthenticatedRequestDescriptor",
    "AuthenticatedSourceStatus", "AuthenticatedBalanceSnapshot", "AuthenticatedOrderSnapshot",
    "AuthenticatedFillSnapshot", "AuthenticatedCommissionSnapshot", "AuthenticatedSourceCollection",
    "AuthenticatedAccountSnapshot", "AuthenticatedReadOnlyTransport", "UnconfiguredAuthenticatedReadOnlyTransport",
    "FixtureAuthenticatedReadOnlyTransport",
]
