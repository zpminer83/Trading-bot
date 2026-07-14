"""Confirmed DreamDEX read-only market, vault and RPC sources.

The REST routes mirror the public Bot Kit client. Account state is not fetched
from an invented account REST resource: balances come from the documented vault
route and read-only Somnia RPC calls. Open orders and fills remain explicitly
unavailable until a confirmed route is provided.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence
import json
import os
import re
from urllib.parse import quote


def _dec(value: Any, default: Decimal | None = None) -> Decimal | None:
    if value is None or value == "":
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
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"invalid {primary}")
    if parsed < 0:
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

    @property
    def active(self) -> bool:
        return (self.status or "").lower() in {"active", "enabled", "online", "trading"}

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
        return _rows(self._get("/markets"), "markets", "items")

    def metadata(self) -> MarketMetadata:
        row = next((item for item in self.markets() if str(_first(item, "symbol", "market", default="")) == self.symbol), None)
        if row is None:
            raise ValueError(f"market {self.symbol} was not found")
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
        return MarketMetadata(
            symbol=self.symbol,
            base_asset=base_asset,
            quote_asset=quote_asset,
            base_token_address=base_address,
            quote_token_address=quote_address,
            pool_contract=pool_contract,
            price_tick_size=_dec(_first(row, "tickSize", "tick_size", "priceTickSize")),
            quantity_step_size=_dec(_first(row, "quantityStepSize", "quantity_step_size", "lotSize", "stepSize")),
            minimum_quantity=_dec(_first(row, "minimumQuantity", "minQuantity", "min_quantity")),
            minimum_notional=_dec(_first(row, "minimumNotional", "minNotional", "minimum_notional")),
            status=_first(row, "status", "marketStatus"),
            supported_order_types=tuple(str(item) for item in (types or ())),
            maker_fee=_dec(_first(row, "makerFee", "maker_fee", "makerFeeBps")),
            taker_fee=_dec(_first(row, "takerFee", "taker_fee", "takerFeeBps")),
            observed_at=_utc(_first(row, "timestamp", "updatedAt", "updated_at"), self.clock()),
            base_decimals=base_decimals,
            quote_decimals=quote_decimals,
            stop_registry=_first(row, "stopRegistry", "stop_registry"),
            base_asset_kind=_asset_kind(self.symbol, base_address, side="base"),
            quote_asset_kind=_asset_kind(self.symbol, quote_address, side="quote"),
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


@dataclass(frozen=True)
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

    def balance(self, asset: str) -> BalanceSnapshot:
        return self.balances.get(asset, BalanceSnapshot(asset, None, None, None, "source_unavailable"))

    @property
    def incomplete(self) -> bool:
        return not all((self.account_address_semantics == "resolved", self.trading_address_status == "available", self.orderbook_status == "available", self.vault_rest.available, self.vault_rpc.base_vault.available, self.vault_rpc.quote_vault.available, self.vault_rpc.base_wallet.available, self.vault_rpc.quote_wallet.available, self.vault_rpc.native_gas.available, self.open_orders_status != "source_unavailable", self.fills_status != "source_unavailable"))

    def safe_dict(self) -> dict[str, Any]:
        return {"account_identifier": mask_account_id(self.account_identifier), "owner_address": mask_account_id(self.owner_address), "trading_address": mask_account_id(self.trading_address), "trading_address_status": self.trading_address_status, "account_address_semantics": self.account_address_semantics, "balances": {key: {"total": str(value.total), "available": str(value.available), "locked": str(value.locked), "status": value.status} for key, value in self.balances.items()}, "open_orders_status": self.open_orders_status, "fills_status": self.fills_status, "observed_at": self.observed_at.isoformat(), "source": self.source}


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

    def __init__(self, *, transport: ReadOnlyTransport | Callable[..., Any], rpc_transport: RpcTransport | Callable[..., Any], owner: str, trading_address: str | None = None, symbol: str = "SOMI:USDso", clock: Callable[[], datetime] | None = None) -> None:
        if not owner or not str(owner).strip() or not _is_address(owner):
            raise ValueError("owner must be a public address")
        if trading_address is not None and not _is_address(trading_address):
            raise ValueError("trading address must be a public address")
        if not symbol or ":" not in symbol:
            raise ValueError("market symbol must be BASE:QUOTE")
        self.owner, self.trading_address, self.symbol, self.clock = str(owner), (str(trading_address) if trading_address else None), symbol, clock or (lambda: datetime.now(timezone.utc))
        self.market_source = MarketReadOnlySource(transport, symbol, self.clock)
        self._transport, self._rpc_transport = transport, rpc_transport

    def fetch_market(self) -> MarketReadOnlySnapshot:
        return self.market_source.snapshot()

    fetch_market_metadata = lambda self: self.market_source.metadata()

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
        owner_diagnostics = rpc_source.fetch_address(self.owner)
        trading_diagnostics = rpc_source.fetch_address(self.trading_address) if self.trading_address else None
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
        balances = {base_asset: BalanceSnapshot(base_asset, rpc.base_wallet.value, rpc.base_wallet.value, None, rpc.base_wallet.status), quote_asset: BalanceSnapshot(quote_asset, rpc.quote_wallet.value, rpc.quote_wallet.value, None, rpc.quote_wallet.status)}
        account = AccountSnapshot(self.trading_address or self.owner, balances, vault, rpc, "source_unavailable", "source_unavailable", self.clock(), owner_address=self.owner, trading_address=self.trading_address, trading_address_status="available" if self.trading_address else "unresolved", orderbook_status=orderbook_status, vault_address_semantics="unresolved", owner_diagnostics=owner_diagnostics, trading_diagnostics=trading_diagnostics, account_address_semantics="unresolved")
        return ReadOnlySnapshot(market.metadata, account, self.clock(), "markets+orderbook+trades+vault_rest+somnia_rpc", market.orderbook, market.recent_trades, owner_diagnostics, trading_diagnostics)

    def fetch_account_snapshot(self) -> AccountSnapshot:
        return self.fetch_snapshot().account

    def reconcile(self, snapshot: ReadOnlySnapshot, *, local_cash: Decimal | None = None, local_inventory: Decimal | None = None) -> ReconciliationReport:
        account = snapshot.account
        quote, base = snapshot.market.quote_asset or "USDso", snapshot.market.base_asset or "SOMI"
        # Neither login owner nor profile trading address is authoritative yet.
        exchange_cash = account.vault_rpc.quote_vault.value if account.account_address_semantics == "resolved" and account.vault_rpc.quote_vault.available else None
        exchange_inventory = account.vault_rpc.base_vault.value if account.account_address_semantics == "resolved" and account.vault_rpc.base_vault.available else None
        mismatches: list[str] = []
        if account.incomplete:
            mismatches.append("incomplete_account_state")
        if account.account_address_semantics != "resolved":
            mismatches.append("authoritative_account_address_unresolved")
        if account.trading_address_status != "available":
            mismatches.append("trading_address_unresolved")
        if account.orderbook_status != "available":
            mismatches.append("orderbook_unavailable")
        if not account.vault_rest.available or not account.vault_rpc.base_vault.available or not account.vault_rpc.quote_vault.available:
            mismatches.append("balance_source_unavailable")
        if account.vault_rest.base.available and account.vault_rpc.base_vault.available and account.vault_rest.base.value != account.vault_rpc.base_vault.value:
            mismatches.append("base_vault_mismatch")
        if account.vault_rest.quote.available and account.vault_rpc.quote_vault.available and account.vault_rest.quote.value != account.vault_rpc.quote_vault.value:
            mismatches.append("quote_vault_mismatch")
        if account.open_orders_status == "source_unavailable": mismatches.append("incomplete_open_orders_source")
        if account.fills_status == "source_unavailable": mismatches.append("incomplete_fills_source")
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

__all__ = ["AccountSnapshot", "AddressReadOnlyDiagnostics", "AssetKind", "BalanceSnapshot", "DreamDexReadOnlyAdapter", "DreamDexReadOnlyClient", "FixtureRpcTransport", "FixtureTransport", "HttpGetTransport", "HttpRpcTransport", "MarketMetadata", "MarketReadOnlySnapshot", "MarketReadOnlySource", "NATIVE_SENTINEL", "ReadOnlyAccountSnapshot", "ReadOnlyMarketMetadata", "ReadOnlyReconciliationReport", "ReadOnlySnapshot", "ReconciliationReport", "RpcAccountReadOnlySnapshot", "RpcAccountReadOnlySource", "SourceValue", "TokenReadOnlyDiagnostics", "VaultReadOnlySnapshot", "VaultReadOnlySource", "load_fixture", "mask_account_id"]
