"""Confirmed DreamDEX read-only market, vault and RPC sources.

The REST routes mirror the public Bot Kit client. Account state is not fetched
from an invented account REST resource: balances come from the documented vault
route and read-only Somnia RPC calls. Open orders and fills remain explicitly
unavailable until a confirmed route is provided.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence
import json
import os
import re
from urllib.parse import quote

from bot.integrations.dreamdex_auth_models import (
    AuthenticatedAccountSnapshot,
    AuthenticatedBalanceSnapshot,
    AuthenticatedOrderSnapshot,
    AuthenticatedSourceStatus,
    AuthenticatedReadOnlyTransport,
    UnconfiguredAuthenticatedReadOnlyTransport,
    DreamDexAuthManager,
)
from bot.integrations.dreamdex_fill_events import (
    FillEventPage,
    OrderFilledEventIndexer,
)
from bot.integrations.dreamdex_order_metadata import (
    OrderMetadataKey,
    OrderMetadataResolver,
    OrderMetadataResolverReport,
    normalize_order_metadata,
)
from bot.integrations.dreamdex_authenticated_read_only import AuthenticatedResponseSchemaFingerprint, DreamDexAuthenticatedReadOnlyTransport
from bot.integrations.dreamdex_market_rules import DreamDexMarketTradingRules, PublicMarketSchemaFingerprint, fingerprint_market_payload, parse_market_trading_rules


def _dec(value: Any, default: Decimal | None = None) -> Decimal | None:
    if isinstance(value, bool) or value is None or value == "":
        return default
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return default


def _first(row: Mapping[str, Any], *names: str, default: Any = None) -> Any:
    for name in names:
        if name in row and row[name] is not None:
            return row[name]
    return default


def _parse_int(row: Mapping[str, Any], primary: str, alias: str) -> int | None:
    value = row.get(primary, row.get(alias))
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"invalid {primary}")
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"invalid {primary}")
    if parsed < 0 or parsed > 255:
        raise ValueError(f"invalid {primary}")
    return parsed


def _is_address(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    clean = value.lower().removeprefix("0x")
    return len(clean) == 40 and all(char in "0123456789abcdef" for char in clean)


class AssetKind(str, Enum):
    native = "native"
    erc20 = "erc20"
    special = "special"
    unknown = "unknown"
    NATIVE = native
    ERC20 = erc20
    SPECIAL = special
    UNKNOWN = unknown


NATIVE_SENTINEL = "0x28f34defd2b4cb48d9ee6d89f2be4bc601694c00"


def _asset_kind(symbol: str, token_address: str | None, *, side: str) -> AssetKind:
    # This is intentionally based on the Bot Kit's explicit SOMI:USDso
    # baseIsNative mapping (packages/core/src/config/tokens.ts), not on
    # token-address heuristics.
    if symbol.upper() == "SOMI:USDSO" and side == "base":
        return AssetKind.NATIVE
    if token_address and token_address.lower() == NATIVE_SENTINEL:
        return AssetKind.SPECIAL
    if _is_address(token_address):
        return AssetKind.ERC20
    return AssetKind.UNKNOWN


def _rows(payload: Any, *keys: str) -> list[Mapping[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, Mapping)]
    if isinstance(payload, Mapping):
        if any(key in payload for key in ("symbol", "market", "orderId", "fillId")):
            return [payload]
        for key in keys:
            if isinstance(payload.get(key), list):
                return [item for item in payload[key] if isinstance(item, Mapping)]
        data = payload.get("data")
        if isinstance(data, list):
            return [item for item in data if isinstance(item, Mapping)]
        if isinstance(data, Mapping):
            return _rows(data, *keys)
    return []


def _utc(value: Any = None, fallback: datetime | None = None) -> datetime:
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, (int, float)):
        number = float(value) / (1000 if value > 10_000_000_000 else 1)
        result = datetime.fromtimestamp(number, tz=timezone.utc)
    elif value:
        try:
            result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            result = fallback or datetime.now(timezone.utc)
    else:
        result = fallback or datetime.now(timezone.utc)
    return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result.astimezone(timezone.utc)


def _orderbook_status(orderbook: Mapping[str, Any] | None, observed_at: datetime, max_age_seconds: Decimal = Decimal("30")) -> str:
    if not isinstance(orderbook, Mapping):
        return "unavailable"
    bids, asks = orderbook.get("bids"), orderbook.get("asks")
    if not isinstance(bids, list) or not isinstance(asks, list) or not bids or not asks:
        return "unavailable"
    raw_timestamp = _first(orderbook, "timestamp", "updatedAt", "updated_at", "time")
    if raw_timestamp is None:
        return "unavailable"
    sentinel = datetime(1970, 1, 1, tzinfo=timezone.utc)
    try:
        timestamp = _utc(raw_timestamp, sentinel)
    except (TypeError, ValueError, OverflowError, OSError):
        return "unavailable"
    if timestamp == sentinel and str(raw_timestamp) not in {"0", "0.0"}:
        return "unavailable"
    age = max(Decimal("0"), Decimal(str((observed_at - timestamp).total_seconds())))
    return "stale" if age > max_age_seconds else "available"


def mask_account_id(value: str | None) -> str:
    if not value:
        return "<missing>"
    text = str(value)
    return "***" if len(text) <= 8 else f"{text[:4]}...{text[-4:]}"


PLATFORM_ROLES = frozenset({"owner_login_wallet", "dreamdex_smart_wallet", "operator_wallet", "unknown"})
EVIDENCE_LEVELS = frozenset({"unavailable", "user_confirmed", "observed", "independently_confirmed", "conflicting", "authoritative"})
ONCHAIN_CODE_TYPES = frozenset({"eoa_no_code", "contract_code_present", "unavailable", "conflicting"})


def _platform_role(value: str | None) -> tuple[str, str]:
    if value is None or value == "":
        return "unknown", "unavailable"
    role = str(value).strip().lower()
    if role not in PLATFORM_ROLES:
        raise ValueError("invalid platform role")
    return role, "user_confirmed"


def _onchain_code_type(code: SourceValue) -> str:
    if code.status == "conflicting":
        return "conflicting"
    if not code.available or code.value is None:
        return "unavailable"
    raw = str(code.value).lower()
    if raw in {"0x", "0x0"}:
        return "eoa_no_code"
    return "contract_code_present"


class ReadOnlyTransport(Protocol):
    def get(self, path: str, *, params: Mapping[str, str] | None = None) -> Any: ...


class RpcTransport(Protocol):
    def call(self, method: str, params: Sequence[Any]) -> Any: ...


class HttpGetTransport:
    """GET-only REST transport; no auth headers and no mutation methods."""

    def __init__(self, base_url: str, timeout_seconds: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    def get(self, path: str, *, params: Mapping[str, str] | None = None) -> Any:
        import httpx
        response = httpx.get(
            f"{self.base_url}/{path.lstrip('/')}",
            params=dict(params or {}),
            headers={"Accept": "application/json"},
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        return response.json()


class HttpRpcTransport:
    """JSON-RPC transport allowing only public read-only methods."""

    ALLOWED_METHODS = frozenset({"eth_call", "eth_getBalance", "eth_getCode", "eth_chainId", "eth_blockNumber"})

    def __init__(self, rpc_url: str, timeout_seconds: float = 10.0) -> None:
        self.rpc_url = rpc_url
        self.timeout_seconds = timeout_seconds

    def call(self, method: str, params: Sequence[Any]) -> Any:
        if method not in self.ALLOWED_METHODS:
            raise ValueError(f"RPC method is not allowed in read-only mode: {method}")
        import httpx
        response = httpx.post(
            self.rpc_url,
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": list(params)},
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("error"):
            raise RuntimeError(str(payload["error"]))
        return payload.get("result")


@dataclass(frozen=True)
class SourceValue:
    value: Any
    status: str
    source: str
    reason: str | None = None
    observed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    error_code: str | None = None

    @property
    def available(self) -> bool:
        return self.status == "available" and self.value is not None


@dataclass(frozen=True)
class MarketMetadata:
    symbol: str
    base_asset: str | None
    quote_asset: str | None
    base_token_address: str | None
    quote_token_address: str | None
    pool_contract: str | None
    price_tick_size: Decimal | None
    quantity_step_size: Decimal | None
    minimum_quantity: Decimal | None
    minimum_notional: Decimal | None
    status: str | None
    base_decimals: int | None = None
    quote_decimals: int | None = None
    stop_registry: str | None = None
    supported_order_types: tuple[str, ...] = ()
    maker_fee: Decimal | None = None
    taker_fee: Decimal | None = None
    observed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    source: str = "markets"
    base_asset_kind: AssetKind = AssetKind.unknown
    quote_asset_kind: AssetKind = AssetKind.unknown
    trading_rules: DreamDexMarketTradingRules | None = None
    schema_fingerprint: PublicMarketSchemaFingerprint | None = None

    @property
    def active(self) -> bool:
        if self.trading_rules is not None:
            return self.trading_rules.trading_enabled is True and self.trading_rules.status_for("market_status") == "confirmed"
        return (self.status or "").lower() in {"active", "trading", "open"}

    @property
    def base_is_native(self) -> bool:
        return self.base_asset_kind is AssetKind.native

    @property
    def quote_is_native(self) -> bool:
        return self.quote_asset_kind is AssetKind.native

    @property
    def pool_address(self) -> str | None:
        """Backward-compatible alias for the confirmed ``contract`` field."""
        return self.pool_contract

    @property
    def quantity_step(self) -> Decimal | None:
        return self.quantity_step_size

    @property
    def lot_size(self) -> Decimal | None:
        return self.quantity_step_size

    def is_fresh(self, *, now: datetime | None = None, max_age_seconds: Decimal = Decimal("30")) -> bool:
        age = max(Decimal("0"), Decimal(str((_utc(now) - self.observed_at).total_seconds())))
        return age <= max_age_seconds


@dataclass(frozen=True)
class MarketReadOnlySnapshot:
    metadata: MarketMetadata
    orderbook: Mapping[str, Any] | None
    recent_trades: tuple[Mapping[str, Any], ...]
    status: str = "available"
    reason: str | None = None
    observed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class MarketReadOnlySource:
    def __init__(self, transport: ReadOnlyTransport | Callable[..., Any], symbol: str = "SOMI:USDso", clock: Callable[[], datetime] | None = None) -> None:
        if not symbol or ":" not in symbol:
            raise ValueError("market symbol must be a non-empty BASE:QUOTE identifier")
        self.transport = transport
        self.symbol = symbol
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._last_schema_fingerprint: PublicMarketSchemaFingerprint | None = None

    def _get(self, path: str, params: Mapping[str, str] | None = None) -> Any:
        try:
            return self.transport.get(path, params=params)  # type: ignore[attr-defined]
        except AttributeError:
            try:
                return self.transport(path, params=params)  # type: ignore[misc]
            except TypeError:
                return self.transport(path)  # type: ignore[misc]
        except TypeError:
            return self.transport.get(path)  # type: ignore[attr-defined]

    def markets(self) -> list[Mapping[str, Any]]:
        payload = self._get("/markets")
        self._last_schema_fingerprint = fingerprint_market_payload(payload, observed_at=self.clock())
        return _rows(payload, "markets", "items")

    @property
    def schema_fingerprint(self) -> PublicMarketSchemaFingerprint | None:
        return self._last_schema_fingerprint

    def metadata(self) -> MarketMetadata:
        rows = [item for item in self.markets() if str(_first(item, "symbol", "market", default="")) == self.symbol]
        if not rows:
            raise ValueError(f"market {self.symbol} was not found")
        row = rows[0]
        duplicate_conflicts: dict[str, tuple[Any, ...]] = {}
        if len(rows) > 1:
            # Duplicate rows are not silently selected. Identical duplicates are
            # harmless fixture noise; conflicting fields remain visible as
            # conflict evidence and fail closed in the validator.
            keys = set().union(*(item.keys() for item in rows))
            for key in keys:
                values = tuple(item.get(key) for item in rows)
                if len(set(repr(value) for value in values)) > 1:
                    duplicate_conflicts[key] = values
        types = _first(row, "supportedOrderTypes", "supported_order_types", "orderTypes", default=())
        if isinstance(types, str):
            types = (types,)
        symbol = self.symbol
        base_asset, quote_asset = symbol.split(":", 1)
        base_address = _first(row, "base", "baseTokenAddress", "base_token_address", "baseAddress")
        quote_address = _first(row, "quote", "quoteTokenAddress", "quote_token_address", "quoteAddress")
        pool_contract = _first(row, "contract", "poolContract", "pool_contract", "poolAddress", "pool_address")
        base_decimals = _parse_int(row, "baseDecimals", "base_decimals")
        quote_decimals = _parse_int(row, "quoteDecimals", "quote_decimals")
        for name, value in (("pool_contract", pool_contract), ("base_token_address", base_address), ("quote_token_address", quote_address), ("stop_registry", _first(row, "stopRegistry", "stop_registry"))):
            if value is not None and not _is_address(value):
                raise ValueError(f"invalid {name} address")
        trading_rules = parse_market_trading_rules(
            row, symbol=self.symbol, observed_at=self.clock(),
            allow_legacy_aliases=bool(getattr(self.transport, "is_fixture", False)),
        )
        if duplicate_conflicts:
            evidence = dict(trading_rules.field_statuses)
            mapped = {
                "contract": "market_address", "base": "base_token_address", "quote": "quote_token_address",
                "tickSize": "tick_size", "lotSize": "quantity_step", "quantityStepSize": "quantity_step",
                "minQuantity": "minimum_quantity", "minimumNotional": "minimum_notional", "status": "market_status",
                "marketStatus": "market_status", "baseDecimals": "base_decimals", "quoteDecimals": "quote_decimals",
            }
            for key in duplicate_conflicts:
                field_name = mapped.get(key)
                if field_name and field_name in evidence:
                    evidence[field_name] = replace(evidence[field_name], status="conflicting", source="conflicting", reason=f"conflicting duplicate {key}")
            trading_rules = replace(
                trading_rules, source_status="conflicting", schema_status="conflicting",
                authoritative_fields=tuple(name for name in trading_rules.authoritative_fields if evidence.get(name, None) and evidence[name].status == "confirmed"),
                conflicts=tuple(sorted(duplicate_conflicts)), conflicting_values=duplicate_conflicts,
                field_statuses=evidence,
            )
        return MarketMetadata(
            symbol=self.symbol,
            base_asset=base_asset,
            quote_asset=quote_asset,
            base_token_address=trading_rules.base_token_address,
            quote_token_address=trading_rules.quote_token_address,
            pool_contract=trading_rules.market_address,
            price_tick_size=trading_rules.tick_size,
            quantity_step_size=trading_rules.quantity_step,
            minimum_quantity=trading_rules.minimum_quantity,
            minimum_notional=trading_rules.minimum_notional,
            status=trading_rules.market_status,
            supported_order_types=tuple(trading_rules.confirmed_order_types or ()),
            maker_fee=_dec(_first(row, "makerFee", "maker_fee", "makerFeeBps")),
            taker_fee=_dec(_first(row, "takerFee", "taker_fee", "takerFeeBps")),
            observed_at=_utc(_first(row, "timestamp", "updatedAt", "updated_at"), self.clock()),
            base_decimals=trading_rules.base_decimals,
            quote_decimals=trading_rules.quote_decimals,
            stop_registry=trading_rules.stop_registry,
            base_asset_kind=_asset_kind(self.symbol, base_address, side="base"),
            quote_asset_kind=_asset_kind(self.symbol, quote_address, side="quote"),
            trading_rules=trading_rules,
            schema_fingerprint=self._last_schema_fingerprint,
        )

    def orderbook(self) -> Mapping[str, Any]:
        payload = self._get("/orderbooks", {"symbols": self.symbol})
        rows = _rows(payload, "orderbooks", "data")
        if rows:
            return rows[0]
        if isinstance(payload, list) and payload and isinstance(payload[0], Mapping):
            return payload[0]
        if isinstance(payload, Mapping):
            for key in ("orderbook", "book", "result", "data"):
                nested = payload.get(key)
                if isinstance(nested, Mapping):
                    return nested
                if isinstance(nested, list) and nested and isinstance(nested[0], Mapping):
                    return nested[0]
        return payload if isinstance(payload, Mapping) else {}

    def recent_trades(self, limit: int = 20) -> tuple[Mapping[str, Any], ...]:
        payload = self._get(f"/markets/{quote(self.symbol, safe='')}/trades", {"limit": str(limit)})
        return tuple(_rows(payload, "trades", "data", "items"))

    def snapshot(self, limit: int = 20) -> MarketReadOnlySnapshot:
        metadata = self.metadata()
        return MarketReadOnlySnapshot(metadata, self.orderbook(), self.recent_trades(limit), observed_at=self.clock())

    fetch_markets = markets
    fetch_metadata = metadata
    fetch_orderbook = orderbook
    fetch_recent_trades = recent_trades
    fetch_snapshot = snapshot


@dataclass(frozen=True)
class VaultReadOnlySnapshot:
    owner: str
    base: SourceValue
    quote: SourceValue
    observed_at: datetime

    @property
    def available(self) -> bool:
        return self.base.available and self.quote.available


class VaultReadOnlySource:
    def __init__(self, transport: ReadOnlyTransport | Callable[..., Any], symbol: str, base_asset: str, quote_asset: str, clock: Callable[[], datetime] | None = None) -> None:
        self.transport, self.symbol = transport, symbol
        self.base_asset, self.quote_asset = base_asset, quote_asset
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def fetch(self, owner: str) -> VaultReadOnlySnapshot:
        observed = self.clock()
        try:
            path = f"/markets/{quote(self.symbol, safe='')}/vault/balance"
            try:
                payload = self.transport.get(path, params={"walletAddress": owner})  # type: ignore[attr-defined]
            except AttributeError:
                payload = self.transport(path, params={"walletAddress": owner})  # type: ignore[misc]
            rows = payload.get("data", payload) if isinstance(payload, Mapping) else payload
            if not isinstance(rows, Mapping):
                raise ValueError("vault response is not an object")
            base = _source_value(rows, self.base_asset, "vault_rest", observed)
            quote_value = _source_value(rows, self.quote_asset, "vault_rest", observed)
            return VaultReadOnlySnapshot(owner, base, quote_value, observed)
        except Exception as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status in {401, 403}:
                reason = "unauthorized"
                return VaultReadOnlySnapshot(owner, SourceValue(None, "unauthorized", "vault_rest", reason, observed, "unauthorized"), SourceValue(None, "unauthorized", "vault_rest", reason, observed, "unauthorized"), observed)
            reason = f"unavailable:{status or type(exc).__name__}"
            return VaultReadOnlySnapshot(owner, SourceValue(None, "unavailable", "vault_rest", reason, observed, "unavailable"), SourceValue(None, "unavailable", "vault_rest", reason, observed, "unavailable"), observed)

    fetch_balance = fetch


def _source_value(payload: Mapping[str, Any], asset: str, source: str, observed: datetime) -> SourceValue:
    candidates = [payload.get(asset), payload.get(asset.lower()), payload.get(asset.upper())]
    balances = payload.get("balances")
    if isinstance(balances, Mapping):
        candidates.append(balances.get(asset))
    elif isinstance(balances, list):
        candidates.extend(
            _first(row, "total", "available", "balance", "amount", "vaultBalance")
            for row in balances
            if isinstance(row, Mapping) and str(_first(row, "asset", "token", "symbol", default="")) == asset
        )
    data = payload.get("data")
    if isinstance(data, list):
        candidates.extend(
            _first(row, "total", "available", "balance", "amount", "vaultBalance")
            for row in data
            if isinstance(row, Mapping) and str(_first(row, "asset", "token", "symbol", default="")) == asset
        )
    value: Any = next((item for item in candidates if item is not None), None)
    if isinstance(value, Mapping):
        value = _first(value, "total", "available", "balance", "amount", "vaultBalance")
    if value is None and str(_first(payload, "asset", "token", default="")) == asset:
        value = _first(payload, "total", "available", "balance", "amount")
    parsed = _dec(value)
    return SourceValue(parsed, "available" if parsed is not None else "unavailable", source, None if parsed is not None else "asset_missing", observed, None if parsed is not None else "unavailable")


@dataclass(frozen=True)
class RpcAccountReadOnlySnapshot:
    owner: str
    base_vault: SourceValue
    quote_vault: SourceValue
    base_wallet: SourceValue
    quote_wallet: SourceValue
    native_gas: SourceValue
    observed_at: datetime
    gas_address: str | None = None


@dataclass(frozen=True)
class TokenReadOnlyDiagnostics:
    address: str | None
    asset_kind: AssetKind
    code: SourceValue
    raw_balance: SourceValue
    balance: SourceValue
    decimals: SourceValue
    balance_method: str


@dataclass(frozen=True, repr=False)
class AddressReadOnlyDiagnostics:
    """Independent public-RPC observations for one candidate account address."""

    address: str
    code: SourceValue
    address_type: str
    native_gas: SourceValue
    # Keep the original wallet fields for callers that consumed this
    # diagnostic object before token-level details were added.
    wallet_base: SourceValue
    wallet_quote: SourceValue
    vault_base: SourceValue
    vault_quote: SourceValue
    base_token: TokenReadOnlyDiagnostics | None = None
    quote_token: TokenReadOnlyDiagnostics | None = None
    platform_role: str = "unknown"
    platform_role_status: str = "unavailable"
    onchain_code_type: str = "unavailable"
    onchain_code_status: str = "unavailable"
    deployment_status: str = "unavailable"
    account_abstraction_status: str = "unavailable"

    def __post_init__(self) -> None:
        role, role_status = _platform_role(self.platform_role)
        object.__setattr__(self, "platform_role", role)
        if self.platform_role_status not in EVIDENCE_LEVELS:
            raise ValueError("invalid platform role status")
        if self.platform_role_status == "unavailable" and role != "unknown":
            object.__setattr__(self, "platform_role_status", role_status)
        code_type = self.onchain_code_type if self.onchain_code_type != "unavailable" else _onchain_code_type(self.code)
        if code_type not in ONCHAIN_CODE_TYPES:
            raise ValueError("invalid onchain code type")
        object.__setattr__(self, "onchain_code_type", code_type)
        object.__setattr__(self, "onchain_code_status", self.onchain_code_status if self.onchain_code_status != "unavailable" else self.code.status)

    def __repr__(self) -> str:
        return (
            f"AddressReadOnlyDiagnostics(address={mask_account_id(self.address)!r}, "
            f"platform_role={self.platform_role!r}, onchain_code_type={self.onchain_code_type!r}, "
            f"onchain_code_status={self.onchain_code_status!r})"
        )

    @property
    def wallet_somi(self) -> SourceValue:
        return self.wallet_base

    @property
    def wallet_usdso(self) -> SourceValue:
        return self.wallet_quote

    @property
    def vault_somi(self) -> SourceValue:
        return self.vault_base

    @property
    def vault_usdso(self) -> SourceValue:
        return self.vault_quote


@dataclass(frozen=True, repr=False)
class DreamDexIdentityBindingEvidence:
    owner_address: str | None
    trading_address: str | None
    owner_platform_role: str
    trading_platform_role: str
    owner_onchain_code_type: str
    trading_onchain_code_type: str
    ui_role_confirmation: str
    authenticated_vault_probe_status: str
    authenticated_order_probe_status: str
    authenticated_query_address: str | None
    token_subject_status: str
    official_mapping_status: str
    binding_status: str
    authoritative: bool
    evidence_sources: tuple[str, ...] = ()
    unresolved_reasons: tuple[str, ...] = ()
    observed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    authenticated_query_address_match: str = "unresolved"

    def __post_init__(self) -> None:
        for name in ("owner_address", "trading_address", "authenticated_query_address"):
            value = getattr(self, name)
            if value is not None and not _is_address(value):
                raise ValueError(f"invalid {name}")
        for name in ("owner_platform_role", "trading_platform_role"):
            role = getattr(self, name)
            if role not in PLATFORM_ROLES:
                raise ValueError(f"invalid {name}")
        for name in ("ui_role_confirmation", "token_subject_status", "official_mapping_status", "binding_status"):
            if getattr(self, name) not in EVIDENCE_LEVELS:
                raise ValueError(f"invalid {name}")
        if self.owner_onchain_code_type not in ONCHAIN_CODE_TYPES or self.trading_onchain_code_type not in ONCHAIN_CODE_TYPES:
            raise ValueError("invalid onchain code type")
        if self.authenticated_query_address_match not in {"yes", "no", "unresolved"}:
            raise ValueError("invalid authenticated query address match")
        if self.trading_address and self.authenticated_query_address:
            matched = self.trading_address.lower() == self.authenticated_query_address.lower()
            object.__setattr__(self, "authenticated_query_address_match", "yes" if matched else "no")
            if not matched:
                object.__setattr__(self, "binding_status", "conflicting")
                object.__setattr__(self, "unresolved_reasons", tuple(dict.fromkeys((*self.unresolved_reasons, "authenticated_query_address_mismatch"))))
        object.__setattr__(self, "observed_at", _utc(self.observed_at))
        if self.authoritative and self.binding_status != "authoritative":
            raise ValueError("authoritative binding requires authoritative status")

    def safe_dict(self) -> dict[str, Any]:
        return {
            "owner_address": mask_account_id(self.owner_address),
            "trading_address": mask_account_id(self.trading_address),
            "owner_platform_role": self.owner_platform_role,
            "trading_platform_role": self.trading_platform_role,
            "owner_onchain_code_type": self.owner_onchain_code_type,
            "trading_onchain_code_type": self.trading_onchain_code_type,
            "ui_role_confirmation": self.ui_role_confirmation,
            "authenticated_vault_probe_status": self.authenticated_vault_probe_status,
            "authenticated_order_probe_status": self.authenticated_order_probe_status,
            "authenticated_query_address": mask_account_id(self.authenticated_query_address),
            "authenticated_query_address_match": self.authenticated_query_address_match,
            "token_subject_status": self.token_subject_status,
            "official_mapping_status": self.official_mapping_status,
            "binding_status": self.binding_status,
            "authoritative": self.authoritative,
            "evidence_sources": self.evidence_sources,
            "unresolved_reasons": self.unresolved_reasons,
            "observed_at": self.observed_at.isoformat(),
        }

    def __repr__(self) -> str:
        return (
            f"DreamDexIdentityBindingEvidence(owner_address={mask_account_id(self.owner_address)!r}, "
            f"trading_address={mask_account_id(self.trading_address)!r}, binding_status={self.binding_status!r}, "
            f"authoritative={self.authoritative})"
        )


class RpcAccountReadOnlySource:
    def __init__(self, transport: RpcTransport | Callable[..., Any], *, pool_address: str, base_token_address: str, quote_token_address: str, owner: str | None = None, account_address: str | None = None, gas_address: str | None = None, base_decimals: int = 18, quote_decimals: int = 18, base_asset_kind: AssetKind = AssetKind.erc20, quote_asset_kind: AssetKind = AssetKind.erc20, clock: Callable[[], datetime] | None = None) -> None:
        self.transport = transport
        self.account_address = account_address or owner
        self.gas_address = gas_address or owner
        self.owner = self.account_address  # compatibility alias; account reads use the explicit trading address
        self.pool_address, self.base_token_address, self.quote_token_address = pool_address, base_token_address, quote_token_address
        self.base_decimals, self.quote_decimals = base_decimals, quote_decimals
        self.base_asset_kind, self.quote_asset_kind = base_asset_kind, quote_asset_kind
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _call(self, method: str, params: Sequence[Any]) -> Any:
        if hasattr(self.transport, "call"):
            return self.transport.call(method, params)  # type: ignore[attr-defined]
        return self.transport(method, params)  # type: ignore[misc]

    @staticmethod
    def _address_word(address: str) -> str:
        clean = address.lower().removeprefix("0x")
        if len(clean) != 40 or any(char not in "0123456789abcdef" for char in clean):
            raise ValueError("invalid public address")
        return clean.rjust(64, "0")

    @classmethod
    def _calldata(cls, signature: str, address: str, second: str | None = None) -> str:
        from eth_utils import keccak
        selector = keccak(text=signature)[:4].hex()
        data = selector + cls._address_word(address)
        if second is not None:
            data += cls._address_word(second)
        return "0x" + data

    @staticmethod
    def _error(source: str, observed: datetime, error_code: str, reason: str | None = None) -> SourceValue:
        return SourceValue(None, "unavailable", source, reason or error_code, observed, error_code)

    @staticmethod
    def _sanitized_error(exc: Exception, error_code: str) -> str:
        text = re.sub(r"0x[0-9a-fA-F]{8,}", "<hex>", str(exc))
        text = re.sub(r"(?i)(private[_ -]?key|seed[_ -]?phrase|authorization|bearer|signature)\s*[:=]\s*\S+", r"\1=<redacted>", text)
        return f"{error_code}:{text[:180]}"

    @staticmethod
    def _decode_uint256(result: Any) -> tuple[int | None, str | None]:
        if result is None or result == "":
            return None, "empty_result"
        if not isinstance(result, str) or not result.startswith("0x"):
            return None, "malformed_hex"
        body = result[2:]
        if not body:
            return None, "empty_result"
        if any(char not in "0123456789abcdefABCDEF" for char in body):
            return None, "malformed_hex"
        try:
            return int(body, 16), None
        except (TypeError, ValueError, OverflowError):
            return None, "decode_error"

    def _view(self, method: str, params: Sequence[Any], source: str, observed: datetime, decimals: int = 0) -> SourceValue:
        try:
            result = self._call(method, params)
            raw, error_code = self._decode_uint256(result)
            if error_code:
                return self._error(source, observed, error_code)
            parsed = Decimal(raw) if raw is not None else None
            if parsed is not None and decimals:
                try:
                    parsed = parsed / (Decimal(10) ** decimals)
                except (ArithmeticError, InvalidOperation):
                    return self._error(source, observed, "decode_error")
            return SourceValue(parsed, "available", source, None, observed, None)
        except Exception as exc:
            text = str(exc).lower()
            code = "contract_revert" if "revert" in text or "execution reverted" in text else "rpc_error"
            return self._error(source, observed, code, self._sanitized_error(exc, code))

    def _code(self, address: str, observed: datetime) -> SourceValue:
        if not _is_address(address):
            return self._error("rpc_code", observed, "invalid_target")
        try:
            result = self._call("eth_getCode", [address, "latest"])
            if result is None or result == "":
                return self._error("rpc_code", observed, "empty_result")
            if not isinstance(result, str) or not result.startswith("0x"):
                return self._error("rpc_code", observed, "malformed_hex")
            body = result[2:]
            if body and any(char not in "0123456789abcdefABCDEF" for char in body):
                return self._error("rpc_code", observed, "malformed_hex")
            return SourceValue(result, "available", "rpc_code", None, observed, None)
        except Exception as exc:
            text = str(exc).lower()
            code = "contract_revert" if "revert" in text else "rpc_error"
            return self._error("rpc_code", observed, code, self._sanitized_error(exc, code))

    def _token_diagnostics(self, address: str | None, kind: AssetKind, metadata_decimals: int, account: str, observed: datetime) -> TokenReadOnlyDiagnostics:
        code = self._code(address or "", observed) if address else self._error("rpc_token_code", observed, "invalid_target")
        if kind is AssetKind.native:
            raw = self._view("eth_getBalance", [account, "latest"], "rpc_native_balance", observed, 0)
            if raw.available:
                try:
                    balance = SourceValue(raw.value / (Decimal(10) ** metadata_decimals), "available", "rpc_native_balance", None, observed, None)
                except (ArithmeticError, InvalidOperation):
                    balance = self._error("rpc_native_balance", observed, "decode_error")
            else:
                balance = raw
            decimals = SourceValue(Decimal(metadata_decimals), "available", "market_metadata", "native_asset_no_decimals_call", observed, None)
            return TokenReadOnlyDiagnostics(address, kind, code, raw, balance, decimals, "eth_getBalance")
        if kind is not AssetKind.erc20 or not address or not _is_address(address):
            unavailable = self._error("rpc_erc20_balance", observed, "unavailable")
            decimals = self._error("rpc_erc20_decimals", observed, "unavailable")
            return TokenReadOnlyDiagnostics(address, kind, code, unavailable, unavailable, decimals, "unavailable")
        decimals = self._view("eth_call", [{"to": address, "data": self._calldata("decimals()", "0x0000000000000000000000000000000000000000")[:10]}, "latest"], "rpc_erc20_decimals", observed, 0)
        raw = self._view("eth_call", [{"to": address, "data": self._calldata("balanceOf(address)", account)}, "latest"], "rpc_erc20_balance", observed, 0)
        if not raw.available:
            balance = raw
        elif not decimals.available or decimals.value is None:
            balance = self._error("rpc_erc20_balance", observed, "decode_error", "decimals unavailable")
        else:
            try:
                balance = SourceValue(raw.value / (Decimal(10) ** int(decimals.value)), "available", "rpc_erc20_balance", None, observed, None)
            except (ArithmeticError, InvalidOperation, ValueError):
                balance = self._error("rpc_erc20_balance", observed, "decode_error")
        return TokenReadOnlyDiagnostics(address, kind, code, raw, balance, decimals, "balanceOf(address)")

    def fetch_address(self, address: str, *, include_gas: bool = True) -> AddressReadOnlyDiagnostics:
        observed = self.clock()
        invalid = not _is_address(address)
        code = self._error("rpc_code", observed, "invalid_target") if invalid else self._code(address, observed)
        if code.available:
            raw_code = str(code.value)[2:]
            address_type = "eoa" if raw_code in {"", "0"} else "contract"
        else:
            address_type = "unavailable"
        if invalid:
            wallet_base = TokenReadOnlyDiagnostics(None, self.base_asset_kind, code, self._error("rpc_erc20_balance", observed, "invalid_target"), self._error("rpc_erc20_balance", observed, "invalid_target"), self._error("rpc_erc20_decimals", observed, "invalid_target"), "unavailable")
            wallet_quote = TokenReadOnlyDiagnostics(None, self.quote_asset_kind, code, self._error("rpc_erc20_balance", observed, "invalid_target"), self._error("rpc_erc20_balance", observed, "invalid_target"), self._error("rpc_erc20_decimals", observed, "invalid_target"), "unavailable")
            vault_base = self._error("rpc_pool_vault", observed, "invalid_target")
            vault_quote = self._error("rpc_pool_vault", observed, "invalid_target")
            native = self._error("rpc_native_balance", observed, "invalid_target")
            return AddressReadOnlyDiagnostics(address, code, address_type, native, wallet_base.balance, wallet_quote.balance, vault_base, vault_quote, wallet_base, wallet_quote)
        targets_valid = _is_address(self.pool_address) and _is_address(self.base_token_address) and _is_address(self.quote_token_address)
        if not targets_valid:
            wallet_base = TokenReadOnlyDiagnostics(self.base_token_address, self.base_asset_kind, self._error("rpc_token_code", observed, "invalid_target"), self._error("rpc_erc20_balance", observed, "invalid_target"), self._error("rpc_erc20_balance", observed, "invalid_target"), self._error("rpc_erc20_decimals", observed, "invalid_target"), "unavailable")
            wallet_quote = TokenReadOnlyDiagnostics(self.quote_token_address, self.quote_asset_kind, self._error("rpc_token_code", observed, "invalid_target"), self._error("rpc_erc20_balance", observed, "invalid_target"), self._error("rpc_erc20_balance", observed, "invalid_target"), self._error("rpc_erc20_decimals", observed, "invalid_target"), "unavailable")
            vault_base = self._error("rpc_pool_vault", observed, "invalid_target")
            vault_quote = self._error("rpc_pool_vault", observed, "invalid_target")
        else:
            vault_base = self._view("eth_call", [{"to": self.pool_address, "data": self._calldata("getWithdrawableBalance(address,address)", address, self.base_token_address)}, "latest"], "rpc_pool_vault", observed, self.base_decimals)
            vault_quote = self._view("eth_call", [{"to": self.pool_address, "data": self._calldata("getWithdrawableBalance(address,address)", address, self.quote_token_address)}, "latest"], "rpc_pool_vault", observed, self.quote_decimals)
            wallet_base = self._token_diagnostics(self.base_token_address, self.base_asset_kind, self.base_decimals, address, observed)
            wallet_quote = self._token_diagnostics(self.quote_token_address, self.quote_asset_kind, self.quote_decimals, address, observed)
        native = wallet_base.balance if include_gas and self.base_asset_kind is AssetKind.native else (self._view("eth_getBalance", [address, "latest"], "rpc_native_balance", observed, 18) if include_gas else self._error("rpc_native_balance", observed, "unavailable"))
        return AddressReadOnlyDiagnostics(address, code, address_type, native, wallet_base.balance, wallet_quote.balance, vault_base, vault_quote, wallet_base, wallet_quote)

    def fetch(self, *, account_reads: bool = True) -> RpcAccountReadOnlySnapshot:
        observed = self.clock()
        if account_reads and self.account_address:
            diagnostics = self.fetch_address(self.account_address)
            return RpcAccountReadOnlySnapshot(diagnostics.address, diagnostics.vault_base, diagnostics.vault_quote, diagnostics.wallet_base, diagnostics.wallet_quote, diagnostics.native_gas, observed, diagnostics.address)
        unavailable = lambda source: self._error(source, observed, "unavailable")
        native_gas = self._view("eth_getBalance", [self.gas_address, "latest"], "rpc_native_balance", observed, 18) if self.gas_address else unavailable("rpc_native_balance")
        return RpcAccountReadOnlySnapshot(self.account_address or "", unavailable("rpc_pool_vault"), unavailable("rpc_pool_vault"), unavailable("rpc_erc20_balance"), unavailable("rpc_erc20_balance"), native_gas, observed, self.gas_address)

    fetch_snapshot = fetch


@dataclass(frozen=True)
class BalanceSnapshot:
    asset: str
    total: Decimal | None
    available: Decimal | None
    locked: Decimal | None
    status: str = "available"


@dataclass(frozen=True)
class AccountSnapshot:
    account_identifier: str
    balances: Mapping[str, BalanceSnapshot]
    vault_rest: VaultReadOnlySnapshot
    vault_rpc: RpcAccountReadOnlySnapshot
    open_orders_status: str = "source_unavailable"
    fills_status: str = "source_unavailable"
    observed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    source: str = "read-only"
    owner_address: str | None = None
    trading_address: str | None = None
    trading_address_status: str = "available"
    orderbook_status: str = "available"
    vault_address_semantics: str = "unresolved"
    owner_diagnostics: AddressReadOnlyDiagnostics | None = None
    trading_diagnostics: AddressReadOnlyDiagnostics | None = None
    account_address_semantics: str = "unresolved"
    authenticated: AuthenticatedAccountSnapshot = field(default_factory=AuthenticatedAccountSnapshot.unavailable)
    onchain_fills: FillEventPage = field(default_factory=FillEventPage.unavailable)
    order_metadata_report: OrderMetadataResolverReport = field(default_factory=OrderMetadataResolverReport.unavailable)
    authenticated_transport_status: str = "unconfigured"
    authenticated_request_execution_enabled: bool = False
    authenticated_order_by_id_status: str = "unconfigured"
    authenticated_schema_fingerprint_status: str = "unavailable"
    authenticated_vault_fingerprint: AuthenticatedResponseSchemaFingerprint | None = None
    authenticated_order_list_fingerprint: AuthenticatedResponseSchemaFingerprint | None = None
    authenticated_order_by_id_fingerprint: AuthenticatedResponseSchemaFingerprint | None = None
    auth_snapshot: Any | None = None
    identity_binding_evidence: DreamDexIdentityBindingEvidence | None = None

    def balance(self, asset: str) -> BalanceSnapshot:
        return self.balances.get(asset, BalanceSnapshot(asset, None, None, None, "source_unavailable"))

    @property
    def incomplete(self) -> bool:
        authenticated_authoritative = self.authenticated.authoritative_for(self.trading_address)
        balance_source_available = authenticated_authoritative or (
            self.vault_rest.available and self.vault_rpc.base_vault.available and self.vault_rpc.quote_vault.available
        )
        return not all((
            self.account_address_semantics == "resolved",
            self.trading_address_status == "available",
            self.orderbook_status == "available",
            balance_source_available,
            self.vault_rpc.base_wallet.available,
            self.vault_rpc.quote_wallet.available,
            self.vault_rpc.native_gas.available,
            self.open_orders_status != "source_unavailable",
            self.fills_status != "source_unavailable",
        ))

    def safe_dict(self) -> dict[str, Any]:
        return {
            "account_identifier": mask_account_id(self.account_identifier),
            "owner_address": mask_account_id(self.owner_address),
            "trading_address": mask_account_id(self.trading_address),
            "trading_address_status": self.trading_address_status,
            "account_address_semantics": self.account_address_semantics,
            "identity_binding_evidence": self.identity_binding_evidence.safe_dict() if self.identity_binding_evidence is not None else None,
            "owner_platform_role": self.owner_diagnostics.platform_role if self.owner_diagnostics else "unknown",
            "owner_platform_role_status": self.owner_diagnostics.platform_role_status if self.owner_diagnostics else "unavailable",
            "owner_onchain_code_type": self.owner_diagnostics.onchain_code_type if self.owner_diagnostics else "unavailable",
            "owner_onchain_code_status": self.owner_diagnostics.onchain_code_status if self.owner_diagnostics else "unavailable",
            "trading_platform_role": self.trading_diagnostics.platform_role if self.trading_diagnostics else "unknown",
            "trading_platform_role_status": self.trading_diagnostics.platform_role_status if self.trading_diagnostics else "unavailable",
            "trading_onchain_code_type": self.trading_diagnostics.onchain_code_type if self.trading_diagnostics else "unavailable",
            "trading_onchain_code_status": self.trading_diagnostics.onchain_code_status if self.trading_diagnostics else "unavailable",
            "balances": {key: {"total": str(value.total), "available": str(value.available), "locked": str(value.locked), "status": value.status} for key, value in self.balances.items()},
            "open_orders_status": self.open_orders_status,
            "fills_status": self.fills_status,
            "authenticated_account_source": "available" if self.authenticated.available else (self.authenticated.balances_status.reason or "unavailable"),
            "authenticated_pagination_complete": self.authenticated.pagination_complete,
            "authenticated_transport_status": self.authenticated_transport_status,
            "authenticated_request_execution_enabled": self.authenticated_request_execution_enabled,
            "authenticated_order_by_id_status": self.authenticated_order_by_id_status,
            "authenticated_schema_fingerprint_status": self.authenticated_schema_fingerprint_status,
            "authenticated_vault_fingerprint_observed": self.authenticated_vault_fingerprint is not None,
            "authenticated_order_list_fingerprint_observed": self.authenticated_order_list_fingerprint is not None,
            "authenticated_order_by_id_fingerprint_observed": self.authenticated_order_by_id_fingerprint is not None,
            "authentication_state": self.auth_snapshot.safe_dict() if self.auth_snapshot is not None and hasattr(self.auth_snapshot, "safe_dict") else {"state": "unconfigured", "token_present": False, "identity_authoritative": False},
            "onchain_fills_source": self.onchain_fills.source_status.status,
            "onchain_fills_reason": self.onchain_fills.source_status.reason,
            "onchain_fills_error_code": self.onchain_fills.source_status.error_code,
            "onchain_latest_block": self.onchain_fills.source_status.latest_block,
            "onchain_confirmed_through_block": self.onchain_fills.source_status.confirmed_through_block,
            "onchain_decoded_fill_count": self.onchain_fills.source_status.decoded_fill_count,
            "onchain_duplicate_count": self.onchain_fills.source_status.duplicate_count,
            "onchain_fills_pagination_complete": self.onchain_fills.pagination_complete,
            "onchain_fills_reorg_status": self.onchain_fills.source_status.reorg_status,
            "onchain_fills_account_match_status": self.onchain_fills.source_status.account_match_status,
            "order_metadata_status": self.order_metadata_report.status,
            "order_metadata_resolved_count": self.order_metadata_report.resolved_count,
            "order_metadata_conflict_count": self.order_metadata_report.conflict_count,
            "order_metadata_account_match": self.order_metadata_report.account_match,
            "observed_at": self.observed_at.isoformat(),
            "source": self.source,
        }


@dataclass(frozen=True)
class ReadOnlySnapshot:
    market: MarketMetadata
    account: AccountSnapshot
    observed_at: datetime
    source: str
    orderbook: Mapping[str, Any] | None = None
    recent_trades: tuple[Mapping[str, Any], ...] = ()
    owner_diagnostics: AddressReadOnlyDiagnostics | None = None
    trading_diagnostics: AddressReadOnlyDiagnostics | None = None
    onchain_fills: FillEventPage = field(default_factory=FillEventPage.unavailable)


@dataclass(frozen=True)
class ReconciliationReport:
    completed: bool
    trading_blocked: bool
    reason: str
    local_cash: Decimal | None
    exchange_cash: Decimal | None
    cash_difference: Decimal | None
    local_inventory: Decimal | None
    exchange_inventory: Decimal | None
    inventory_difference: Decimal | None
    vault_rest_base: SourceValue
    vault_rpc_base: SourceValue
    vault_rest_quote: SourceValue
    vault_rpc_quote: SourceValue
    mismatches: tuple[str, ...]
    observed_at: datetime
    unresolved_orders: tuple[str, ...] = ()


class DreamDexReadOnlyAdapter:
    """Compose confirmed market, vault and RPC sources; never mutates state."""

    def __init__(self, *, transport: ReadOnlyTransport | Callable[..., Any], rpc_transport: RpcTransport | Callable[..., Any], owner: str, trading_address: str | None = None, symbol: str = "SOMI:USDso", clock: Callable[[], datetime] | None = None, authenticated_transport: AuthenticatedReadOnlyTransport | None = None, fill_event_indexer: OrderFilledEventIndexer | None = None, order_metadata_resolver: OrderMetadataResolver | None = None, auth_manager: DreamDexAuthManager | None = None, owner_platform_role: str | None = None, trading_platform_role: str | None = None) -> None:
        if not owner or not str(owner).strip() or not _is_address(owner):
            raise ValueError("owner must be a public address")
        if trading_address is not None and not _is_address(trading_address):
            raise ValueError("trading address must be a public address")
        if not symbol or ":" not in symbol:
            raise ValueError("market symbol must be BASE:QUOTE")
        self.owner, self.trading_address, self.symbol, self.clock = str(owner), (str(trading_address) if trading_address else None), symbol, clock or (lambda: datetime.now(timezone.utc))
        self.authenticated_transport = authenticated_transport or UnconfiguredAuthenticatedReadOnlyTransport()
        self.auth_manager = auth_manager
        self.owner_platform_role, self._owner_platform_role_status = _platform_role(owner_platform_role)
        self.trading_platform_role, self._trading_platform_role_status = _platform_role(trading_platform_role)
        self.fill_event_indexer = fill_event_indexer
        self.order_metadata_resolver = order_metadata_resolver
        self._last_authenticated_vault_fingerprint: AuthenticatedResponseSchemaFingerprint | None = None
        self._last_authenticated_order_list_fingerprint: AuthenticatedResponseSchemaFingerprint | None = None
        self._last_authenticated_order_by_id_fingerprint: AuthenticatedResponseSchemaFingerprint | None = None
        self.market_source = MarketReadOnlySource(transport, symbol, self.clock)
        self._transport, self._rpc_transport = transport, rpc_transport

    def fetch_market(self) -> MarketReadOnlySnapshot:
        return self.market_source.snapshot()

    fetch_market_metadata = lambda self: self.market_source.metadata()

    def _fetch_authenticated_account(self) -> AuthenticatedAccountSnapshot:
        account_identifier = self.trading_address or self.owner
        if isinstance(self.authenticated_transport, DreamDexAuthenticatedReadOnlyTransport):
            return self._fetch_pinned_authenticated_account(account_identifier)
        try:
            balances = self.authenticated_transport.fetch_account_balances(account_identifier, (self.symbol,))
            open_orders = self.authenticated_transport.fetch_open_orders(account_identifier, self.symbol)
            recent_orders = self.authenticated_transport.fetch_recent_orders(account_identifier, self.symbol)
            fills = self.authenticated_transport.fetch_fills(account_identifier, self.symbol)
            commissions = self.authenticated_transport.fetch_commissions(account_identifier, self.symbol)
            observed = max((item.source_status.observed_at for item in (balances, open_orders, recent_orders, fills, commissions)), default=self.clock())
            return AuthenticatedAccountSnapshot(
                mask_account_id(account_identifier), balances.records, open_orders.records,
                recent_orders.records, fills.records, commissions.records,
                balances.source_status, open_orders.source_status, recent_orders.source_status,
                fills.source_status, commissions.source_status, observed,
            )
        except Exception as exc:
            observed = self.clock()
            reason = re.sub(r"0x[0-9a-fA-F]{8,}", "<hex>", str(exc))[:180]
            unavailable = AuthenticatedAccountSnapshot.unavailable(account_identifier)
            # Keep the source-level reason explicit without exposing account
            # identifiers, tokens, headers, or exception payloads.
            from dataclasses import replace
            statuses = {
                "balances_status": replace(unavailable.balances_status, reason="authenticated_source_error:" + reason, error_code="authenticated_source_error", observed_at=observed),
                "open_orders_status": replace(unavailable.open_orders_status, reason="authenticated_source_error:" + reason, error_code="authenticated_source_error", observed_at=observed),
                "recent_orders_status": replace(unavailable.recent_orders_status, reason="authenticated_source_error:" + reason, error_code="authenticated_source_error", observed_at=observed),
                "fills_status": replace(unavailable.fills_status, reason="authenticated_source_error:" + reason, error_code="authenticated_source_error", observed_at=observed),
                "commissions_status": replace(unavailable.commissions_status, reason="authenticated_source_error:" + reason, error_code="authenticated_source_error", observed_at=observed),
                "observed_at": observed,
            }
            return replace(unavailable, **statuses)

    def _fetch_pinned_authenticated_account(self, account_identifier: str) -> AuthenticatedAccountSnapshot:
        """Adapt the strict GET-only transport to the existing account model.

        The production transport intentionally exposes no account-wide or fill
        endpoint.  Consequently only the confirmed vault route and the
        documented-but-incomplete order list can be represented here; fills,
        commissions, and authoritative pagination remain unavailable.
        """
        transport = self.authenticated_transport
        self._last_authenticated_vault_fingerprint = None
        self._last_authenticated_order_list_fingerprint = None
        self._last_authenticated_order_by_id_fingerprint = None
        try:
            vault = transport.fetch_vault_balance(self.symbol, account_identifier)
            raw_orders, order_status = transport.fetch_orders_page(self.symbol, status="open")
            self._last_authenticated_vault_fingerprint = vault.schema_fingerprint
            self._last_authenticated_order_list_fingerprint = order_status.schema_fingerprint
        except Exception as exc:
            observed = self.clock()
            safe_reason = re.sub(r"0x[0-9a-fA-F]{8,}", "<hex>", str(exc))[:180]
            unavailable = AuthenticatedAccountSnapshot.unavailable(account_identifier)
            return replace(
                unavailable,
                balances_status=replace(unavailable.balances_status, reason="authenticated_source_error:" + safe_reason, error_code="authenticated_source_error", observed_at=observed),
                open_orders_status=replace(unavailable.open_orders_status, reason="authenticated_source_error:" + safe_reason, error_code="authenticated_source_error", observed_at=observed),
                observed_at=observed,
            )

        balances = tuple(vault.balances)
        orders: list[AuthenticatedOrderSnapshot] = []
        for raw in raw_orders:
            normalized = normalize_order_metadata(raw)
            orders.append(AuthenticatedOrderSnapshot(
                order_id=normalized.order_id or raw.key.order_id,
                symbol=normalized.symbol or raw.key.symbol,
                side=normalized.side,
                price=normalized.price,
                quantity=normalized.quantity,
                remaining_quantity=normalized.remaining_quantity,
                raw_status_name=normalized.raw_status,
                observed_at=raw.observed_at,
                account_identifier=account_identifier,
                source_status=AuthenticatedSourceStatus(
                    "available_empty" if order_status.records_status == "available_empty" else order_status.status,
                    order_status.endpoint_name,
                    order_status.observed_at,
                    order_status.raw_status_name,
                    order_status.reason,
                    order_status.error_code,
                    order_status.pagination_complete,
                    order_status.duplicate_count,
                    order_status.malformed_count,
                    order_status.response_body_status,
                    order_status.schema_status,
                    order_status.records_status,
                    order_status.pagination_status,
                    order_status.authority_status,
                ),
            ))
        observed = max((status.observed_at for status in (vault.source_status, order_status)), default=self.clock())
        fills_status = AuthenticatedSourceStatus(
            "unavailable", "account_fills", observed,
            reason="authenticated_fills_source_unavailable",
            error_code="authenticated_fills_source_unavailable",
            pagination_complete=False,
        )
        recent_status = AuthenticatedSourceStatus(
            "unavailable", "historical_orders", observed,
            reason="authenticated_historical_orders_source_unavailable",
            error_code="authenticated_historical_orders_source_unavailable",
            pagination_complete=False,
        )
        commissions_status = AuthenticatedSourceStatus(
            "unavailable", "account_commissions", observed,
            reason="authenticated_commissions_source_unavailable",
            error_code="authenticated_commissions_source_unavailable",
            pagination_complete=False,
        )
        # A configured transport with a missing/invalid response must not be
        # upgraded to an authoritative account snapshot by this adapter.
        return AuthenticatedAccountSnapshot(
            mask_account_id(account_identifier), balances, tuple(orders), (), (), (),
            vault.source_status,
            AuthenticatedSourceStatus(
                "available_empty" if order_status.records_status == "available_empty" else order_status.status,
                order_status.endpoint_name, order_status.observed_at,
                order_status.raw_status_name, order_status.reason, order_status.error_code,
                order_status.pagination_complete, order_status.duplicate_count, order_status.malformed_count,
                response_body_status=order_status.response_body_status,
                schema_status=order_status.schema_status,
                records_status=order_status.records_status,
                pagination_status=order_status.pagination_status,
                authority_status=order_status.authority_status,
            ),
            recent_status, fills_status, commissions_status, observed,
        )

    def _build_identity_binding_evidence(
        self,
        *,
        owner_diagnostics: AddressReadOnlyDiagnostics,
        trading_diagnostics: AddressReadOnlyDiagnostics | None,
        authenticated: AuthenticatedAccountSnapshot,
        queried_address: str | None,
    ) -> DreamDexIdentityBindingEvidence:
        trading_address = self.trading_address
        query_match = "yes" if trading_address and queried_address == trading_address else "unresolved"
        if trading_address and queried_address and queried_address != trading_address:
            query_match = "no"
        vault_status = authenticated.balances_status.status
        order_status = authenticated.open_orders_status.status
        probe_available = vault_status in {"available", "valid_confirmed_schema"}
        order_probe_available = order_status in {"available", "available_empty", "valid_confirmed_schema"}
        role_declared = self.trading_platform_role != "unknown"
        if query_match == "no":
            binding_status = "conflicting"
        elif role_declared and probe_available and order_probe_available:
            binding_status = "observed"
        elif role_declared or probe_available or order_probe_available:
            binding_status = "observed"
        else:
            binding_status = "unavailable"
        token_subject_status = "unresolved"
        auth_state = None
        if self.auth_manager is not None:
            token_subject_status = str(getattr(self.auth_manager.identity, "identity_status", "unresolved"))
            auth_state = getattr(self.auth_manager.snapshot(), "state", None)
            if auth_state == "authenticated":
                token_subject_status = "observed"
        if token_subject_status not in EVIDENCE_LEVELS:
            token_subject_status = "unavailable"
        reasons: list[str] = [
            "official_mapping_unconfirmed",
            "authenticated_subject_wallet_binding_unconfirmed",
            "role_does_not_prove_ownership",
        ]
        if not role_declared:
            reasons.append("trading_platform_role_not_declared")
        if query_match == "no":
            reasons.append("authenticated_query_address_mismatch")
        if not probe_available:
            reasons.append("authenticated_vault_probe_unavailable")
        if not order_probe_available:
            reasons.append("authenticated_order_probe_unavailable")
        sources = ["eth_getCode"]
        if role_declared:
            sources.append("local_platform_role_declaration")
        if probe_available:
            sources.append("authenticated_vault_probe")
        if order_probe_available:
            sources.append("authenticated_order_probe")
        if auth_state == "authenticated":
            sources.append("successful_authenticated_login_workflow")
        ui_confirmation = "user_confirmed" if role_declared else "unavailable"
        return DreamDexIdentityBindingEvidence(
            owner_address=self.owner,
            trading_address=trading_address,
            owner_platform_role=self.owner_platform_role,
            trading_platform_role=self.trading_platform_role,
            owner_onchain_code_type=owner_diagnostics.onchain_code_type,
            trading_onchain_code_type=trading_diagnostics.onchain_code_type if trading_diagnostics else "unavailable",
            ui_role_confirmation=ui_confirmation,
            authenticated_vault_probe_status=vault_status,
            authenticated_order_probe_status=order_status,
            authenticated_query_address=queried_address,
            token_subject_status=token_subject_status,
            official_mapping_status="unavailable",
            binding_status=binding_status,
            authoritative=False,
            evidence_sources=tuple(sources),
            unresolved_reasons=tuple(dict.fromkeys(reasons)),
            observed_at=self.clock(),
            authenticated_query_address_match=query_match,
        )

    def fetch_snapshot(self) -> ReadOnlySnapshot:
        market = self.fetch_market()
        metadata = market.metadata
        required = {
            "pool_contract": metadata.pool_contract,
            "base_token_address": metadata.base_token_address,
            "quote_token_address": metadata.quote_token_address,
            "base_decimals": metadata.base_decimals,
            "quote_decimals": metadata.quote_decimals,
        }
        missing = [name for name, value in required.items() if value is None]
        if missing:
            raise ValueError(f"market metadata incomplete: missing {', '.join(missing)}")
        if metadata.base_decimals is None or metadata.quote_decimals is None:
            raise ValueError("market metadata incomplete: missing token decimals")
        base_asset = metadata.base_asset or self.symbol.split(":", 1)[0]
        quote_asset = metadata.quote_asset or self.symbol.split(":", 1)[1]
        orderbook_status = _orderbook_status(market.orderbook, self.clock())
        rpc_source = RpcAccountReadOnlySource(self._rpc_transport, account_address=self.trading_address, gas_address=self.owner, pool_address=metadata.pool_contract, base_token_address=metadata.base_token_address, quote_token_address=metadata.quote_token_address, base_decimals=metadata.base_decimals, quote_decimals=metadata.quote_decimals, base_asset_kind=metadata.base_asset_kind, quote_asset_kind=metadata.quote_asset_kind, clock=self.clock)
        owner_diagnostics = replace(
            rpc_source.fetch_address(self.owner),
            platform_role=self.owner_platform_role,
            platform_role_status=self._owner_platform_role_status,
        )
        trading_diagnostics = (
            replace(
                rpc_source.fetch_address(self.trading_address),
                platform_role=self.trading_platform_role,
                platform_role_status=self._trading_platform_role_status,
            )
            if self.trading_address else None
        )
        if self.trading_address:
            vault = VaultReadOnlySource(self._transport, self.symbol, base_asset, quote_asset, self.clock).fetch(self.trading_address)
        else:
            observed = self.clock()
            unresolved = lambda source: SourceValue(None, "unavailable", source, "trading_address_unresolved", observed, "unavailable")
            vault = VaultReadOnlySnapshot("", unresolved("vault_rest"), unresolved("vault_rest"), observed)
        if trading_diagnostics:
            rpc = RpcAccountReadOnlySnapshot(trading_diagnostics.address, trading_diagnostics.vault_base, trading_diagnostics.vault_quote, trading_diagnostics.wallet_base, trading_diagnostics.wallet_quote, owner_diagnostics.native_gas, self.clock(), self.owner)
        else:
            observed = self.clock()
            unavailable = lambda source: rpc_source._error(source, observed, "unavailable")
            rpc = RpcAccountReadOnlySnapshot("", unavailable("rpc_pool_vault"), unavailable("rpc_pool_vault"), unavailable("rpc_erc20_balance"), unavailable("rpc_erc20_balance"), owner_diagnostics.native_gas, observed, self.owner)
        authenticated = self._fetch_authenticated_account()
        auth_snapshot = self.auth_manager.snapshot() if self.auth_manager is not None else None
        onchain_fills = self.fill_event_indexer.fetch() if self.fill_event_indexer is not None else FillEventPage.unavailable()
        order_metadata_report = self.order_metadata_resolver.resolve_fills(onchain_fills.fills) if self.order_metadata_resolver is not None else OrderMetadataResolverReport.unavailable()
        if self.order_metadata_resolver is not None and order_metadata_report.authoritative:
            correlated_ids = {item.fill_id for item in order_metadata_report.correlations if item.status == "matched" and item.owner_match is True}
            updated_fills = tuple(replace(fill, account_match=True) if fill.fill_id in correlated_ids else fill for fill in onchain_fills.fills)
            onchain_fills = replace(onchain_fills, fills=updated_fills, source_status=replace(onchain_fills.source_status, account_match_status="matched"))
        onchain_account_match = onchain_fills.source_status.account_match_status
        onchain_authoritative = onchain_fills.source_status.authoritative and onchain_account_match == "matched"
        authenticated_authoritative = authenticated.authoritative_for(self.trading_address, now=self.clock())
        authenticated_balances = {item.asset: item for item in authenticated.balances}
        base_auth = authenticated_balances.get(base_asset)
        quote_auth = authenticated_balances.get(quote_asset)
        balances = {
            base_asset: BalanceSnapshot(base_asset, base_auth.total if base_auth else rpc.base_wallet.value, base_auth.available if base_auth else rpc.base_wallet.value, base_auth.locked if base_auth else None, base_auth.source_status.status if base_auth else rpc.base_wallet.status),
            quote_asset: BalanceSnapshot(quote_asset, quote_auth.total if quote_auth else rpc.quote_wallet.value, quote_auth.available if quote_auth else rpc.quote_wallet.value, quote_auth.locked if quote_auth else None, quote_auth.source_status.status if quote_auth else rpc.quote_wallet.status),
        }
        identity_binding_evidence = self._build_identity_binding_evidence(
            owner_diagnostics=owner_diagnostics,
            trading_diagnostics=trading_diagnostics,
            authenticated=authenticated,
            queried_address=self.trading_address or self.owner,
        )
        account = AccountSnapshot(
            self.trading_address or self.owner, balances, vault, rpc,
            "confirmed" if authenticated.open_orders_status.available else "source_unavailable",
            "confirmed" if authenticated.fills_status.available or onchain_authoritative else "source_unavailable",
            self.clock(), owner_address=self.owner, trading_address=self.trading_address,
            trading_address_status="available" if self.trading_address else "unresolved",
            orderbook_status=orderbook_status, vault_address_semantics="unresolved",
            owner_diagnostics=owner_diagnostics, trading_diagnostics=trading_diagnostics,
            account_address_semantics=(
                "observed_non_authoritative"
                if (
                    self.auth_manager is not None and not self.auth_manager.identity.authoritative
                ) or self.trading_platform_role != "unknown" or self.owner_platform_role != "unknown"
                else "resolved" if authenticated_authoritative or onchain_authoritative else "unresolved"
            ),
            authenticated=authenticated,
            onchain_fills=onchain_fills,
            order_metadata_report=order_metadata_report,
            authenticated_transport_status=(
                "unconfigured" if isinstance(self.authenticated_transport, UnconfiguredAuthenticatedReadOnlyTransport)
                else str(getattr(self.authenticated_transport, "configuration_status", "configured" if getattr(self.authenticated_transport, "configured", True) else "unconfigured"))
            ),
            authenticated_request_execution_enabled=bool(getattr(self.authenticated_transport, "request_execution_enabled", False)),
            authenticated_order_by_id_status=(
                "not_requested" if bool(getattr(self.authenticated_transport, "configured", False))
                else str(getattr(self.authenticated_transport, "configuration_status", "unconfigured"))
            ),
            authenticated_schema_fingerprint_status=(
                "observed" if any((
                    self._last_authenticated_vault_fingerprint,
                    self._last_authenticated_order_list_fingerprint,
                    self._last_authenticated_order_by_id_fingerprint,
                ))
                else "unavailable"
            ),
            authenticated_vault_fingerprint=self._last_authenticated_vault_fingerprint,
            authenticated_order_list_fingerprint=self._last_authenticated_order_list_fingerprint,
            authenticated_order_by_id_fingerprint=self._last_authenticated_order_by_id_fingerprint,
            auth_snapshot=auth_snapshot,
            identity_binding_evidence=identity_binding_evidence,
        )
        return ReadOnlySnapshot(market.metadata, account, self.clock(), "markets+orderbook+trades+vault_rest+somnia_rpc+onchain_order_filled", market.orderbook, market.recent_trades, owner_diagnostics, trading_diagnostics, onchain_fills)

    def fetch_account_snapshot(self) -> AccountSnapshot:
        return self.fetch_snapshot().account

    def reconcile(self, snapshot: ReadOnlySnapshot, *, local_cash: Decimal | None = None, local_inventory: Decimal | None = None) -> ReconciliationReport:
        account = snapshot.account
        quote, base = snapshot.market.quote_asset or "USDso", snapshot.market.base_asset or "SOMI"
        authenticated_authoritative = account.authenticated.authoritative_for(account.trading_address)
        authenticated_balances = {item.asset: item for item in account.authenticated.balances}
        auth_quote = authenticated_balances.get(quote)
        auth_base = authenticated_balances.get(base)
        exchange_cash = auth_quote.available if authenticated_authoritative and auth_quote else (
            account.vault_rpc.quote_vault.value if account.account_address_semantics == "resolved" and account.vault_rpc.quote_vault.available else None
        )
        exchange_inventory = auth_base.available if authenticated_authoritative and auth_base else (
            account.vault_rpc.base_vault.value if account.account_address_semantics == "resolved" and account.vault_rpc.base_vault.available else None
        )
        mismatches: list[str] = []
        if account.incomplete:
            mismatches.append("incomplete_account_state")
        if account.account_address_semantics != "resolved":
            mismatches.append("authoritative_account_address_unresolved")
        if account.trading_address_status != "available":
            mismatches.append("trading_address_unresolved")
        if account.orderbook_status != "available":
            mismatches.append("orderbook_unavailable")
        if not authenticated_authoritative:
            mismatches.append("authenticated_account_state_unavailable")
        if not authenticated_authoritative and (not account.vault_rest.available or not account.vault_rpc.base_vault.available or not account.vault_rpc.quote_vault.available):
            mismatches.append("balance_source_unavailable")
        if not authenticated_authoritative and account.vault_rest.base.available and account.vault_rpc.base_vault.available and account.vault_rest.base.value != account.vault_rpc.base_vault.value:
            mismatches.append("base_vault_mismatch")
        if not authenticated_authoritative and account.vault_rest.quote.available and account.vault_rpc.quote_vault.available and account.vault_rest.quote.value != account.vault_rpc.quote_vault.value:
            mismatches.append("quote_vault_mismatch")
        if (
            account.open_orders_status == "source_unavailable"
            or not account.authenticated.open_orders_status.available
            or not account.authenticated.open_orders_status.pagination_complete
        ):
            mismatches.append("incomplete_open_orders_source")
        onchain_fills_authoritative = account.onchain_fills.source_status.authoritative and account.onchain_fills.source_status.account_match_status == "matched"
        if (account.fills_status == "source_unavailable" or not account.authenticated.fills_status.available) and not onchain_fills_authoritative:
            mismatches.append("incomplete_fills_source")
        if account.onchain_fills.source_status.status == "unavailable" and self.fill_event_indexer is not None:
            mismatches.append("onchain_fills_unavailable")
        if account.onchain_fills.source_status.reorg_status == "reorg_detected":
            mismatches.append("reorg_detected")
        if account.onchain_fills.source_status.malformed_count:
            mismatches.append("malformed_onchain_fill_logs")
        if self.fill_event_indexer is not None and account.account_address_semantics != "resolved" and account.onchain_fills.source_status.account_match_status != "matched":
            mismatches.append("authoritative_account_address_unresolved")
        if self.order_metadata_resolver is not None and not account.order_metadata_report.authoritative:
            mismatches.append(account.order_metadata_report.reason or "order_metadata_unavailable")
        if local_cash is not None and exchange_cash is not None and local_cash != exchange_cash: mismatches.append("cash_mismatch")
        if local_inventory is not None and exchange_inventory is not None and local_inventory != exchange_inventory: mismatches.append("inventory_mismatch")
        complete = not mismatches
        return ReconciliationReport(complete, bool(mismatches), ";".join(mismatches) if mismatches else "reconciled", local_cash, exchange_cash, None if local_cash is None or exchange_cash is None else exchange_cash - local_cash, local_inventory, exchange_inventory, None if local_inventory is None or exchange_inventory is None else exchange_inventory - local_inventory, account.vault_rest.base, account.vault_rpc.base_vault, account.vault_rest.quote, account.vault_rpc.quote_vault, tuple(mismatches), snapshot.observed_at)


def load_fixture(path: str | Path) -> Mapping[str, Any]:
    with Path(path).open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, Mapping): raise ValueError("fixture must be an object")
    return value


class FixtureTransport:
    is_fixture = True

    def __init__(self, fixture: Mapping[str, Any]) -> None: self.fixture, self.paths = fixture, []
    def get(self, path: str, *, params: Mapping[str, str] | None = None) -> Any:
        self.paths.append((path, dict(params or {})))
        if path == "/markets": return self.fixture.get("markets", [])
        if path.startswith("/orderbooks"): return self.fixture.get("orderbook", {})
        if "/trades" in path: return self.fixture.get("trades", [])
        if "/vault/balance" in path: return self.fixture.get("vault_rest", {})
        raise RuntimeError("unavailable fixture route")


class FixtureRpcTransport:
    def __init__(self, fixture: Mapping[str, Any]) -> None: self.fixture, self.calls, self._vault_index, self._wallet_index = fixture, [], 0, 0

    def _market(self) -> Mapping[str, Any]:
        rows = _rows(self.fixture.get("markets", []), "markets", "items")
        return rows[0] if rows else {}

    def call(self, method: str, params: Sequence[Any]) -> Any:
        self.calls.append((method, params))
        if method == "eth_getCode":
            address = str(params[0]).lower() if params else ""
            values = self.fixture.get("rpc", {})
            codes = values.get("code_by_address", values.get("codes", {})) if isinstance(values, Mapping) else {}
            return codes.get(address, codes.get(address.removeprefix("0x"), values.get("code", "0x"))) if isinstance(codes, Mapping) else values.get("code", "0x")
        if method == "eth_getBalance":
            address = str(params[0]).lower() if params else ""
            values = self.fixture.get("rpc", {})
            if isinstance(values, Mapping):
                by_address = values.get("native_balance_by_address", values.get("balance_by_address", {}))
                if isinstance(by_address, Mapping):
                    selected = by_address.get(address, by_address.get(address.removeprefix("0x")))
                    if selected is not None:
                        return selected
            return self.fixture.get("native_gas")
        values = self.fixture.get("rpc", {})
        data = params[0].get("data", "") if params and isinstance(params[0], Mapping) else ""
        if data[2:10] == _selector("getWithdrawableBalance(address,address)"):
            token_word = data[-40:].lower() if len(data) >= 40 else ""
            base_token = str(self._market().get("base", "")).lower().removeprefix("0x")
            value = values.get("base_vault") if token_word == base_token else values.get("quote_vault")
            self._vault_index += 1
            return value
        if data[2:10] == _selector("balanceOf(address)"):
            target = str(params[0].get("to", "")).lower() if params and isinstance(params[0], Mapping) else ""
            base_token = str(self._market().get("base", "")).lower()
            value = values.get("base_wallet") if target == base_token else values.get("quote_wallet")
            self._wallet_index += 1
            return value
        if data[2:10] == _selector("decimals()"):
            target = str(params[0].get("to", "")).lower() if params and isinstance(params[0], Mapping) else ""
            market = self._market()
            base_token = str(market.get("base", "")).lower()
            return hex(int(market.get("baseDecimals", 18) if target == base_token else market.get("quoteDecimals", 18)))
        raise RuntimeError("unsupported fixture RPC call")


def _selector(signature: str) -> str:
    from eth_utils import keccak
    return keccak(text=signature)[:4].hex()


ReadOnlyMarketMetadata = MarketMetadata
ReadOnlyAccountSnapshot = AccountSnapshot
ReadOnlyReconciliationReport = ReconciliationReport
DreamDexReadOnlyClient = DreamDexReadOnlyAdapter

__all__ = ["AccountSnapshot", "AddressReadOnlyDiagnostics", "AssetKind", "BalanceSnapshot", "DreamDexIdentityBindingEvidence", "DreamDexMarketTradingRules", "DreamDexReadOnlyAdapter", "DreamDexReadOnlyClient", "EVIDENCE_LEVELS", "FixtureRpcTransport", "FixtureTransport", "HttpGetTransport", "HttpRpcTransport", "MarketMetadata", "MarketReadOnlySnapshot", "MarketReadOnlySource", "NATIVE_SENTINEL", "ONCHAIN_CODE_TYPES", "PLATFORM_ROLES", "PublicMarketSchemaFingerprint", "ReadOnlyAccountSnapshot", "ReadOnlyMarketMetadata", "ReadOnlyReconciliationReport", "ReadOnlySnapshot", "ReconciliationReport", "RpcAccountReadOnlySnapshot", "RpcAccountReadOnlySource", "SourceValue", "TokenReadOnlyDiagnostics", "VaultReadOnlySnapshot", "VaultReadOnlySource", "load_fixture", "mask_account_id", "parse_market_trading_rules"]
