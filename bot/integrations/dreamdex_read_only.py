"""Read-only DreamDEX state adapter.

The adapter intentionally exposes only GET-shaped operations.  It accepts an
injected transport for deterministic tests and fixtures; the optional HTTP
transport is also GET-only and never accepts credentials or auth headers.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence
import json
import os


def _decimal(value: Any, default: Decimal | None = None) -> Decimal | None:
    if value is None or value == "":
        return default
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return default


def _first(mapping: Mapping[str, Any], *names: str, default: Any = None) -> Any:
    for name in names:
        if name in mapping and mapping[name] is not None:
            return mapping[name]
    return default


def _list_payload(payload: Any, *keys: str) -> list[Mapping[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, Mapping)]
    if isinstance(payload, Mapping):
        if any(name in payload for name in ("symbol", "market", "orderId", "order_id", "fillId", "tradeId")):
            return [payload]
        for key in keys:
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, Mapping)]
        data = payload.get("data")
        if isinstance(data, list):
            return [item for item in data if isinstance(item, Mapping)]
        if isinstance(data, Mapping):
            return _list_payload(data, *keys)
    return []


def _timestamp(value: Any, fallback: datetime | None = None) -> datetime:
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, (int, float)):
        number = float(value)
        if number > 10_000_000_000:
            number /= 1000
        result = datetime.fromtimestamp(number, tz=timezone.utc)
    elif value:
        text = str(value).replace("Z", "+00:00")
        try:
            result = datetime.fromisoformat(text)
        except ValueError:
            result = fallback or datetime.now(timezone.utc)
    else:
        result = fallback or datetime.now(timezone.utc)
    if result.tzinfo is None or result.utcoffset() is None:
        return result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def mask_account_id(value: str | None) -> str:
    """Mask identifiers for human-facing output; never mask in comparisons."""
    if not value:
        return "<missing>"
    text = str(value)
    if len(text) <= 8:
        return "***"
    return f"{text[:4]}…{text[-4:]}"


class ReadOnlyTransport(Protocol):
    def get(self, path: str, *, params: Mapping[str, str] | None = None) -> Any:
        ...


class HttpGetTransport:
    """Minimal GET-only transport.  It cannot send auth or mutation methods."""

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


@dataclass(frozen=True)
class MarketMetadata:
    symbol: str
    base_asset: str | None
    quote_asset: str | None
    price_tick_size: Decimal | None
    quantity_step_size: Decimal | None
    minimum_quantity: Decimal | None
    minimum_notional: Decimal | None
    status: str | None
    supported_order_types: tuple[str, ...] = ()
    maker_fee: Decimal | None = None
    taker_fee: Decimal | None = None
    observed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    source: str = "unknown"

    @property
    def active(self) -> bool:
        return (self.status or "active").lower() in {"active", "enabled", "online", "trading"}

    def is_fresh(self, *, now: datetime | None = None, max_age_seconds: Decimal = Decimal("30")) -> bool:
        observed = _timestamp(self.observed_at)
        current = _timestamp(now)
        age = max(Decimal("0"), Decimal(str((current - observed).total_seconds())))
        return age <= max_age_seconds


@dataclass(frozen=True)
class BalanceSnapshot:
    asset: str
    total: Decimal
    available: Decimal
    locked: Decimal


@dataclass(frozen=True)
class OpenOrderSnapshot:
    order_id: str
    client_order_id: str | None
    symbol: str
    side: str | None
    price: Decimal | None
    quantity: Decimal | None
    remaining_quantity: Decimal | None
    status: str | None
    observed_at: datetime


@dataclass(frozen=True)
class RecentOrderSnapshot:
    order_id: str
    symbol: str
    status: str | None
    side: str | None
    price: Decimal | None
    quantity: Decimal | None
    observed_at: datetime


@dataclass(frozen=True)
class FillSnapshot:
    fill_id: str
    order_id: str | None
    symbol: str
    side: str | None
    price: Decimal | None
    quantity: Decimal | None
    notional: Decimal | None
    commission: Decimal | None
    observed_at: datetime


@dataclass(frozen=True)
class AccountSnapshot:
    account_identifier: str
    balances: Mapping[str, BalanceSnapshot]
    open_orders: tuple[OpenOrderSnapshot, ...]
    recent_orders: tuple[RecentOrderSnapshot, ...]
    recent_fills: tuple[FillSnapshot, ...]
    commissions: tuple[Mapping[str, Any], ...]
    observed_at: datetime
    source: str

    def balance(self, asset: str) -> BalanceSnapshot:
        return self.balances.get(
            asset,
            BalanceSnapshot(asset=asset, total=Decimal("0"), available=Decimal("0"), locked=Decimal("0")),
        )

    def safe_dict(self) -> dict[str, Any]:
        return {
            "account_identifier": mask_account_id(self.account_identifier),
            "balances": {
                asset: {"total": str(value.total), "available": str(value.available), "locked": str(value.locked)}
                for asset, value in self.balances.items()
            },
            "open_orders": len(self.open_orders),
            "recent_orders": len(self.recent_orders),
            "recent_fills": len(self.recent_fills),
            "commissions": len(self.commissions),
            "observed_at": self.observed_at.isoformat(),
            "source": self.source,
        }


@dataclass(frozen=True)
class ReadOnlySnapshot:
    market: MarketMetadata
    account: AccountSnapshot
    observed_at: datetime
    source: str


@dataclass(frozen=True)
class ReconciliationReport:
    completed: bool
    trading_blocked: bool
    reason: str
    local_cash: Decimal
    exchange_cash: Decimal
    cash_difference: Decimal
    local_inventory: Decimal
    exchange_inventory: Decimal
    inventory_difference: Decimal
    local_open_order_ids: tuple[str, ...]
    exchange_open_order_ids: tuple[str, ...]
    local_fill_ids: tuple[str, ...]
    exchange_fill_ids: tuple[str, ...]
    mismatches: tuple[str, ...]
    observed_at: datetime
    unresolved_orders: tuple[str, ...] = ()


class DreamDexReadOnlyAdapter:
    """Fetch and normalize public market plus account read snapshots."""

    def __init__(
        self,
        *,
        transport: ReadOnlyTransport | Callable[..., Any] | None = None,
        client: ReadOnlyTransport | Callable[..., Any] | None = None,
        base_url: str | None = None,
        account_identifier: str | None = None,
        account_id: str | None = None,
        wallet_address: str | None = None,
        market_symbol: str = "SOMI:USDso",
        source: str = "dreamdex-read-only",
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        account_identifier = account_identifier or account_id or wallet_address
        if not account_identifier or not str(account_identifier).strip():
            raise ValueError("account_identifier is required")
        self.account_identifier = str(account_identifier)
        self.market_symbol = market_symbol
        self.source = source
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._transport = transport or client or HttpGetTransport(base_url or os.environ.get("DREAMDEX_READ_ONLY_BASE_URL", ""))
        if isinstance(self._transport, HttpGetTransport) and not self._transport.base_url:
            raise ValueError("base_url is required for the HTTP read-only transport")

    def _get(self, path: str, *, params: Mapping[str, str] | None = None) -> Any:
        transport = self._transport
        if hasattr(transport, "get"):
            try:
                return transport.get(path, params=params)
            except TypeError:
                return transport.get(path)
        try:
            return transport(path, params=params)
        except TypeError:
            return transport(path)

    def fetch_market_metadata(self) -> MarketMetadata:
        payload = self._get("/markets")
        rows = _list_payload(payload, "markets", "items")
        row = next((item for item in rows if str(_first(item, "symbol", "market", default="")) == self.market_symbol), None)
        if row is None:
            raise ValueError(f"market {self.market_symbol} was not found")
        types = _first(row, "supportedOrderTypes", "supported_order_types", "orderTypes", default=())
        if isinstance(types, str):
            types = (types,)
        return MarketMetadata(
            symbol=str(_first(row, "symbol", "market", default=self.market_symbol)),
            base_asset=_first(row, "baseToken", "base_asset", "base", "baseAsset"),
            quote_asset=_first(row, "quoteToken", "quote_asset", "quote", "quoteAsset"),
            price_tick_size=_decimal(_first(row, "tickSize", "tick_size", "priceTickSize")),
            quantity_step_size=_decimal(_first(row, "quantityStepSize", "quantity_step_size", "lotSize", "lot_size", "stepSize")),
            minimum_quantity=_decimal(_first(row, "minimumQuantity", "minQuantity", "min_quantity")),
            minimum_notional=_decimal(_first(row, "minimumNotional", "minNotional", "minimum_notional")),
            status=str(_first(row, "status", "marketStatus", default="unknown")),
            supported_order_types=tuple(str(item) for item in (types or ())),
            maker_fee=_decimal(_first(row, "makerFee", "maker_fee", "makerFeeBps")),
            taker_fee=_decimal(_first(row, "takerFee", "taker_fee", "takerFeeBps")),
            observed_at=_timestamp(_first(row, "timestamp", "updatedAt", "updated_at"), self._clock()),
            source=self.source,
        )

    def fetch_account_snapshot(self) -> AccountSnapshot:
        account = self._get(f"/accounts/{self.account_identifier}")
        if not isinstance(account, Mapping):
            account = {}
        if isinstance(account.get("data"), Mapping):
            account = account["data"]
        # Some deployments expose the read-only resources separately.  These
        # are optional fallbacks; no mutation endpoint is ever attempted.
        def optional(path: str, current: Any, *keys: str) -> Any:
            if current not in (None, {}, []):
                return current
            try:
                return self._get(path)
            except Exception:
                return current

        balances_value = account.get("balances", account.get("assets", {}))
        balances_value = optional(f"/accounts/{self.account_identifier}/balances", balances_value, "balances", "assets")
        open_orders_value = account.get("openOrders", account.get("open_orders", []))
        open_orders_value = optional(f"/accounts/{self.account_identifier}/orders/open", open_orders_value, "orders", "openOrders")
        recent_orders_value = account.get("recentOrders", account.get("recent_orders", []))
        recent_orders_value = optional(f"/accounts/{self.account_identifier}/orders", recent_orders_value, "orders", "recentOrders")
        fills_value = account.get("recentFills", account.get("recent_fills", account.get("fills", [])))
        fills_value = optional(f"/accounts/{self.account_identifier}/fills", fills_value, "fills", "trades")
        commissions_value = account.get("commissions", [])
        commissions_value = optional(f"/accounts/{self.account_identifier}/commissions", commissions_value, "commissions")
        balances_raw = account.get("balances", account.get("assets", {}))
        balances_raw = balances_value
        balances: dict[str, BalanceSnapshot] = {}
        if isinstance(balances_raw, Mapping):
            balance_rows = [dict(value, asset=key) if isinstance(value, Mapping) else {"asset": key, "total": value} for key, value in balances_raw.items()]
        else:
            balance_rows = _list_payload(balances_raw, "balances", "assets")
        for row in balance_rows:
            asset = str(_first(row, "asset", "symbol", "currency", default=""))
            if not asset:
                continue
            total = _decimal(_first(row, "total", "balance", default="0"), Decimal("0")) or Decimal("0")
            available = _decimal(_first(row, "available", "free", default=total), total) or total
            locked = _decimal(_first(row, "locked", "hold", default=total - available), total - available) or (total - available)
            balances[asset] = BalanceSnapshot(asset, total, available, locked)
        observed = _timestamp(_first(account, "timestamp", "updatedAt", "updated_at"), self._clock())
        return AccountSnapshot(
            account_identifier=str(_first(account, "accountIdentifier", "account_id", "wallet", "address", default=self.account_identifier)),
            balances=balances,
            open_orders=tuple(self._parse_open_order(row, observed) for row in _list_payload(open_orders_value, "orders", "openOrders")),
            recent_orders=tuple(self._parse_recent_order(row, observed) for row in _list_payload(recent_orders_value, "orders", "recentOrders")),
            recent_fills=tuple(self._parse_fill(row, observed) for row in _list_payload(fills_value, "fills", "trades")),
            commissions=tuple(row for row in _list_payload(commissions_value, "commissions")),
            observed_at=observed,
            source=self.source,
        )

    def fetch_snapshot(self) -> ReadOnlySnapshot:
        observed = _timestamp(None, self._clock())
        market = self.fetch_market_metadata()
        account = self.fetch_account_snapshot()
        return ReadOnlySnapshot(market=market, account=account, observed_at=observed, source=self.source)

    def reconcile(
        self,
        snapshot: ReadOnlySnapshot,
        *,
        local_cash: Decimal = Decimal("0"),
        local_inventory: Decimal = Decimal("0"),
        local_open_order_ids: Sequence[str] = (),
        local_fill_ids: Sequence[str] = (),
    ) -> ReconciliationReport:
        quote = snapshot.market.quote_asset or "USDso"
        base = snapshot.market.base_asset or "SOMI"
        exchange_cash = snapshot.account.balance(quote).total
        exchange_inventory = snapshot.account.balance(base).total
        exchange_orders = tuple(order.order_id for order in snapshot.account.open_orders)
        exchange_fills = tuple(fill.fill_id for fill in snapshot.account.recent_fills)
        mismatches: list[str] = []
        if Decimal(str(local_cash)) != exchange_cash:
            mismatches.append("cash_mismatch")
        if Decimal(str(local_inventory)) != exchange_inventory:
            mismatches.append("inventory_mismatch")
        if set(local_open_order_ids) != set(exchange_orders):
            mismatches.append("open_orders_mismatch")
        if set(local_fill_ids) != set(exchange_fills):
            mismatches.append("fills_mismatch")
        return ReconciliationReport(
            completed=True,
            trading_blocked=bool(mismatches),
            reason=";".join(mismatches) if mismatches else "reconciled",
            local_cash=Decimal(str(local_cash)), exchange_cash=exchange_cash,
            cash_difference=exchange_cash - Decimal(str(local_cash)),
            local_inventory=Decimal(str(local_inventory)), exchange_inventory=exchange_inventory,
            inventory_difference=exchange_inventory - Decimal(str(local_inventory)),
            local_open_order_ids=tuple(str(item) for item in local_open_order_ids),
            exchange_open_order_ids=exchange_orders,
            local_fill_ids=tuple(str(item) for item in local_fill_ids), exchange_fill_ids=exchange_fills,
            mismatches=tuple(mismatches), observed_at=snapshot.observed_at,
            unresolved_orders=tuple(sorted(set(local_open_order_ids) ^ set(exchange_orders))) if "open_orders_mismatch" in mismatches else (),
        )

    @staticmethod
    def _parse_open_order(row: Mapping[str, Any], observed: datetime) -> OpenOrderSnapshot:
        return OpenOrderSnapshot(
            order_id=str(_first(row, "orderId", "order_id", "id", default="")),
            client_order_id=_first(row, "clientOrderId", "client_order_id"),
            symbol=str(_first(row, "symbol", "market", default="")), side=_first(row, "side"),
            price=_decimal(_first(row, "price", "limitPrice")), quantity=_decimal(_first(row, "quantity", "size")),
            remaining_quantity=_decimal(_first(row, "remainingQuantity", "remaining_quantity", "remaining")),
            status=_first(row, "status", "state"), observed_at=_timestamp(_first(row, "timestamp", "updatedAt"), observed),
        )

    @staticmethod
    def _parse_recent_order(row: Mapping[str, Any], observed: datetime) -> RecentOrderSnapshot:
        return RecentOrderSnapshot(
            order_id=str(_first(row, "orderId", "order_id", "id", default="")), symbol=str(_first(row, "symbol", "market", default="")),
            status=_first(row, "status", "state"), side=_first(row, "side"), price=_decimal(_first(row, "price")), quantity=_decimal(_first(row, "quantity", "size")), observed_at=_timestamp(_first(row, "timestamp", "updatedAt"), observed),
        )

    @staticmethod
    def _parse_fill(row: Mapping[str, Any], observed: datetime) -> FillSnapshot:
        price = _decimal(_first(row, "price", "fillPrice")); quantity = _decimal(_first(row, "quantity", "size", "filledQuantity"))
        notional = _decimal(_first(row, "notional", "quoteQuantity"), price * quantity if price is not None and quantity is not None else None)
        return FillSnapshot(
            fill_id=str(_first(row, "fillId", "tradeId", "fill_id", "id", default="")), order_id=_first(row, "orderId", "order_id"), symbol=str(_first(row, "symbol", "market", default="")), side=_first(row, "side"), price=price, quantity=quantity, notional=notional, commission=_decimal(_first(row, "commission", "fee")), observed_at=_timestamp(_first(row, "timestamp", "createdAt"), observed),
        )


def load_fixture(path: str | Path) -> Mapping[str, Any]:
    with Path(path).open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, Mapping):
        raise ValueError("read-only fixture must contain a JSON object")
    return payload


class FixtureTransport:
    """GET-only transport for offline snapshots."""

    def __init__(self, fixture: Mapping[str, Any]) -> None:
        self.fixture = fixture
        self.paths: list[str] = []

    def get(self, path: str, *, params: Mapping[str, str] | None = None) -> Any:
        self.paths.append(path)
        if path == "/markets":
            return self.fixture.get("markets", [])
        return self.fixture.get("account", {})


__all__ = [
    "AccountSnapshot", "BalanceSnapshot", "DreamDexReadOnlyAdapter", "FillSnapshot", "FixtureTransport",
    "HttpGetTransport", "MarketMetadata", "OpenOrderSnapshot", "ReadOnlySnapshot", "ReconciliationReport",
    "RecentOrderSnapshot", "ReadOnlyMarketMetadata", "ReadOnlyAccountSnapshot", "ReadOnlyReconciliationReport",
    "DreamDexReadOnlyClient", "mask_account_id", "load_fixture",
]

# Descriptive aliases keep the public API readable for callers that prefer
# explicit read-only names.
ReadOnlyMarketMetadata = MarketMetadata
ReadOnlyAccountSnapshot = AccountSnapshot
ReadOnlyReconciliationReport = ReconciliationReport
DreamDexReadOnlyClient = DreamDexReadOnlyAdapter
