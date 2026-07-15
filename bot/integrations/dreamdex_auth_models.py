"""Offline models for the documented DreamDEX authenticated account flow.

This module deliberately contains data structures and fixture/unconfigured
read-only sources only.  It does not sign SIWE messages, create JWTs, send
HTTP requests, or expose mutation endpoints.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
import hashlib
import threading
from typing import Any, Callable, Generic, Mapping, Protocol, Sequence, TypeVar


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
    response_body_status: str = "unknown"
    schema_status: str = "unknown"
    records_status: str = "unknown"
    pagination_status: str = "unresolved"
    authority_status: str = "non_authoritative"

    @property
    def available(self) -> bool:
        return self.status in {"available", "valid_confirmed_schema", "available_empty"}

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


# ---------------------------------------------------------------------------
# Offline SIWE authentication models and state machine
# ---------------------------------------------------------------------------

AUTH_SCHEMA_AUDIT: tuple[AuthEndpointDescriptor, ...] = (
    AuthEndpointDescriptor(
        "auth_nonce", "GET", "/auth/nonce", False,
        response_shape="{nonce:string, message?:string}",
        confirmation="confirmed_core_ts;conflicting_async_python_post",
        source_file="packages/core/src/rest.ts;examples/05-production-async/src/dreamdex_bot/core/rest_client.py",
        source_lines="102;96-104",
    ),
    AuthEndpointDescriptor(
        "auth_login", "POST", "/auth/login", False,
        required_body_fields=("message", "signature"),
        response_shape="{token:string, expiresAt:number}",
        confirmation="confirmed_core_ts",
        source_file="packages/core/src/rest.ts",
        source_lines="119-125",
    ),
)


def _normalize_auth_address(value: Any, field_name: str = "address") -> str:
    if not isinstance(value, str):
        raise ValueError(f"invalid {field_name}")
    text = value.strip()
    clean = text.lower().removeprefix("0x")
    if len(clean) != 40 or any(char not in "0123456789abcdef" for char in clean):
        raise ValueError(f"invalid {field_name}")
    return "0x" + clean


def _required_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"invalid {field_name}")
    return value


def _format_siwe_time(value: datetime) -> str:
    normalized = _utc(value)
    return normalized.isoformat(timespec="milliseconds").replace("+00:00", "Z")


class AuthIdentityStatus(str, Enum):
    confirmed = "confirmed"
    unavailable = "unavailable"
    unresolved = "unresolved"
    conflicting = "conflicting"
    malformed = "malformed"


class DreamDexAuthState(str, Enum):
    unconfigured = "unconfigured"
    nonce_required = "nonce_required"
    nonce_available = "nonce_available"
    message_built = "message_built"
    signature_available = "signature_available"
    login_pending = "login_pending"
    authenticated = "authenticated"
    refresh_required = "refresh_required"
    expired = "expired"
    unauthorized = "unauthorized"
    failed_closed = "failed_closed"


AuthState = DreamDexAuthState


@dataclass(frozen=True, repr=False)
class DreamDexNonceResponse:
    nonce: str | None
    message: str | None = None
    source_status: str = "confirmed"
    error_code: str | None = None
    observed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        if self.nonce is not None and (not isinstance(self.nonce, str) or not self.nonce.strip()):
            raise ValueError("malformed nonce")
        if self.message is not None and not isinstance(self.message, str):
            raise ValueError("malformed nonce message")
        object.__setattr__(self, "observed_at", _utc(self.observed_at))

    @classmethod
    def from_payload(cls, payload: Any, *, observed_at: datetime | None = None) -> "DreamDexNonceResponse":
        if not isinstance(payload, Mapping):
            return cls(None, source_status="malformed", error_code="malformed_nonce_response", observed_at=observed_at or datetime.now(timezone.utc))
        nonce = payload.get("nonce")
        if not isinstance(nonce, str) or not nonce.strip():
            return cls(None, source_status="malformed", error_code="missing_nonce", observed_at=observed_at or datetime.now(timezone.utc))
        message = payload.get("message")
        if message is not None and not isinstance(message, str):
            return cls(None, source_status="malformed", error_code="malformed_nonce_message", observed_at=observed_at or datetime.now(timezone.utc))
        return cls(nonce, message, observed_at=observed_at or datetime.now(timezone.utc))

    def __repr__(self) -> str:
        return f"DreamDexNonceResponse(nonce_present={self.nonce is not None}, message_present={self.message is not None}, source_status={self.source_status!r}, error_code={self.error_code!r})"


@dataclass(frozen=True, repr=False)
class DreamDexSiweMessage:
    address: str
    domain: str
    uri: str
    chain_id: int
    nonce: str
    issued_at: datetime
    statement: str = "Sign in to dreamDEX"
    expiration_time: datetime | None = None
    message: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "address", _normalize_auth_address(self.address))
        object.__setattr__(self, "domain", _required_text(self.domain, "domain"))
        object.__setattr__(self, "uri", _required_text(self.uri, "uri"))
        object.__setattr__(self, "nonce", _required_text(self.nonce, "nonce"))
        if isinstance(self.chain_id, bool) or not isinstance(self.chain_id, int) or self.chain_id <= 0:
            raise ValueError("invalid chain_id")
        if not isinstance(self.statement, str) or not self.statement:
            raise ValueError("invalid statement")
        issued = _utc(self.issued_at)
        expiration = _utc(self.expiration_time) if self.expiration_time is not None else None
        if expiration is not None and expiration <= issued:
            raise ValueError("expiration_time must be after issued_at")
        object.__setattr__(self, "issued_at", issued)
        object.__setattr__(self, "expiration_time", expiration)
        lines = [
            f"{self.domain} wants you to sign in with your Ethereum account:",
            self.address,
            "",
            self.statement,
            "",
            f"URI: {self.uri}",
            "Version: 1",
            f"Chain ID: {self.chain_id}",
            f"Nonce: {self.nonce}",
            f"Issued At: {_format_siwe_time(issued)}",
        ]
        if expiration is not None:
            lines.append(f"Expiration Time: {_format_siwe_time(expiration)}")
        object.__setattr__(self, "message", "\n".join(lines))

    def __repr__(self) -> str:
        return f"DreamDexSiweMessage(address={_mask(self.address)!r}, domain={self.domain!r}, uri={self.uri!r}, chain_id={self.chain_id}, nonce_present=True, issued_at={self.issued_at.isoformat()!r})"


@dataclass(frozen=True, repr=False)
class DreamDexLoginRequest:
    message: str
    signature: str
    endpoint: str = "/auth/login"
    method: str = "POST"

    def __post_init__(self) -> None:
        _required_text(self.message, "message")
        _required_text(self.signature, "signature")

    def __repr__(self) -> str:
        return "DreamDexLoginRequest(message=<redacted>, signature=<redacted>, endpoint='/auth/login', method='POST')"


def _parse_expires_at(value: Any) -> datetime | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not number.is_finite() or number <= 0:
        return None
    # Bot Kit uses Date.now()-compatible milliseconds.
    if number < Decimal("100000000000"):
        return None
    return datetime.fromtimestamp(float(number / Decimal("1000")), tz=timezone.utc)


@dataclass(frozen=True, repr=False)
class DreamDexLoginResponse:
    token: str | None
    expires_at: datetime | None
    source_status: str = "confirmed"
    error_code: str | None = None
    observed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        if self.token is not None and (not isinstance(self.token, str) or not self.token.strip()):
            raise ValueError("malformed token")
        object.__setattr__(self, "expires_at", _utc(self.expires_at) if self.expires_at is not None else None)
        object.__setattr__(self, "observed_at", _utc(self.observed_at))

    @classmethod
    def from_payload(cls, payload: Any, *, observed_at: datetime | None = None) -> "DreamDexLoginResponse":
        observed = observed_at or datetime.now(timezone.utc)
        if not isinstance(payload, Mapping):
            return cls(None, None, source_status="malformed", error_code="malformed_login_response", observed_at=observed)
        token = payload.get("token")
        expires_at = _parse_expires_at(payload.get("expiresAt"))
        if not isinstance(token, str) or not token.strip():
            return cls(None, expires_at, source_status="malformed", error_code="missing_token", observed_at=observed)
        if expires_at is None:
            return cls(None, None, source_status="malformed", error_code="malformed_expiresAt", observed_at=observed)
        return cls(token, expires_at, observed_at=observed)

    def __repr__(self) -> str:
        return f"DreamDexLoginResponse(token_present={self.token is not None}, expires_at={self.expires_at.isoformat() if self.expires_at else None!r}, source_status={self.source_status!r}, error_code={self.error_code!r})"


@dataclass(frozen=True, repr=False)
class DreamDexTokenState:
    token: str | None = None
    expires_at: datetime | None = None
    refresh_skew_seconds: Decimal = Decimal("60")
    source_status: str = "unconfigured"

    def __post_init__(self) -> None:
        if self.token is not None and (not isinstance(self.token, str) or not self.token.strip()):
            raise ValueError("empty token")
        if self.expires_at is not None:
            object.__setattr__(self, "expires_at", _utc(self.expires_at))
        if self.refresh_skew_seconds < 0:
            raise ValueError("refresh_skew_seconds must be >= 0")

    @property
    def token_present(self) -> bool:
        return self.token is not None

    def seconds_until_expiry(self, now: datetime | None = None) -> Decimal | None:
        if self.expires_at is None:
            return None
        current = _utc(now)
        return Decimal(str((self.expires_at - current).total_seconds()))

    def expiry_status(self, now: datetime | None = None) -> str:
        remaining = self.seconds_until_expiry(now)
        if not self.token_present:
            return "unavailable"
        if remaining is None:
            return "unavailable"
        return "expired" if remaining <= 0 else ("refresh_required" if remaining <= self.refresh_skew_seconds else "valid")

    def refresh_required(self, now: datetime | None = None) -> bool:
        return self.expiry_status(now) in {"expired", "refresh_required"}

    def usable(self, now: datetime | None = None) -> bool:
        return self.token_present and self.expiry_status(now) == "valid" and self.source_status == "confirmed"

    def __repr__(self) -> str:
        return f"DreamDexTokenState(token_present={self.token_present}, expiry_status={self.expiry_status()}, expires_at={self.expires_at.isoformat() if self.expires_at else None!r}, source_status={self.source_status!r})"


@dataclass(frozen=True, repr=False)
class DreamDexAuthIdentity:
    login_address: str | None = None
    owner_address: str | None = None
    trading_address: str | None = None
    operator_address: str | None = None
    signer_address: str | None = None
    authenticated_subject: str | None = None
    vault_query_address: str | None = None
    order_owner_address: str | None = None
    identity_status: str = AuthIdentityStatus.unresolved.value
    owner_match_status: str = AuthIdentityStatus.unresolved.value
    trading_match_status: str = AuthIdentityStatus.unresolved.value
    operator_match_status: str = AuthIdentityStatus.unresolved.value
    address_semantics_status: str = AuthIdentityStatus.unresolved.value
    authoritative: bool = False
    unresolved_reasons: tuple[str, ...] = ("identity_not_explicitly_confirmed",)

    def __post_init__(self) -> None:
        for name in ("login_address", "owner_address", "trading_address", "operator_address", "signer_address", "authenticated_subject", "vault_query_address", "order_owner_address"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _normalize_auth_address(value, name))
        allowed = {item.value for item in AuthIdentityStatus}
        for name in ("identity_status", "owner_match_status", "trading_match_status", "operator_match_status", "address_semantics_status"):
            if getattr(self, name) not in allowed:
                raise ValueError(f"invalid {name}")
        if self.authoritative and (self.identity_status != "confirmed" or self.address_semantics_status != "confirmed"):
            raise ValueError("authoritative identity requires confirmed semantics")

    @classmethod
    def unresolved(cls, login_address: str | None = None, *, reasons: Sequence[str] = ()) -> "DreamDexAuthIdentity":
        return cls(login_address=login_address, unresolved_reasons=tuple(reasons) or ("identity_not_explicitly_confirmed",))

    def __repr__(self) -> str:
        return f"DreamDexAuthIdentity(login_address={_mask(self.login_address)!r}, owner_address={_mask(self.owner_address)!r}, trading_address={_mask(self.trading_address)!r}, operator_address={_mask(self.operator_address)!r}, identity_status={self.identity_status!r}, authoritative={self.authoritative})"


@dataclass(frozen=True, repr=False)
class DreamDexAuthAttempt:
    state: str
    started_at: datetime
    completed_at: datetime | None = None
    http_status: int | None = None
    error_code: str | None = None
    nonce: str | None = None
    message: str | None = None
    signature: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "started_at", _utc(self.started_at))
        object.__setattr__(self, "completed_at", _utc(self.completed_at) if self.completed_at else None)

    def __repr__(self) -> str:
        return f"DreamDexAuthAttempt(state={self.state!r}, http_status={self.http_status!r}, error_code={self.error_code!r}, nonce_present={self.nonce is not None}, message_present={self.message is not None}, signature_present={self.signature is not None})"


@dataclass(frozen=True, repr=False)
class DreamDexAuthSnapshot:
    state: str
    identity: DreamDexAuthIdentity
    token_state: DreamDexTokenState
    nonce_response: DreamDexNonceResponse | None = None
    siwe_message: DreamDexSiweMessage | None = None
    last_attempt: DreamDexAuthAttempt | None = None
    manager_configured: bool = False
    signer_configured: bool = False
    transport_configured: bool = False
    unresolved_reasons: tuple[str, ...] = ()
    observed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        object.__setattr__(self, "observed_at", _utc(self.observed_at))

    @property
    def token_present(self) -> bool:
        return self.token_state.token_present

    @property
    def identity_authoritative(self) -> bool:
        return self.identity.authoritative

    def safe_dict(self) -> dict[str, Any]:
        return {
            "state": self.state, "manager_configured": self.manager_configured,
            "signer_configured": self.signer_configured, "transport_configured": self.transport_configured,
            "token_present": self.token_present, "expiry_status": self.token_state.expiry_status(self.observed_at),
            "refresh_required": self.token_state.refresh_required(self.observed_at),
            "authenticated_subject": _mask(self.identity.authenticated_subject),
            "identity_authoritative": self.identity_authoritative,
            "owner_match": self.identity.owner_match_status,
            "trading_match": self.identity.trading_match_status,
            "operator_match": self.identity.operator_match_status,
            "address_semantics": self.identity.address_semantics_status,
            "unresolved_reasons": self.unresolved_reasons or self.identity.unresolved_reasons,
        }

    def __repr__(self) -> str:
        return f"DreamDexAuthSnapshot(state={self.state!r}, token_present={self.token_present}, identity_authoritative={self.identity_authoritative}, manager_configured={self.manager_configured}, signer_configured={self.signer_configured}, transport_configured={self.transport_configured})"


class DreamDexMessageSigner(Protocol):
    address: str
    chain_id: int

    def sign_message(self, message: str) -> str: ...


class RejectingUnconfiguredSigner:
    configured = False
    address = "<unresolved>"
    chain_id = 0

    def sign_message(self, message: str) -> str:
        raise RuntimeError("unconfigured_signer")


class FixtureMessageSigner:
    configured = True

    def __init__(self, address: str, *, chain_id: int = 5031) -> None:
        self.address = _normalize_auth_address(address)
        self.chain_id = chain_id

    def sign_message(self, message: str) -> str:
        if not isinstance(message, str) or not message:
            raise ValueError("message is required")
        # Deliberately non-cryptographic fixture output. It is never accepted
        # by a production transport and contains no private key material.
        return "0xfixture" + hashlib.sha256(message.encode("utf-8")).hexdigest()

    def __repr__(self) -> str:
        return f"FixtureMessageSigner(address={_mask(self.address)!r}, configured=True)"


@dataclass(frozen=True)
class AuthTransportResponse:
    status: int
    payload: Any = None

    def __repr__(self) -> str:
        # Auth responses can contain bearer material in fixtures; never make
        # the payload part of an accidental log/repr output.
        return f"AuthTransportResponse(status={self.status!r}, payload_present={self.payload is not None})"


class DreamDexAuthTransport(Protocol):
    configured: bool

    def get_nonce(self, address: str) -> Any: ...
    def login(self, message: str, signature: str) -> Any: ...
    def authenticated_get(self, path: str, *, token: str) -> Any: ...


class UnconfiguredDreamDexAuthTransport:
    configured = False

    def get_nonce(self, address: str) -> Any:
        raise RuntimeError("auth_transport_unconfigured")

    def login(self, message: str, signature: str) -> Any:
        raise RuntimeError("auth_transport_unconfigured")

    def authenticated_get(self, path: str, *, token: str) -> Any:
        raise RuntimeError("auth_transport_unconfigured")


class FixtureDreamDexAuthTransport:
    configured = True

    def __init__(self, fixture: Mapping[str, Any], *, now: datetime | None = None) -> None:
        self.fixture = fixture
        self.now = now or datetime.now(timezone.utc)
        self.calls: list[tuple[str, str]] = []
        self.nonce_calls = 0
        self.login_calls = 0
        self.authenticated_get_calls = 0

    def __repr__(self) -> str:
        return (
            "FixtureDreamDexAuthTransport(configured=True, "
            f"nonce_calls={self.nonce_calls}, login_calls={self.login_calls}, "
            f"authenticated_get_calls={self.authenticated_get_calls})"
        )

    def get_nonce(self, address: str) -> DreamDexNonceResponse:
        self.nonce_calls += 1
        self.calls.append(("GET", "/auth/nonce"))
        return DreamDexNonceResponse.from_payload(self.fixture.get("nonce_response", self.fixture.get("nonce", {})), observed_at=self.now)

    def login(self, message: str, signature: str) -> DreamDexLoginResponse:
        self.login_calls += 1
        self.calls.append(("POST", "/auth/login"))
        return DreamDexLoginResponse.from_payload(self.fixture.get("login_response", self.fixture.get("login", {})), observed_at=self.now)

    def authenticated_get(self, path: str, *, token: str) -> AuthTransportResponse:
        self.authenticated_get_calls += 1
        self.calls.append(("GET", path))
        sequence = self.fixture.get("authenticated_get_sequence")
        if isinstance(sequence, list) and sequence:
            item = sequence.pop(0)
        else:
            item = self.fixture.get("authenticated_get", {"status": 200, "payload": {}})
        if isinstance(item, AuthTransportResponse):
            return item
        if isinstance(item, tuple) and len(item) == 2:
            return AuthTransportResponse(int(item[0]), item[1])
        if isinstance(item, Mapping):
            return AuthTransportResponse(int(item.get("status", 200)), item.get("payload", item.get("body")))
        return AuthTransportResponse(200, item)


def _response_parts(value: Any) -> tuple[int, Any]:
    if isinstance(value, AuthTransportResponse):
        return value.status, value.payload
    if isinstance(value, tuple) and len(value) == 2:
        return int(value[0]), value[1]
    if isinstance(value, Mapping) and "status" in value:
        return int(value["status"]), value.get("payload", value.get("body"))
    return 200, value


class DreamDexAuthManager:
    """Offline-first SIWE state machine.

    The manager only calls the supplied transport and signer. The production
    default has neither, so it cannot perform a network login or read secrets.
    """

    def __init__(
        self,
        *,
        signer: DreamDexMessageSigner | None = None,
        transport: DreamDexAuthTransport | None = None,
        domain: str = "api.dreamdex.io",
        uri: str = "https://api.dreamdex.io",
        chain_id: int = 5031,
        refresh_skew_seconds: Decimal = Decimal("60"),
        clock: Callable[[], datetime] | None = None,
        identity: DreamDexAuthIdentity | None = None,
    ) -> None:
        self.signer = signer
        self.transport = transport or UnconfiguredDreamDexAuthTransport()
        self.domain, self.uri, self.chain_id = _required_text(domain, "domain"), _required_text(uri, "uri"), chain_id
        self.refresh_skew_seconds = Decimal(str(refresh_skew_seconds))
        if self.refresh_skew_seconds < 0:
            raise ValueError("refresh_skew_seconds must be >= 0")
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.identity = identity or DreamDexAuthIdentity.unresolved(getattr(signer, "address", None))
        self._state = DreamDexAuthState.unconfigured.value if signer is None or not getattr(self.transport, "configured", False) else DreamDexAuthState.nonce_required.value
        self._token_state = DreamDexTokenState(refresh_skew_seconds=self.refresh_skew_seconds)
        self._nonce: DreamDexNonceResponse | None = None
        self._message: DreamDexSiweMessage | None = None
        self._attempt: DreamDexAuthAttempt | None = None
        self._last_now: datetime | None = None
        self._lock = threading.RLock()

    @property
    def manager_configured(self) -> bool:
        return self.signer is not None and bool(getattr(self.transport, "configured", False))

    def _now(self) -> datetime:
        current = _utc(self.clock())
        if self._last_now is not None and current < self._last_now:
            self._state = DreamDexAuthState.failed_closed.value
            self._token_state = DreamDexTokenState(refresh_skew_seconds=self.refresh_skew_seconds, source_status="failed_closed")
            raise RuntimeError("auth_clock_regression")
        self._last_now = current
        return current

    def snapshot(self) -> DreamDexAuthSnapshot:
        with self._lock:
            if self._last_now is None:
                self._last_now = _utc(self.clock())
            observed = self._last_now
            return DreamDexAuthSnapshot(
                self._state, self.identity, self._token_state, self._nonce, self._message, self._attempt,
                self.manager_configured, self.signer is not None, bool(getattr(self.transport, "configured", False)),
                tuple(self.identity.unresolved_reasons) if not self.identity.authoritative else (), observed,
            )

    def _failed(self, code: str, started: datetime, *, status: int | None = None) -> DreamDexAuthSnapshot:
        self._state = DreamDexAuthState.failed_closed.value
        self._token_state = DreamDexTokenState(refresh_skew_seconds=self.refresh_skew_seconds, source_status="failed_closed")
        self._attempt = DreamDexAuthAttempt(self._state, started, self._now(), status, code)
        return self.snapshot()

    def get_nonce(self) -> DreamDexAuthSnapshot:
        with self._lock:
            started = self._now()
            if not self.manager_configured:
                self._state = DreamDexAuthState.unconfigured.value
                return self.snapshot()
            try:
                response = self.transport.get_nonce(self.signer.address)  # type: ignore[union-attr]
                response = response if isinstance(response, DreamDexNonceResponse) else DreamDexNonceResponse.from_payload(response, observed_at=self._now())
                self._nonce = response
                if response.source_status != "confirmed":
                    return self._failed(response.error_code or "malformed_nonce_response", started)
                self._state = DreamDexAuthState.nonce_available.value
                self._attempt = DreamDexAuthAttempt(self._state, started, self._now())
                return self.snapshot()
            except Exception as exc:
                return self._failed(str(exc).split(":", 1)[0][:80], started)

    def build_message(self) -> DreamDexAuthSnapshot:
        with self._lock:
            started = self._now()
            if self._nonce is None or self._nonce.nonce is None:
                self._state = DreamDexAuthState.failed_closed.value
                return self._failed("nonce_required", started)
            try:
                self._message = DreamDexSiweMessage(self.signer.address, self.domain, self.uri, self.chain_id, self._nonce.nonce, started)  # type: ignore[union-attr]
                self._state = DreamDexAuthState.message_built.value
                self._attempt = DreamDexAuthAttempt(self._state, started, self._now(), nonce=self._nonce.nonce, message=self._message.message)
                return self.snapshot()
            except Exception as exc:
                return self._failed(str(exc).split(":", 1)[0][:80], started)

    def authenticate(self, *, force: bool = False) -> DreamDexAuthSnapshot:
        with self._lock:
            now = self._now()
            if not force and self._token_state.usable(now):
                self._state = DreamDexAuthState.authenticated.value
                return self.snapshot()
            if not self.manager_configured:
                self._state = DreamDexAuthState.unconfigured.value
                return self.snapshot()
            if self._token_state.token_present and self._token_state.refresh_required(now):
                self._state = DreamDexAuthState.refresh_required.value
                self._nonce = None
                self._message = None
            if force:
                self._nonce = None
                self._message = None
            nonce_snapshot = self.snapshot() if self._nonce is not None and self._nonce.source_status == "confirmed" else self.get_nonce()
            if nonce_snapshot.state not in {DreamDexAuthState.nonce_available.value, DreamDexAuthState.message_built.value}:
                return nonce_snapshot
            message_snapshot = nonce_snapshot if nonce_snapshot.state == DreamDexAuthState.message_built.value else self.build_message()
            if message_snapshot.state != DreamDexAuthState.message_built.value:
                return message_snapshot
            started = self._now()
            try:
                signature = self.signer.sign_message(self._message.message)  # type: ignore[union-attr]
                self._state = DreamDexAuthState.signature_available.value
                self._attempt = DreamDexAuthAttempt(self._state, started, self._now(), nonce=self._nonce.nonce if self._nonce else None, message=self._message.message if self._message else None, signature=signature)
                self._state = DreamDexAuthState.login_pending.value
                response = self.transport.login(self._message.message, signature)  # type: ignore[union-attr]
                response = response if isinstance(response, DreamDexLoginResponse) else DreamDexLoginResponse.from_payload(response, observed_at=self._now())
                if response.source_status != "confirmed" or response.token is None or response.expires_at is None:
                    return self._failed(response.error_code or "malformed_login_response", started)
                self._token_state = DreamDexTokenState(response.token, response.expires_at, self.refresh_skew_seconds, "confirmed")
                self._state = DreamDexAuthState.authenticated.value
                self._attempt = DreamDexAuthAttempt(self._state, started, self._now(), nonce=self._nonce.nonce if self._nonce else None, message=self._message.message if self._message else None, signature=signature)
                return self.snapshot()
            except Exception as exc:
                return self._failed(str(exc).split(":", 1)[0][:80], started)

    def ensure_authenticated(self) -> DreamDexAuthSnapshot:
        with self._lock:
            now = self._now()
            if self._token_state.usable(now):
                self._state = DreamDexAuthState.authenticated.value
                return self.snapshot()
            return self.authenticate(force=False)

    def authenticated_get(self, path: str) -> AuthTransportResponse:
        with self._lock:
            first = self.ensure_authenticated()
            if first.state != DreamDexAuthState.authenticated.value or not first.token_state.token:
                return AuthTransportResponse(401, None)
            status, payload = _response_parts(self.transport.authenticated_get(path, token=first.token_state.token))
            if status != 401:
                return AuthTransportResponse(status, payload)
            refreshed = self.authenticate(force=True)
            if refreshed.state != DreamDexAuthState.authenticated.value or not refreshed.token_state.token:
                self._state = DreamDexAuthState.unauthorized.value
                self._token_state = DreamDexTokenState(refresh_skew_seconds=self.refresh_skew_seconds, source_status="unauthorized")
                return AuthTransportResponse(401, None)
            status, payload = _response_parts(self.transport.authenticated_get(path, token=refreshed.token_state.token))
            if status == 401:
                self._state = DreamDexAuthState.unauthorized.value
                self._token_state = DreamDexTokenState(refresh_skew_seconds=self.refresh_skew_seconds, source_status="unauthorized")
            return AuthTransportResponse(status, payload)

    def reset(self) -> DreamDexAuthSnapshot:
        with self._lock:
            self._state = DreamDexAuthState.unconfigured.value if not self.manager_configured else DreamDexAuthState.nonce_required.value
            self._token_state = DreamDexTokenState(refresh_skew_seconds=self.refresh_skew_seconds)
            self._nonce = None
            self._message = None
            self._attempt = None
            return self.snapshot()


__all__ += [
    "AUTH_SCHEMA_AUDIT", "AuthIdentityStatus", "DreamDexAuthState", "DreamDexNonceResponse",
    "DreamDexSiweMessage", "DreamDexLoginRequest", "DreamDexLoginResponse", "DreamDexTokenState",
    "DreamDexAuthIdentity", "DreamDexAuthAttempt", "DreamDexAuthSnapshot", "DreamDexMessageSigner",
    "FixtureMessageSigner", "RejectingUnconfiguredSigner", "AuthTransportResponse", "DreamDexAuthTransport",
    "FixtureDreamDexAuthTransport", "UnconfiguredDreamDexAuthTransport", "DreamDexAuthManager",
    "AuthState",
]
