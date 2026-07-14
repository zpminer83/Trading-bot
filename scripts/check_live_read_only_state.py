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
    if value.value is not None:
        return f"{value.status}: {value.value}"
    suffix = f" error_code={value.error_code}" if getattr(value, "error_code", None) else ""
    return f"{value.status}: {value.reason or 'unavailable'}{suffix}"


def _source_status(value) -> str:
    if value is None:
        return "unavailable (error_code=unavailable)"
    suffix = f" (error_code={value.error_code})" if getattr(value, "error_code", None) else ""
    return f"{value.status}{suffix}"


def _masked(value: str | None) -> str:
    return mask_account_id(value) if value else "<unresolved>"


def _print_address_diagnostics(label: str, diagnostics, fallback_address: str | None = None) -> None:
    print(f"{label}:")
    if diagnostics is None:
        print(f"  address masked: {_masked(fallback_address)}")
        print("  type: unavailable (error_code=unavailable)")
        for name in ("native SOMI", "wallet SOMI", "wallet USDso", "vault SOMI", "vault USDso"):
            print(f"  {name}: unavailable (error_code=unavailable)")
        return
    print(f"  address masked: {_masked(diagnostics.address)}")
    type_suffix = "" if diagnostics.address_type != "unavailable" else f" (error_code={diagnostics.code.error_code})"
    print(f"  type: {diagnostics.address_type}{type_suffix}")
    print(f"  base asset kind: {diagnostics.base_token.asset_kind.value}")
    print(f"  base token code status: {_source_status(diagnostics.base_token.code)}")
    print(f"  base raw balance status: {_source_status(diagnostics.base_token.raw_balance)}")
    print(f"  base decimals: {_source(diagnostics.base_token.decimals)}")
    print(f"  base balance read method: {diagnostics.base_token.balance_method}")
    print(f"  quote asset kind: {diagnostics.quote_token.asset_kind.value}")
    print(f"  quote token code status: {_source_status(diagnostics.quote_token.code)}")
    print(f"  quote raw balance status: {_source_status(diagnostics.quote_token.raw_balance)}")
    print(f"  quote decimals: {_source(diagnostics.quote_token.decimals)}")
    print(f"  quote balance read method: {diagnostics.quote_token.balance_method}")
    for name, value in (("native SOMI", diagnostics.native_gas), ("wallet SOMI", diagnostics.wallet_somi), ("wallet USDso", diagnostics.wallet_usdso), ("vault SOMI", diagnostics.vault_somi), ("vault USDso", diagnostics.vault_usdso)):
        print(f"  {name}: {_source(value)}")


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
    print(f"Tick size: {market.price_tick_size}")
    print(f"Quantity step: {market.quantity_step_size}")
    print(f"Minimum quantity: {market.minimum_quantity}")
    print(f"Minimum notional: {market.minimum_notional if market.minimum_notional is not None else 'unavailable'}")
    _print_address_diagnostics("OWNER/LOGIN", snapshot.owner_diagnostics, account.owner_address or account.account_identifier)
    _print_address_diagnostics("TRADING/SMART", snapshot.trading_diagnostics, account.trading_address)
    book = snapshot.orderbook if isinstance(snapshot.orderbook, dict) else {}
    bids, asks = book.get("bids", []), book.get("asks", [])
    best_bid = bids[0].get("price", bids[0]) if bids and isinstance(bids[0], dict) else (bids[0] if bids else "<unavailable>")
    best_ask = asks[0].get("price", asks[0]) if asks and isinstance(asks[0], dict) else (asks[0] if asks else "<unavailable>")
    print(f"Best bid: {best_bid}")
    print(f"Best ask: {best_ask}")
    timestamp = _orderbook_timestamp(book)
    age = None if timestamp is None else max(Decimal("0"), Decimal(str((datetime.now(timezone.utc) - timestamp).total_seconds())))
    max_age = _decimal_env("DREAMDEX_READ_ONLY_MAX_MARKET_AGE_SECONDS", Decimal("30"))
    print(f"Orderbook timestamp: {timestamp.isoformat() if timestamp else '<unavailable>'}")
    print(f"Orderbook age: {age if age is not None else '<unavailable>'} seconds")
    freshness = "fresh" if account.orderbook_status == "available" and age is not None and age <= max_age else ("stale" if account.orderbook_status == "stale" else "unavailable")
    print(f"Orderbook source status: {'available' if bids and asks else 'unavailable'}")
    print(f"Orderbook freshness: {freshness}")
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
    print(f"Native gas balance (eth_getBalance, owner/login address={_masked(account.owner_address)}; normalized 18 decimals): {_source(account.vault_rpc.native_gas)}")
    print(f"Open-orders source status: {account.open_orders_status}")
    print(f"Fills source status: {account.fills_status}")
    authenticated = account.authenticated
    auth_unconfigured = authenticated.balances_status.error_code == "authenticated_transport_unconfigured"
    print(f"Authenticated account source: {'unconfigured' if auth_unconfigured else ('available' if authenticated.available else 'unavailable')}")
    print(f"Authenticated balances: {authenticated.balances_status.status}")
    print(f"Authenticated open orders: {authenticated.open_orders_status.status}")
    print(f"Authenticated fills: {authenticated.fills_status.status}")
    print(f"Authenticated pagination complete: {'YES' if authenticated.pagination_complete else 'NO'}")
    onchain = account.onchain_fills
    onchain_status = onchain.source_status
    print(f"On-chain fills source: {onchain_status.status if onchain_status.status != 'unavailable' else 'unavailable'}")
    print(f"On-chain latest block: {onchain_status.latest_block if onchain_status.latest_block is not None else 'unavailable'}")
    print(f"On-chain confirmed through block: {onchain_status.confirmed_through_block if onchain_status.confirmed_through_block is not None else 'unavailable'}")
    print(f"On-chain decoded fills: {onchain_status.decoded_fill_count}")
    print(f"On-chain duplicate count: {onchain_status.duplicate_count}")
    print(f"On-chain pagination complete: {'YES' if onchain_status.pagination_complete else 'NO'}")
    print(f"On-chain reorg status: {onchain_status.reorg_status}")
    print(f"On-chain account match status: {onchain_status.account_match_status}")
    print(f"On-chain fills authoritative: {'YES' if onchain_status.authoritative and onchain_status.account_match_status == 'matched' else 'NO'}")
    metadata = account.order_metadata_report
    metadata_source = "unconfigured" if metadata.reason == "authenticated_transport_unconfigured" else ("available" if metadata.resolved_count else "unavailable")
    print(f"Order metadata source: {metadata_source}")
    print(f"Order metadata records resolved: {metadata.resolved_count}")
    print(f"Order metadata conflicts: {metadata.conflict_count}")
    print(f"Fill/order correlation status: {metadata.status}")
    print(f"Account-correlated fills authoritative: {'YES' if metadata.authoritative else 'NO'}")
    print(f"Reconciliation complete: {'YES' if report.completed else 'NO'}")
    print(f"Account address semantics: {account.account_address_semantics}")
    print(f"Hypothetical trading blocked: {'YES' if report.trading_blocked else 'NO'}")
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
