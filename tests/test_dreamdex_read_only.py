from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from bot.integrations.dreamdex_read_only import (
    DreamDexReadOnlyAdapter,
    FixtureTransport,
    mask_account_id,
    load_fixture,
)


FIXTURE = Path(__file__).parent / "fixtures" / "read_only" / "normal_account.json"


def make_adapter():
    transport = FixtureTransport(load_fixture(FIXTURE))
    return DreamDexReadOnlyAdapter(
        transport=transport,
        account_identifier="0x1234567890abcdef",
        market_symbol="SOMI:USDso",
        clock=lambda: datetime(2026, 7, 14, 10, 0, tzinfo=timezone.utc),
    ), transport


def test_read_only_adapter_parses_market_account_and_timestamps_without_network():
    adapter, transport = make_adapter()
    snapshot = adapter.fetch_snapshot()
    assert snapshot.market.symbol == "SOMI:USDso"
    assert snapshot.market.base_asset == "SOMI"
    assert snapshot.market.quote_asset == "USDso"
    assert snapshot.market.price_tick_size == Decimal("0.0001")
    assert snapshot.market.quantity_step_size == Decimal("0.01")
    assert snapshot.market.minimum_notional == Decimal("5")
    assert snapshot.account.balance("USDso").available == Decimal("900")
    assert snapshot.account.balance("SOMI").locked == Decimal("2")
    assert len(snapshot.account.open_orders) == 1
    assert len(snapshot.account.recent_fills) == 1
    assert transport.paths == ["/markets", "/accounts/0x1234567890abcdef"]


def test_reconciliation_reports_matching_and_mismatched_authoritative_state():
    adapter, _ = make_adapter()
    snapshot = adapter.fetch_snapshot()
    matching = adapter.reconcile(
        snapshot,
        local_cash=Decimal("1000"),
        local_inventory=Decimal("10"),
        local_open_order_ids=["order-1"],
        local_fill_ids=["fill-0"],
    )
    assert matching.completed and not matching.trading_blocked
    mismatch = adapter.reconcile(snapshot, local_cash=Decimal("1"), local_inventory=Decimal("0"))
    assert mismatch.trading_blocked
    assert "cash_mismatch" in mismatch.mismatches
    assert mismatch.unresolved_orders == ("order-1",)


def test_account_safe_representation_masks_identifier():
    adapter, _ = make_adapter()
    account = adapter.fetch_account_snapshot()
    safe = account.safe_dict()
    assert "0x1234567890abcdef" not in str(safe)
    assert safe["account_identifier"] == "0x12…cdef"
    assert mask_account_id("private-key-like-value") != "private-key-like-value"


def test_adapter_has_no_order_mutation_api():
    adapter, _ = make_adapter()
    for name in ("create_order", "submit_order", "cancel_order", "replace_order", "create", "cancel", "replace"):
        assert not hasattr(adapter, name)
