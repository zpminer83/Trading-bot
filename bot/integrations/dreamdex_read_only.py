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
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence
import json
import os
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


def mask_account_id(value: str | None) -> str:
    if not value:
        return "<missing>"
    text = str(value)
    return "***" if len(text) <= 8 else f"{text[:4]}…{text[-4:]}"


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
    """JSON-RPC transport allowing only ``eth_call`` and ``eth_getBalance``."""

    ALLOWED_METHODS = frozenset({"eth_call", "eth_getBalance", "eth_chainId", "eth_blockNumber"})

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
    value: Decimal | None
    status: str
    source: str
    reason: str | None = None
    observed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

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

    @property
    def active(self) -> bool:
        return (self.status or "").lower() in {"active", "enabled", "online", "trading"}

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
        )

    def orderbook(self) -> Mapping[str, Any]:
        payload = self._get("/orderbooks", {"symbols": self.symbol})
        rows = _rows(payload, "orderbooks", "data")
        return rows[0] if rows else (payload[0] if isinstance(payload, list) and payload else payload)

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
            reason = f"unavailable:{status or type(exc).__name__}"
            return VaultReadOnlySnapshot(owner, SourceValue(None, "unavailable", "vault_rest", reason, observed), SourceValue(None, "unavailable", "vault_rest", reason, observed), observed)

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
    return SourceValue(parsed, "available" if parsed is not None else "unavailable", source, None if parsed is not None else "asset_missing", observed)


@dataclass(frozen=True)
class RpcAccountReadOnlySnapshot:
    owner: str
    base_vault: SourceValue
    quote_vault: SourceValue
    base_wallet: SourceValue
    quote_wallet: SourceValue
    native_gas: SourceValue
    observed_at: datetime


class RpcAccountReadOnlySource:
    def __init__(self, transport: RpcTransport | Callable[..., Any], *, owner: str, pool_address: str, base_token_address: str, quote_token_address: str, base_decimals: int = 18, quote_decimals: int = 18, clock: Callable[[], datetime] | None = None) -> None:
        self.transport, self.owner = transport, owner
        self.pool_address, self.base_token_address, self.quote_token_address = pool_address, base_token_address, quote_token_address
        self.base_decimals, self.quote_decimals = base_decimals, quote_decimals
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

    def _view(self, method: str, params: Sequence[Any], source: str, observed: datetime, decimals: int = 0) -> SourceValue:
        try:
            result = self._call(method, params)
            raw = int(str(result), 16) if isinstance(result, str) and result.startswith("0x") else result
            parsed = _dec(raw)
            if parsed is not None and decimals:
                parsed = parsed / (Decimal(10) ** decimals)
            return SourceValue(parsed, "available" if parsed is not None else "unavailable", source, None if parsed is not None else "invalid_result", observed)
        except Exception as exc:
            return SourceValue(None, "unavailable", source, f"{type(exc).__name__}", observed)

    def fetch(self) -> RpcAccountReadOnlySnapshot:
        observed = self.clock()
        base_vault = self._view("eth_call", [{"to": self.pool_address, "data": self._calldata("getWithdrawableBalance(address,address)", self.owner, self.base_token_address)}, "latest"], "rpc_pool_vault", observed, self.base_decimals)
        quote_vault = self._view("eth_call", [{"to": self.pool_address, "data": self._calldata("getWithdrawableBalance(address,address)", self.owner, self.quote_token_address)}, "latest"], "rpc_pool_vault", observed, self.quote_decimals)
        base_wallet = self._view("eth_call", [{"to": self.base_token_address, "data": self._calldata("balanceOf(address)", self.owner)}, "latest"], "rpc_erc20_balance", observed, self.base_decimals)
        quote_wallet = self._view("eth_call", [{"to": self.quote_token_address, "data": self._calldata("balanceOf(address)", self.owner)}, "latest"], "rpc_erc20_balance", observed, self.quote_decimals)
        native_gas = self._view("eth_getBalance", [self.owner, "latest"], "rpc_native_balance", observed)
        return RpcAccountReadOnlySnapshot(self.owner, base_vault, quote_vault, base_wallet, quote_wallet, native_gas, observed)

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

    def balance(self, asset: str) -> BalanceSnapshot:
        return self.balances.get(asset, BalanceSnapshot(asset, None, None, None, "source_unavailable"))

    @property
    def incomplete(self) -> bool:
        return not all((self.vault_rest.available, self.vault_rpc.base_vault.available, self.vault_rpc.quote_vault.available, self.vault_rpc.base_wallet.available, self.vault_rpc.quote_wallet.available, self.vault_rpc.native_gas.available, self.open_orders_status != "source_unavailable", self.fills_status != "source_unavailable"))

    def safe_dict(self) -> dict[str, Any]:
        return {"account_identifier": mask_account_id(self.account_identifier), "balances": {key: {"total": str(value.total), "available": str(value.available), "locked": str(value.locked), "status": value.status} for key, value in self.balances.items()}, "open_orders_status": self.open_orders_status, "fills_status": self.fills_status, "observed_at": self.observed_at.isoformat(), "source": self.source}


@dataclass(frozen=True)
class ReadOnlySnapshot:
    market: MarketMetadata
    account: AccountSnapshot
    observed_at: datetime
    source: str
    orderbook: Mapping[str, Any] | None = None
    recent_trades: tuple[Mapping[str, Any], ...] = ()


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

    def __init__(self, *, transport: ReadOnlyTransport | Callable[..., Any], rpc_transport: RpcTransport | Callable[..., Any], owner: str, symbol: str = "SOMI:USDso", clock: Callable[[], datetime] | None = None) -> None:
        if not owner or not str(owner).strip():
            raise ValueError("owner is required")
        if not symbol or ":" not in symbol:
            raise ValueError("market symbol must be BASE:QUOTE")
        self.owner, self.symbol, self.clock = str(owner), symbol, clock or (lambda: datetime.now(timezone.utc))
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
        vault = VaultReadOnlySource(self._transport, self.symbol, metadata.base_asset or self.symbol.split(":", 1)[0], metadata.quote_asset or self.symbol.split(":", 1)[1], self.clock).fetch(self.owner)
        rpc = RpcAccountReadOnlySource(self._rpc_transport, owner=self.owner, pool_address=metadata.pool_contract, base_token_address=metadata.base_token_address, quote_token_address=metadata.quote_token_address, base_decimals=metadata.base_decimals, quote_decimals=metadata.quote_decimals, clock=self.clock).fetch()
        balances = {metadata.base_asset: BalanceSnapshot(metadata.base_asset, rpc.base_wallet.value, rpc.base_wallet.value, None, rpc.base_wallet.status), metadata.quote_asset: BalanceSnapshot(metadata.quote_asset, rpc.quote_wallet.value, rpc.quote_wallet.value, None, rpc.quote_wallet.status)}
        account = AccountSnapshot(self.owner, balances, vault, rpc, "source_unavailable", "source_unavailable", self.clock())
        return ReadOnlySnapshot(market.metadata, account, self.clock(), "markets+orderbook+trades+vault_rest+somnia_rpc", market.orderbook, market.recent_trades)

    def fetch_account_snapshot(self) -> AccountSnapshot:
        return self.fetch_snapshot().account

    def reconcile(self, snapshot: ReadOnlySnapshot, *, local_cash: Decimal | None = None, local_inventory: Decimal | None = None) -> ReconciliationReport:
        account = snapshot.account
        quote, base = snapshot.market.quote_asset or "USDso", snapshot.market.base_asset or "SOMI"
        exchange_cash = account.vault_rpc.quote_vault.value if account.vault_rpc.quote_vault.available else None
        exchange_inventory = account.vault_rpc.base_vault.value if account.vault_rpc.base_vault.available else None
        mismatches: list[str] = []
        if account.incomplete:
            mismatches.append("incomplete_account_state")
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
    def call(self, method: str, params: Sequence[Any]) -> Any:
        self.calls.append((method, params))
        if method == "eth_getBalance": return self.fixture.get("native_gas")
        values = self.fixture.get("rpc", {})
        data = params[0].get("data", "") if params and isinstance(params[0], Mapping) else ""
        if data[2:10] == _selector("getWithdrawableBalance(address,address)"):
            value = values.get("base_vault") if self._vault_index == 0 else values.get("quote_vault")
            self._vault_index += 1
            return value
        if data[2:10] == _selector("balanceOf(address)"):
            value = values.get("base_wallet") if self._wallet_index == 0 else values.get("quote_wallet")
            self._wallet_index += 1
            return value
        raise RuntimeError("unsupported fixture RPC call")


def _selector(signature: str) -> str:
    from eth_utils import keccak
    return keccak(text=signature)[:4].hex()


ReadOnlyMarketMetadata = MarketMetadata
ReadOnlyAccountSnapshot = AccountSnapshot
ReadOnlyReconciliationReport = ReconciliationReport
DreamDexReadOnlyClient = DreamDexReadOnlyAdapter

__all__ = ["AccountSnapshot", "BalanceSnapshot", "DreamDexReadOnlyAdapter", "DreamDexReadOnlyClient", "FixtureRpcTransport", "FixtureTransport", "HttpGetTransport", "HttpRpcTransport", "MarketMetadata", "MarketReadOnlySnapshot", "MarketReadOnlySource", "ReadOnlyAccountSnapshot", "ReadOnlyMarketMetadata", "ReadOnlyReconciliationReport", "ReadOnlySnapshot", "ReconciliationReport", "RpcAccountReadOnlySnapshot", "RpcAccountReadOnlySource", "SourceValue", "VaultReadOnlySnapshot", "VaultReadOnlySource", "load_fixture", "mask_account_id"]
