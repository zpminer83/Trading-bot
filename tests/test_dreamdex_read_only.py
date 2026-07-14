from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from bot.integrations.dreamdex_read_only import (
    AddressReadOnlyDiagnostics,
    AssetKind,
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
    assert snapshot.market.base_asset_kind is AssetKind.native
    assert snapshot.market.quote_asset_kind is AssetKind.erc20
    assert snapshot.account.vault_rest.base.value == Decimal("10")
    assert snapshot.account.vault_rest.quote.value == Decimal("1000")
    assert snapshot.account.vault_rpc.base_vault.value == Decimal("10")
    assert snapshot.account.vault_rpc.quote_vault.value == Decimal("1000")
    assert snapshot.account.vault_rpc.base_wallet.value == Decimal("10")
    assert snapshot.account.vault_rpc.quote_wallet.value == Decimal("1000")
    assert snapshot.account.vault_rpc.native_gas.value == Decimal("10")
    assert any(path == "/markets" for path, _ in rest.paths)
    assert any("/vault/balance" in path and params.get("walletAddress") == "0xabcdefabcdefabcdefabcdefabcdefabcdefabcd" for path, params in rest.paths)
    assert [method for method, _ in rpc.calls].count("eth_getCode") == 6
    assert [method for method, _ in rpc.calls].count("eth_getBalance") == 2


def test_reconciliation_reports_match_and_blocks_incomplete_account_state():
    adapter, _, _ = make_adapter()
    snapshot = adapter.fetch_snapshot()
    report = adapter.reconcile(snapshot, local_cash=Decimal("1000"), local_inventory=Decimal("10"))
    assert not report.completed
    assert report.trading_blocked
    assert "incomplete_open_orders_source" in report.mismatches
    complete_account = replace(snapshot.account, open_orders_status="confirmed", fills_status="confirmed", orderbook_status="available")
    complete = adapter.reconcile(replace(snapshot, account=complete_account), local_cash=Decimal("1000"), local_inventory=Decimal("10"))
    assert not complete.completed and complete.trading_blocked
    assert "authoritative_account_address_unresolved" in complete.mismatches


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


def test_real_markets_wrapper_maps_native_somi_and_optional_fields_remain_unavailable():
    market = {
        "symbol": "SOMI:USDso",
        "base": "0x28f34DeFd2b4CB48d9eE6d89f2Be4Bc601694c00",
        "quote": "0x00000022dA000002656c64D9eA6011ea952D008A",
        "contract": "0x035De7403eac6872787779CCA7CCF1b4CDb61379",
        "baseDecimals": 18,
        "quoteDecimals": 18,
        "tickSize": "0.0001",
        "lotSize": "0.01",
        "minQuantity": "1",
        "stopRegistry": "0x68c8f6fb1EA19A28F25358Ff00b8Ed8E1216df30",
    }
    metadata = MarketReadOnlySource(FixtureTransport({"markets": {"markets": [market]}}), "SOMI:USDso").metadata()
    assert metadata.base_asset_kind is AssetKind.native
    assert metadata.base_token_address == market["base"]
    assert metadata.quote_token_address == market["quote"]
    assert metadata.pool_contract == market["contract"]
    assert metadata.price_tick_size == Decimal("0.0001")
    assert metadata.quantity_step == Decimal("0.01")
    assert metadata.minimum_quantity == Decimal("1")
    assert metadata.base_decimals == 18 and metadata.quote_decimals == 18
    assert metadata.stop_registry == market["stopRegistry"]
    assert metadata.minimum_notional is None
    assert metadata.status is None
    assert metadata.maker_fee is None and metadata.taker_fee is None


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
    assert [method for method, _ in rpc.calls].count("eth_getCode") == 3
    assert [method for method, _ in rpc.calls].count("eth_getBalance") == 1
    report = adapter.reconcile(snapshot)
    assert "trading_address_unresolved" in report.mismatches
    assert not report.completed


def test_invalid_trading_address_is_rejected_without_fallback():
    fixture = load_fixture(FIXTURE)
    with pytest.raises(ValueError, match="trading address"):
        DreamDexReadOnlyAdapter(transport=FixtureTransport(fixture), rpc_transport=FixtureRpcTransport(fixture), owner="0x1234567890abcdef1234567890abcdef12345678", trading_address="not-an-address")


def test_dual_address_types_and_no_authoritative_choice():
    fixture = load_fixture(FIXTURE)
    owner = "0x1234567890abcdef1234567890abcdef12345678"
    trading = "0xabcdefabcdefabcdefabcdefabcdefabcdefabcd"
    fixture["rpc"]["code_by_address"] = {owner.lower(): "0x", trading.lower(): "0x60006000"}
    adapter = DreamDexReadOnlyAdapter(transport=FixtureTransport(fixture), rpc_transport=FixtureRpcTransport(fixture), owner=owner, trading_address=trading)
    snapshot = adapter.fetch_snapshot()
    assert isinstance(snapshot.owner_diagnostics, AddressReadOnlyDiagnostics)
    assert snapshot.owner_diagnostics.address_type == "eoa"
    assert snapshot.trading_diagnostics.address_type == "contract"
    assert snapshot.account.account_address_semantics == "unresolved"
    report = adapter.reconcile(snapshot)
    assert not report.completed and "authoritative_account_address_unresolved" in report.mismatches
    assert report.exchange_cash is None and report.exchange_inventory is None


class _DiagnosticRpc:
    def __init__(self, mode="success"):
        self.mode = mode
        self.calls = []

    def call(self, method, params):
        self.calls.append((method, params))
        if method == "eth_getCode":
            return "0x"
        if method == "eth_getBalance":
            return "0x8ac7230489e80000"
        data = params[0]["data"]
        if self.mode == "revert":
            raise RuntimeError("execution reverted: bad token")
        if self.mode == "empty":
            return "0x"
        if self.mode == "malformed":
            return "0xnot-hex"
        if data.startswith("0x70a08231"):
            return "0xde0b6b3a7640000"
        if data.startswith("0x313ce567"):
            return "0x12"
        return "0x8ac7230489e80000"


def _diagnostic_source(rpc):
    return RpcAccountReadOnlySource(rpc, owner="0x1234567890abcdef1234567890abcdef12345678", pool_address="0x3333333333333333333333333333333333333333", base_token_address="0x1111111111111111111111111111111111111111", quote_token_address="0x2222222222222222222222222222222222222222")


def test_balance_of_selector_padding_and_somi_success():
    rpc = _DiagnosticRpc()
    diagnostics = _diagnostic_source(rpc).fetch_address("0x1234567890abcdef1234567890abcdef12345678")
    assert diagnostics.wallet_base.value == Decimal("1")
    balance_calls = [params for method, params in rpc.calls if method == "eth_call" and params[0]["data"].startswith("0x70a08231")]
    assert balance_calls
    assert len(balance_calls[0][0]["data"]) == 74
    assert balance_calls[0][1] == "latest"
    assert set(balance_calls[0][0]) == {"to", "data"}


def test_native_asset_uses_eth_get_balance_and_never_balance_of():
    rpc = _DiagnosticRpc()
    source = RpcAccountReadOnlySource(rpc, owner="0x1234567890abcdef1234567890abcdef12345678", pool_address="0x3333333333333333333333333333333333333333", base_token_address="0x28f34DeFd2b4CB48d9eE6d89f2Be4Bc601694c00", quote_token_address="0x2222222222222222222222222222222222222222", base_asset_kind=AssetKind.native)
    diagnostics = source.fetch_address("0x1234567890abcdef1234567890abcdef12345678")
    assert diagnostics.base_token.asset_kind is AssetKind.native
    assert diagnostics.base_token.balance_method == "eth_getBalance"
    assert diagnostics.base_token.balance.value == Decimal("10")
    assert not any(method == "eth_call" and params[0]["data"].startswith("0x70a08231") and params[0]["to"].lower().startswith("0x28f3") for method, params in rpc.calls)


def test_special_and_unknown_assets_remain_unavailable_without_fallback():
    for kind in (AssetKind.special, AssetKind.unknown):
        rpc = _DiagnosticRpc()
        source = RpcAccountReadOnlySource(rpc, owner="0x1234567890abcdef1234567890abcdef12345678", pool_address="0x3333333333333333333333333333333333333333", base_token_address="0x1111111111111111111111111111111111111111", quote_token_address="0x2222222222222222222222222222222222222222", base_asset_kind=kind)
        diagnostics = source.fetch_address("0x1234567890abcdef1234567890abcdef12345678")
        assert diagnostics.base_token.balance.value is None
        assert diagnostics.base_token.balance_method == "unavailable"
        assert not any(method == "eth_call" and params[0]["data"].startswith("0x70a08231") and params[0]["to"].lower().startswith("0x1111") for method, params in rpc.calls)


def test_erc20_decimals_error_is_not_silently_converted():
    diagnostics = _diagnostic_source(_DiagnosticRpc("revert")).fetch_address("0x1234567890abcdef1234567890abcdef12345678")
    assert diagnostics.quote_token.decimals.error_code == "contract_revert"
    assert diagnostics.quote_token.balance.error_code == "contract_revert"


@pytest.mark.parametrize("mode,error_code", [("empty", "empty_result"), ("malformed", "malformed_hex"), ("revert", "contract_revert")])
def test_balance_of_failures_are_explicit(mode, error_code):
    diagnostics = _diagnostic_source(_DiagnosticRpc(mode)).fetch_address("0x1234567890abcdef1234567890abcdef12345678")
    assert diagnostics.wallet_base.value is None
    assert diagnostics.wallet_base.error_code == error_code


def test_eth_get_code_rpc_error_is_unavailable():
    class CodeError(_DiagnosticRpc):
        def call(self, method, params):
            if method == "eth_getCode":
                raise RuntimeError("rpc unavailable")
            return super().call(method, params)
    diagnostics = _diagnostic_source(CodeError()).fetch_address("0x1234567890abcdef1234567890abcdef12345678")
    assert diagnostics.address_type == "unavailable"
    assert diagnostics.code.error_code == "rpc_error"
    assert diagnostics.base_token.code.error_code == "rpc_error"


def test_unauthorized_rest_vault_is_not_zero():
    class Response:
        status_code = 401
    class Unauthorized:
        def get(self, path, **kwargs):
            error = RuntimeError("unauthorized")
            error.response = Response()
            raise error
    vault = VaultReadOnlySource(Unauthorized(), "SOMI:USDso", "SOMI", "USDso").fetch("0x1234567890abcdef1234567890abcdef12345678")
    assert vault.base.status == "unauthorized"
    assert vault.base.value is None
    assert vault.base.error_code == "unauthorized"
