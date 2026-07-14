from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from bot.integrations.dreamdex_read_only import (
    DreamDexReadOnlyAdapter,
    FixtureRpcTransport,
    FixtureTransport,
    HttpRpcTransport,
    MarketReadOnlySource,
    RpcAccountReadOnlySource,
    VaultReadOnlySource,
    load_fixture,
    mask_account_id,
)


FIXTURE = Path(__file__).parent / "fixtures" / "read_only" / "normal_account.json"


def make_adapter(fixture=None):
    fixture = fixture or load_fixture(FIXTURE)
    rest = FixtureTransport(fixture)
    rpc = FixtureRpcTransport(fixture)
    owner = "0x1234567890abcdef1234567890abcdef12345678"
    trading = "0xabcdefabcdefabcdefabcdefabcdefabcdefabcd"
    return DreamDexReadOnlyAdapter(transport=rest, rpc_transport=rpc, owner=owner, trading_address=trading, symbol="SOMI:USDso"), rest, rpc


def test_confirmed_market_vault_and_rpc_sources_match_without_network():
    adapter, rest, rpc = make_adapter()
    snapshot = adapter.fetch_snapshot()
    assert snapshot.market.symbol == "SOMI:USDso"
    assert snapshot.market.pool_address.startswith("0x3333")
    assert snapshot.market.base_token_address.startswith("0x1111")
    assert snapshot.market.quote_token_address.startswith("0x2222")
    assert snapshot.market.base_decimals == 18 and snapshot.market.quote_decimals == 18
    assert snapshot.market.price_tick_size == Decimal("0.0001")
    assert snapshot.market.quantity_step == Decimal("0.01")
    assert snapshot.market.minimum_quantity == Decimal("1")
    assert snapshot.market.stop_registry.startswith("0x4444")
    assert snapshot.market.minimum_notional is None
    assert snapshot.market.status is None
    assert snapshot.market.maker_fee is None
    assert snapshot.market.supported_order_types == ()
    assert snapshot.account.vault_rest.base.value == Decimal("10")
    assert snapshot.account.vault_rest.quote.value == Decimal("1000")
    assert snapshot.account.vault_rpc.base_vault.value == Decimal("10")
    assert snapshot.account.vault_rpc.quote_vault.value == Decimal("1000")
    assert snapshot.account.vault_rpc.base_wallet.value == Decimal("10")
    assert snapshot.account.vault_rpc.quote_wallet.value == Decimal("1000")
    assert snapshot.account.vault_rpc.native_gas.value == Decimal("10")
    assert any(path == "/markets" for path, _ in rest.paths)
    assert any("/vault/balance" in path and params.get("walletAddress") == "0xabcdefabcdefabcdefabcdefabcdefabcdefabcd" for path, params in rest.paths)
    assert [method for method, _ in rpc.calls] == ["eth_call", "eth_call", "eth_call", "eth_call", "eth_getBalance"]


def test_reconciliation_reports_match_and_blocks_incomplete_account_state():
    adapter, _, _ = make_adapter()
    snapshot = adapter.fetch_snapshot()
    report = adapter.reconcile(snapshot, local_cash=Decimal("1000"), local_inventory=Decimal("10"))
    assert not report.completed
    assert report.trading_blocked
    assert "incomplete_open_orders_source" in report.mismatches
    complete_account = replace(snapshot.account, open_orders_status="confirmed", fills_status="confirmed", orderbook_status="available")
    complete = adapter.reconcile(replace(snapshot, account=complete_account), local_cash=Decimal("1000"), local_inventory=Decimal("10"))
    assert complete.completed and not complete.trading_blocked


def test_balance_mismatch_keeps_both_sources_and_blocks():
    adapter, _, _ = make_adapter()
    snapshot = adapter.fetch_snapshot()
    mismatched = replace(snapshot.account, vault_rest=replace(snapshot.account.vault_rest, base=replace(snapshot.account.vault_rest.base, value=Decimal("9"))))
    report = adapter.reconcile(replace(snapshot, account=mismatched))
    assert "base_vault_mismatch" in report.mismatches
    assert report.vault_rest_base.value == Decimal("9")
    assert report.vault_rpc_base.value == Decimal("10")


def test_rest_and_rpc_unavailable_are_explicit_not_zero():
    class FailingRest:
        def get(self, path, **kwargs): raise RuntimeError("404 unavailable")
    class FailingRpc:
        def call(self, method, params): raise RuntimeError("rpc unavailable")
    owner = "0x1234567890abcdef1234567890abcdef12345678"
    vault = VaultReadOnlySource(FailingRest(), "SOMI:USDso", "SOMI", "USDso").fetch(owner)
    assert vault.base.status == "unavailable" and vault.base.value is None
    rpc = RpcAccountReadOnlySource(FailingRpc(), owner=owner, pool_address="0x3333333333333333333333333333333333333333", base_token_address="0x1111111111111111111111111111111111111111", quote_token_address="0x2222222222222222222222222222222222222222").fetch()
    assert rpc.base_vault.status == "unavailable" and rpc.base_vault.value is None


def test_unknown_market_and_invalid_symbol_are_rejected():
    adapter, _, _ = make_adapter()
    with pytest.raises(ValueError, match="not found"):
        MarketReadOnlySource(FixtureTransport(load_fixture(FIXTURE)), "NOPE:USDso").metadata()
    with pytest.raises(ValueError, match="BASE:QUOTE"):
        MarketReadOnlySource(FixtureTransport(load_fixture(FIXTURE)), "invalid").metadata()


def test_no_transaction_or_signing_methods_and_rpc_mutations_are_rejected():
    adapter, _, _ = make_adapter()
    for name in ("create_order", "submit_order", "cancel_order", "replace_order", "sign", "send_transaction", "send_raw_transaction"):
        assert not hasattr(adapter, name)
    for method in ("eth_sendTransaction", "eth_sendRawTransaction", "personal_sign"):
        with pytest.raises(ValueError):
            HttpRpcTransport("https://public.example").call(method, [])


def test_account_identifier_is_masked():
    assert "0x1234567890abcdef" not in mask_account_id("0x1234567890abcdef")


def test_login_owner_and_trading_address_are_separate_and_no_owner_fallback():
    fixture = load_fixture(FIXTURE)
    rest, rpc = FixtureTransport(fixture), FixtureRpcTransport(fixture)
    owner = "0x1234567890abcdef1234567890abcdef12345678"
    adapter = DreamDexReadOnlyAdapter(transport=rest, rpc_transport=rpc, owner=owner, symbol="SOMI:USDso")
    snapshot = adapter.fetch_snapshot()
    assert snapshot.account.owner_address == owner
    assert snapshot.account.trading_address is None
    assert snapshot.account.trading_address_status == "unresolved"
    assert not any("/vault/balance" in path for path, _ in rest.paths)
    assert [method for method, _ in rpc.calls] == ["eth_getBalance"]
    report = adapter.reconcile(snapshot)
    assert "trading_address_unresolved" in report.mismatches
    assert not report.completed


def test_invalid_trading_address_is_rejected_without_fallback():
    fixture = load_fixture(FIXTURE)
    with pytest.raises(ValueError, match="trading address"):
        DreamDexReadOnlyAdapter(transport=FixtureTransport(fixture), rpc_transport=FixtureRpcTransport(fixture), owner="0x1234567890abcdef1234567890abcdef12345678", trading_address="not-an-address")
