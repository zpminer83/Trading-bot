"""Safe read-only DreamDEX state check (no order or transaction operations)."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import os
import re

from bot.execution.dry_run_order_validator import DryRunOrderValidator, DryRunValidationLimits
from bot.execution.order import OrderIntent
from bot.integrations.dreamdex_read_only import DreamDexReadOnlyAdapter, FixtureRpcTransport, FixtureTransport, load_fixture, mask_account_id


def _decimal_env(name: str, default: Decimal) -> Decimal:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return Decimal(raw)
    except (InvalidOperation, ValueError):
        raise ValueError(f"{name} must be a decimal")


def _safe_error(exc: Exception, owner: str | None = None) -> str:
    message = re.sub(r"(?i)(private[_ -]?key|seed[_ -]?phrase|api[_ -]?secret|authorization|bearer|signature)\s*[:=]\s*\S+", r"\1=<redacted>", str(exc))
    return message.replace(owner, mask_account_id(owner))[:300] if owner else message[:300]


def _source(value) -> str:
    if value is None:
        return "unavailable"
    return f"{value.status}: {value.value if value.value is not None else value.reason}"


def _masked(value: str | None) -> str:
    return mask_account_id(value) if value else "<unresolved>"


def _orderbook_timestamp(book) -> datetime | None:
    value = book.get("timestamp", book.get("updatedAt", book.get("updated_at"))) if isinstance(book, dict) else None
    if value is None:
        return None
    try:
        if isinstance(value, (int, float)):
            seconds = float(value) / (1000 if value > 10_000_000_000 else 1)
            return datetime.fromtimestamp(seconds, tz=timezone.utc)
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _print_report(snapshot, report, validation) -> None:
    market, account = snapshot.market, snapshot.account
    base, quote = market.base_asset or "SOMI", market.quote_asset or "USDso"
    print("READ-ONLY ACCOUNT CHECK")
    print(f"Owner/login address: {_masked(account.owner_address or account.account_identifier)}")
    print(f"Trading address: {_masked(account.trading_address)}")
    print(f"Trading address status: {account.trading_address_status}")
    print(f"Market: {market.symbol}")
    print(f"Pool address: {market.pool_address or '<unavailable>'}")
    print(f"Base token address: {market.base_token_address or '<unavailable>'}")
    print(f"Quote token address: {market.quote_token_address or '<unavailable>'}")
    print("Market metadata status: available")
    print(f"Market status: {market.status or 'unavailable'}")
    print(f"Tick size: {market.price_tick_size}; quantity step: {market.quantity_step_size}; minimum quantity: {market.minimum_quantity}; minimum notional: {market.minimum_notional if market.minimum_notional is not None else 'unavailable'}")
    book = snapshot.orderbook if isinstance(snapshot.orderbook, dict) else {}
    bids, asks = book.get("bids", []), book.get("asks", [])
    print(f"Orderbook source: {account.orderbook_status}")
    best_bid = bids[0].get("price", bids[0]) if bids and isinstance(bids[0], dict) else (bids[0] if bids else "<unavailable>")
    best_ask = asks[0].get("price", asks[0]) if asks and isinstance(asks[0], dict) else (asks[0] if asks else "<unavailable>")
    print(f"Best bid: {best_bid}")
    print(f"Best ask: {best_ask}")
    timestamp = _orderbook_timestamp(book)
    age = None if timestamp is None else max(Decimal("0"), Decimal(str((datetime.now(timezone.utc) - timestamp).total_seconds())))
    max_age = _decimal_env("DREAMDEX_READ_ONLY_MAX_MARKET_AGE_SECONDS", Decimal("30"))
    print(f"Orderbook timestamp: {timestamp.isoformat() if timestamp else '<unavailable>'}")
    print(f"Orderbook age: {age if age is not None else '<unavailable>'} seconds")
    print(f"Orderbook freshness: {'available' if account.orderbook_status == 'available' and age is not None and age <= max_age else 'unavailable'}")
    print("Wallet token balances:")
    wallet_address = _masked(account.trading_address)
    for asset in (base, quote):
        balance = account.balance(asset)
        print(f"  {asset} (address={wallet_address}): total={balance.total} available={balance.available} status={balance.status}")
    print("Vault balances REST:")
    print(f"  address semantics: {account.vault_address_semantics} ({_masked(account.trading_address)})")
    print(f"  {base}: {_source(account.vault_rest.base)}")
    print(f"  {quote}: {_source(account.vault_rest.quote)}")
    print("Vault balances RPC getWithdrawableBalance:")
    print(f"  address semantics: {account.vault_address_semantics} ({_masked(account.trading_address)})")
    print(f"  {base}: {_source(account.vault_rpc.base_vault)}")
    print(f"  {quote}: {_source(account.vault_rpc.quote_vault)}")
    print(f"Native gas balance (eth_getBalance, owner/login address={_masked(account.vault_rpc.gas_address or account.owner_address)}; normalized 18 decimals): {_source(account.vault_rpc.native_gas)}")
    print(f"Open-orders source status: {account.open_orders_status}")
    print(f"Fills source status: {account.fills_status}")
    print(f"Reconciliation complete: {'YES' if report.completed else 'NO'}")
    print(f"Hypothetical trading blocked reason: {report.reason if report.trading_blocked else ', '.join(validation.reasons) or 'none'}")
    print(f"Dry-run approved: {'YES' if validation.approved else 'NO'}")
    print(f"Dry-run reasons: {', '.join(validation.reasons) or 'none'}")
    print("Real submission enabled: NO")


def main() -> int:
    fixture_path = os.environ.get("DREAMDEX_READ_ONLY_FIXTURE")
    required = ["DREAMDEX_READ_ONLY_OWNER_ADDRESS"]
    if not fixture_path:
        required.extend(("DREAMDEX_READ_ONLY_BASE_URL", "DREAMDEX_READ_ONLY_RPC_URL"))
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        print("READ-ONLY ACCOUNT CHECK")
        print("Missing configuration: " + ", ".join(missing))
        print("No network request or order operation was attempted.")
        print("Real submission enabled: NO")
        return 2
    owner = os.environ["DREAMDEX_READ_ONLY_OWNER_ADDRESS"]
    try:
        symbol = os.environ.get("DREAMDEX_READ_ONLY_MARKET", "SOMI:USDso")
        if fixture_path:
            fixture = load_fixture(fixture_path)
            rest_transport, rpc_transport = FixtureTransport(fixture), FixtureRpcTransport(fixture)
        else:
            from bot.integrations.dreamdex_read_only import HttpGetTransport, HttpRpcTransport
            rest_transport, rpc_transport = HttpGetTransport(os.environ["DREAMDEX_READ_ONLY_BASE_URL"]), HttpRpcTransport(os.environ["DREAMDEX_READ_ONLY_RPC_URL"])
        trading_address = os.environ.get("DREAMDEX_READ_ONLY_TRADING_ADDRESS")
        adapter = DreamDexReadOnlyAdapter(transport=rest_transport, rpc_transport=rpc_transport, owner=owner, trading_address=trading_address, symbol=symbol)
        snapshot = adapter.fetch_snapshot()
        local_cash = os.environ.get("DREAMDEX_READ_ONLY_LOCAL_CASH")
        local_inventory = os.environ.get("DREAMDEX_READ_ONLY_LOCAL_INVENTORY")
        report = adapter.reconcile(snapshot, local_cash=None if local_cash is None else Decimal(local_cash), local_inventory=None if local_inventory is None else Decimal(local_inventory))
        intent = OrderIntent(symbol, os.environ.get("DREAMDEX_READ_ONLY_DRY_RUN_SIDE", "buy"), "limit", _decimal_env("DREAMDEX_READ_ONLY_DRY_RUN_PRICE", Decimal("0")), _decimal_env("DREAMDEX_READ_ONLY_DRY_RUN_QUANTITY", Decimal("0")))
        validation = DryRunOrderValidator(DryRunValidationLimits(_decimal_env("DREAMDEX_READ_ONLY_MAX_NOTIONAL", Decimal("100000")), _decimal_env("DREAMDEX_READ_ONLY_MAX_INVENTORY", Decimal("100000")))).validate(intent, market=snapshot.market, account=snapshot.account, reconciliation=report, market_fresh=snapshot.market.is_fresh(now=datetime.now(timezone.utc), max_age_seconds=_decimal_env("DREAMDEX_READ_ONLY_MAX_MARKET_AGE_SECONDS", Decimal("30"))))
        _print_report(snapshot, report, validation)
        return 0
    except Exception as exc:
        print("READ-ONLY ACCOUNT CHECK")
        print(f"Read-only check failed: {_safe_error(exc, owner)}")
        print("No order submission was attempted.")
        print("Real submission enabled: NO")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
