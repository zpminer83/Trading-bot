"""Safe read-only DreamDEX account check.

This script intentionally has no order mutation methods and no flag that can
enable real submission.  A fixture may be selected with an environment
variable for completely offline operation.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
import os
import re
import sys
from datetime import datetime, timezone

from bot.execution.dry_run_order_validator import DryRunOrderValidator, DryRunValidationLimits
from bot.execution.order import OrderIntent
from bot.integrations.dreamdex_read_only import DreamDexReadOnlyAdapter, FixtureTransport, load_fixture, mask_account_id


TRUE_VALUES = {"1", "true", "yes", "on"}


def _decimal_env(name: str, default: Decimal) -> Decimal:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return Decimal(raw)
    except (InvalidOperation, ValueError):
        raise ValueError(f"{name} must be a decimal")


def _safe_error(exc: Exception, account_identifier: str | None = None) -> str:
    message = str(exc)
    message = re.sub(r"(?i)(private[_ -]?key|seed[_ -]?phrase|api[_ -]?secret|authorization|bearer|signature)\s*[:=]\s*\S+", r"\1=<redacted>", message)
    if account_identifier:
        message = message.replace(account_identifier, mask_account_id(account_identifier))
    return message[:300]


def _print_snapshot(snapshot, report, validation) -> None:
    market = snapshot.market
    account = snapshot.account
    quote = market.quote_asset or "USDso"
    base = market.base_asset or "SOMI"
    print("READ-ONLY ACCOUNT CHECK")
    print(f"Market: {market.symbol} ({base}/{quote})")
    print(f"  Status: {market.status}; tick {market.price_tick_size}; quantity step {market.quantity_step_size}")
    print(f"  Minimum quantity: {market.minimum_quantity}; minimum notional: {market.minimum_notional}")
    print(f"  Supported orders: {', '.join(market.supported_order_types) or 'unknown'}")
    print(f"  Maker/taker fee: {market.maker_fee} / {market.taker_fee}")
    print(f"Balances (account {mask_account_id(account.account_identifier)}):")
    for asset in (quote, base):
        balance = account.balance(asset)
        print(f"  {asset}: total={balance.total} available={balance.available} locked={balance.locked}")
    print(f"Open orders: {len(account.open_orders)}")
    for order in account.open_orders:
        print(f"  {order.order_id}: {order.side or '?'} {order.quantity} @ {order.price} ({order.status or 'unknown'})")
    print(f"Recent orders: {len(account.recent_orders)}")
    print(f"Recent fills: {len(account.recent_fills)}")
    for fill in account.recent_fills:
        print(f"  {fill.fill_id}: {fill.side or '?'} {fill.quantity} @ {fill.price}; commission={fill.commission}")
    print(f"Commissions: {len(account.commissions)}")
    print("Trading constraints:")
    print(f"  Reconciliation: {'OK' if report.completed and not report.trading_blocked else 'BLOCKED'} ({report.reason})")
    print(f"  Cash difference: {report.cash_difference}; inventory difference: {report.inventory_difference}")
    print("Dry-run validation:")
    print(f"  Approved: {'YES' if validation.approved else 'NO'}")
    print(f"  Normalized price/quantity: {validation.normalized_price} / {validation.normalized_quantity}")
    print(f"  Notional: {validation.notional}")
    print(f"  Reasons: {', '.join(validation.reasons) or 'none'}")
    print("Reconciliation status:")
    print(f"  Completed: {report.completed}; unresolved orders: {len(report.unresolved_orders)}")
    print("Real submission enabled: NO")


def main() -> int:
    required = ["DREAMDEX_READ_ONLY_ACCOUNT_ID"]
    fixture_path = os.environ.get("DREAMDEX_READ_ONLY_FIXTURE")
    if not fixture_path:
        required.append("DREAMDEX_READ_ONLY_BASE_URL")
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        print("READ-ONLY ACCOUNT CHECK")
        print("Missing configuration: " + ", ".join(missing))
        print("No network request or order operation was attempted.")
        print("Real submission enabled: NO")
        return 2
    try:
        account_id = os.environ["DREAMDEX_READ_ONLY_ACCOUNT_ID"]
        market_symbol = os.environ.get("DREAMDEX_READ_ONLY_MARKET", "SOMI:USDso")
        if fixture_path:
            fixture = load_fixture(fixture_path)
            transport = FixtureTransport(fixture)
            adapter = DreamDexReadOnlyAdapter(transport=transport, account_identifier=account_id, market_symbol=market_symbol)
        else:
            adapter = DreamDexReadOnlyAdapter(base_url=os.environ["DREAMDEX_READ_ONLY_BASE_URL"], account_identifier=account_id, market_symbol=market_symbol)
        snapshot = adapter.fetch_snapshot()
        local_cash = _decimal_env("DREAMDEX_READ_ONLY_LOCAL_CASH", Decimal("0"))
        local_inventory = _decimal_env("DREAMDEX_READ_ONLY_LOCAL_INVENTORY", Decimal("0"))
        report = adapter.reconcile(snapshot, local_cash=local_cash, local_inventory=local_inventory)
        price = _decimal_env("DREAMDEX_READ_ONLY_DRY_RUN_PRICE", Decimal("0"))
        quantity = _decimal_env("DREAMDEX_READ_ONLY_DRY_RUN_QUANTITY", Decimal("0"))
        intent = OrderIntent(market_symbol, os.environ.get("DREAMDEX_READ_ONLY_DRY_RUN_SIDE", "buy"), "limit", price, quantity)
        market_fresh = snapshot.market.is_fresh(
            now=datetime.now(timezone.utc),
            max_age_seconds=_decimal_env("DREAMDEX_READ_ONLY_MAX_MARKET_AGE_SECONDS", Decimal("30")),
        )
        validation = DryRunOrderValidator(
            DryRunValidationLimits(
                maximum_notional=_decimal_env("DREAMDEX_READ_ONLY_MAX_NOTIONAL", Decimal("100000")),
                maximum_inventory=_decimal_env("DREAMDEX_READ_ONLY_MAX_INVENTORY", Decimal("100000")),
            )
        ).validate(intent, market=snapshot.market, account=snapshot.account, reconciliation=report, market_fresh=market_fresh)
        _print_snapshot(snapshot, report, validation)
        return 0
    except Exception as exc:
        print("READ-ONLY ACCOUNT CHECK")
        print(f"Read-only check failed: {_safe_error(exc, os.environ.get('DREAMDEX_READ_ONLY_ACCOUNT_ID'))}")
        print("No order submission was attempted.")
        print("Real submission enabled: NO")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
