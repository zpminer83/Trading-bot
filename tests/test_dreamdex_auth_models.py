from datetime import datetime, timedelta, timezone
from decimal import Decimal

from bot.integrations.dreamdex_auth_models import (
    AUTH_ENDPOINT_DESCRIPTORS,
    AuthenticatedAccountSnapshot,
    FixtureAuthenticatedReadOnlyTransport,
    UnconfiguredAuthenticatedReadOnlyTransport,
)
from bot.integrations.dreamdex_read_only import DreamDexReadOnlyAdapter, FixtureRpcTransport, FixtureTransport


OWNER = "0x1234567890abcdef1234567890abcdef12345678"
TRADING = "0xabcdefabcdefabcdefabcdefabcdefabcdefabcd"


def _fixture(**auth_overrides):
    account = {
        "balances": {"status": "available", "pagination_complete": True, "records": [
            {"currency": "SOMI", "amount": "2.5"}, {"currency": "USDso", "amount": "100"},
        ]},
        "openOrders": {"status": "available", "pagination_complete": True, "records": [
            {"id": "o-1", "symbol": "SOMI:USDso", "side": "buy", "price": "10", "amount": "1", "remaining": "1", "status": "open"},
        ]},
        "recentOrders": {"status": "available", "pagination_complete": True, "records": []},
        "fills": {"status": "available", "pagination_complete": True, "records": [
            {"fillId": "f-1", "orderId": "o-1", "symbol": "SOMI:USDso", "side": "buy", "price": "10", "quantity": "0.5", "fee": "0.01"},
            {"fillId": "f-1", "orderId": "o-1", "symbol": "SOMI:USDso", "side": "buy", "price": "10", "quantity": "0.5", "fee": "0.01"},
        ]},
        "commissions": {"status": "available", "pagination_complete": True, "records": []},
        **auth_overrides,
    }
    return {"authenticated_account": account}


def test_endpoint_descriptors_match_official_implementation_without_exposing_auth_data():
    by_name = {item.name: item for item in AUTH_ENDPOINT_DESCRIPTORS}
    assert (by_name["auth_nonce"].method, by_name["auth_nonce"].path) == ("GET", "/auth/nonce")
    assert by_name["auth_nonce"].confirmation == "confirmed"
    assert by_name["auth_login"].required_body_fields == ("message", "signature")
    assert by_name["auth_login"].response_shape == "{token|jwt, expiresAt}"
    assert by_name["account_vault_balances"].required_query_params == ("walletAddress",)
    assert by_name["open_orders_by_market"].confirmation == "hypothetical"
    assert by_name["market_trades_feed"].confirmation == "confirmed_public_not_account_fills"
    assert by_name["account_fills"].confirmation == "hypothetical_account_filter"
    assert by_name["account_commissions"].confirmation == "hypothetical"


def test_fixture_parses_balances_orders_fills_commissions_and_deduplicates_fills():
    transport = FixtureAuthenticatedReadOnlyTransport(_fixture())
    snapshot = transport.fetch_account_snapshot(TRADING, "SOMI:USDso")
    assert snapshot.account_identifier == "0xab...abcd"
    assert {row.asset: row.total for row in snapshot.balances} == {"SOMI": Decimal("2.5"), "USDso": Decimal("100")}
    assert snapshot.open_orders[0].order_id == "o-1"
    assert len(snapshot.fills) == 1
    assert snapshot.fills_status.duplicate_count == 1
    assert snapshot.fills[0].quantity == Decimal("0.5")
    assert snapshot.authoritative_for(TRADING)


def test_incomplete_pagination_stale_and_malformed_records_are_not_authoritative():
    incomplete = FixtureAuthenticatedReadOnlyTransport(_fixture(balances={"status": "available", "pagination_complete": False, "records": [{"currency": "USDso", "amount": "1"}]})).fetch_account_snapshot(TRADING, "SOMI:USDso")
    assert not incomplete.pagination_complete
    assert not incomplete.authoritative_for(TRADING)

    stale_time = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    stale = FixtureAuthenticatedReadOnlyTransport(_fixture(observed_at=stale_time)).fetch_account_snapshot(TRADING, "SOMI:USDso")
    assert not stale.balances_status.is_fresh(max_age_seconds=Decimal("30"))
    assert not stale.authoritative_for(TRADING)

    malformed = FixtureAuthenticatedReadOnlyTransport(_fixture(openOrders={"status": "available", "pagination_complete": True, "records": [{"symbol": "SOMI:USDso"}]})).fetch_account_snapshot(TRADING, "SOMI:USDso")
    assert malformed.open_orders_status.status == "malformed"
    assert not malformed.authoritative_for(TRADING)


def test_unconfigured_transport_is_explicit_and_has_no_auth_or_mutation_methods():
    transport = UnconfiguredAuthenticatedReadOnlyTransport()
    balances = transport.fetch_account_balances(TRADING, ("SOMI:USDso",))
    assert balances.status == "unavailable"
    assert balances.source_status.error_code == "authenticated_transport_unconfigured"
    for name in ("login", "sign", "authenticate", "post", "submit_order", "cancel_order", "replace_order"):
        assert not hasattr(transport, name)


def test_adapter_uses_fixture_auth_state_but_default_transport_remains_fail_closed():
    public_fixture = {
        "markets": [{"symbol": "SOMI:USDso", "base": "0x1111111111111111111111111111111111111111", "quote": "0x2222222222222222222222222222222222222222", "contract": "0x3333333333333333333333333333333333333333", "baseDecimals": 18, "quoteDecimals": 18, "tickSize": "0.0001", "lotSize": "0.01", "minQuantity": "1", "stopRegistry": "0x4444444444444444444444444444444444444444"}],
        "orderbook": {"symbol": "SOMI:USDso", "bids": [{"price": "10", "quantity": "2"}], "asks": [{"price": "10.1", "quantity": "2"}], "timestamp": datetime.now(timezone.utc).isoformat()},
        "vault_rest": {"SOMI": "0", "USDso": "0"},
        "rpc": {"base_vault": "0x0", "quote_vault": "0x0", "base_wallet": "0x0", "quote_wallet": "0x0"},
        "native_gas": "0x0",
    }
    auth_transport = FixtureAuthenticatedReadOnlyTransport(_fixture())
    adapter = DreamDexReadOnlyAdapter(transport=FixtureTransport(public_fixture), rpc_transport=FixtureRpcTransport(public_fixture), owner=OWNER, trading_address=TRADING, authenticated_transport=auth_transport)
    snapshot = adapter.fetch_snapshot()
    assert snapshot.account.authenticated.available
    assert snapshot.account.account_address_semantics == "resolved"
    assert snapshot.account.open_orders_status == "confirmed"
    assert snapshot.account.fills_status == "confirmed"

    default_adapter = DreamDexReadOnlyAdapter(transport=FixtureTransport(public_fixture), rpc_transport=FixtureRpcTransport(public_fixture), owner=OWNER, trading_address=TRADING)
    default_snapshot = default_adapter.fetch_snapshot()
    report = default_adapter.reconcile(default_snapshot)
    assert not report.completed
    assert "authenticated_account_state_unavailable" in report.mismatches
    assert "incomplete_open_orders_source" in report.mismatches
    assert "incomplete_fills_source" in report.mismatches
