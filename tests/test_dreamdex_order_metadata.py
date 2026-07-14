from decimal import Decimal

from bot.integrations.dreamdex_fill_events import NormalizedOrderFill
from bot.integrations.dreamdex_order_metadata import (
    CONFIRMED_ORDER_ALIASES,
    ORDER_ENDPOINT_DESCRIPTORS,
    FixtureOrderMetadataTransport,
    OrderMetadataResolver,
    UnconfiguredOrderMetadataTransport,
    normalize_order_metadata,
)


OWNER = "0x" + "12" * 20
OTHER = "0x" + "34" * 20
SYMBOL = "SOMI:USDso"


def _fixture(**overrides):
    orders = [
        {"id": "7", "symbol": SYMBOL, "owner": OWNER, "side": "buy", "isBid": True, "price": "123.45", "amount": "2", "filledQuantity": "0.5", "remaining": "1.5", "status": "open", "confirmed_fields": ["owner", "side"]},
        {"orderId": "8", "market": SYMBOL, "owner": OWNER, "side": "sell", "isBid": False, "price": "123.45", "quantity": "2", "filledQuantity": "0.5", "remainingQuantity": "1.5", "status": "open", "confirmed_fields": ["owner", "side"]},
    ]
    section = {"status": "available", "pagination_complete": True, "orders": orders}
    section.update(overrides)
    return {"order_metadata": {SYMBOL: section}}


def _fill(*, side="buy", price=Decimal("123.45"), quantity=Decimal("0.5"), symbol=SYMBOL):
    return NormalizedOrderFill(
        "20:0x" + "aa" * 32 + ":0", 20, "0x" + "35" * 20, symbol, 7, 7, 8,
        None, side, True if side == "buy" else False, None, 123450000000000000000,
        500000000000000000, price, quantity, price * quantity, 16, "0x" + "bb" * 32,
        None, "0x" + "aa" * 32, 0, 0, True,
    )


def test_official_endpoint_descriptors_and_confirmed_aliases():
    by_name = {item["name"]: item for item in ORDER_ENDPOINT_DESCRIPTORS}
    assert by_name["order_by_id"]["method"] == "GET"
    assert by_name["order_by_id"]["path"] == "/markets/{symbol}/orders/{orderId}"
    assert by_name["order_by_id"]["requires_authorization"] is True
    assert by_name["orders_page"]["response_shape"] == "list or {orders: [...] }".replace(" ", "") or by_name["orders_page"]["response_shape"] == "list or {orders: [...]}"
    assert CONFIRMED_ORDER_ALIASES["order_id"] == ("orderId", "id")
    assert by_name["orders_page"]["pagination"] == "not_confirmed"


def test_known_order_parsing_masks_owner_and_uses_decimal_values():
    transport = FixtureOrderMetadataTransport(_fixture())
    result = transport.fetch_order_by_id(SYMBOL, "7")
    assert result.status == "available"
    metadata = result.metadata
    assert metadata is not None
    assert metadata.order_id == "7"
    assert metadata.account_identifier == "0x12...1212"
    assert metadata.owner == OWNER
    assert metadata.side == "buy"
    assert metadata.is_bid is True
    assert metadata.price == Decimal("123.45")
    assert metadata.quantity == Decimal("2")
    assert metadata.filled_quantity == Decimal("0.5")
    assert metadata.remaining_quantity == Decimal("1.5")
    assert metadata.status == "open"
    assert metadata.malformed_fields == ()


def test_confirmed_vendor_aliases_and_unknown_status_are_preserved_safely():
    fixture = _fixture(orders=[{"id": "7", "symbol": SYMBOL, "amount": "1.00", "remaining": "1.00", "price": "10", "status": "canceled"}])
    result = FixtureOrderMetadataTransport(fixture).fetch_order_by_id(SYMBOL, "7")
    assert result.metadata is not None
    assert result.metadata.quantity == Decimal("1.00")
    assert result.metadata.remaining_quantity == Decimal("1.00")
    assert result.metadata.status == "cancelled"

    unknown = _fixture(orders=[{"id": "7", "symbol": SYMBOL, "status": "brand_new_status"}])
    unknown_result = FixtureOrderMetadataTransport(unknown).fetch_order_by_id(SYMBOL, "7")
    assert unknown_result.metadata is not None
    assert unknown_result.metadata.raw_status == "brand_new_status"
    assert unknown_result.metadata.status == "unknown"


def test_missing_malformed_and_unknown_order_records_are_fail_closed():
    transport = FixtureOrderMetadataTransport(_fixture(orders=[{"symbol": SYMBOL, "price": "not-a-number"}]))
    rows, status = transport.fetch_orders_page(SYMBOL)
    assert len(rows) == 1
    assert status.malformed_count == 1
    malformed = normalize_order_metadata(rows[0])
    assert "order_id" in malformed.malformed_fields
    assert "price" in malformed.malformed_fields
    missing = transport.fetch_order_by_id(SYMBOL, "does-not-exist")
    assert missing.metadata is None
    assert missing.status == "malformed"


def test_duplicate_and_conflicting_order_records_are_reported():
    identical = _fixture(orders=[_fixture()["order_metadata"][SYMBOL]["orders"][0]] * 2)
    rows, status = FixtureOrderMetadataTransport(identical).fetch_orders_page(SYMBOL)
    assert len(rows) == 1
    assert status.duplicate_count == 1
    conflict_rows = [_fixture()["order_metadata"][SYMBOL]["orders"][0], {**_fixture()["order_metadata"][SYMBOL]["orders"][0], "price": "999"}]
    _, conflict_status = FixtureOrderMetadataTransport(_fixture(orders=conflict_rows)).fetch_orders_page(SYMBOL)
    assert conflict_status.status == "conflicting"
    assert conflict_status.conflict_count == 1
    assert conflict_status.error_code == "conflicting_duplicate"


def test_incomplete_pagination_and_unconfigured_transport():
    _, status = FixtureOrderMetadataTransport(_fixture(pagination_complete=False, next_cursor="page-2")).fetch_orders_page(SYMBOL)
    assert status.pagination_complete is False
    assert status.next_cursor == "page-2"
    transport = UnconfiguredOrderMetadataTransport()
    assert transport.fetch_order_by_id(SYMBOL, "7").status == "unconfigured"
    assert transport.fetch_orders_page(SYMBOL)[1].error_code == "authenticated_transport_unconfigured"
    for name in ("login", "sign", "authenticate", "post", "submit_order", "cancel_order", "replace_order"):
        assert not hasattr(transport, name)


def test_taker_maker_correlation_matches_owner_and_validates_quantity_price_remaining():
    resolver = OrderMetadataResolver(FixtureOrderMetadataTransport(_fixture()), symbol=SYMBOL, expected_account=OWNER)
    report = resolver.resolve_fills([_fill()])
    assert report.status == "matched"
    assert report.account_match is True
    correlation = report.correlations[0]
    assert correlation.status == "matched"
    assert correlation.owner_match is True
    assert correlation.market_match is True
    assert correlation.quantity_valid is True
    assert correlation.price_valid is True
    assert correlation.remaining_quantity_consistent is True


def test_correlation_wrong_account_market_and_inconsistent_values_are_not_authoritative():
    wrong_account = OrderMetadataResolver(FixtureOrderMetadataTransport(_fixture()), symbol=SYMBOL, expected_account=OTHER).resolve_fills([_fill()])
    assert wrong_account.account_match is False
    assert wrong_account.status == "partial_match"
    wrong_market_fixture = {"order_metadata": {"OTHER:USDso": {"status": "available", "orders": _fixture()["order_metadata"][SYMBOL]["orders"]}}}
    wrong_market = OrderMetadataResolver(FixtureOrderMetadataTransport(wrong_market_fixture), symbol="OTHER:USDso", expected_account=OWNER).resolve_fills([_fill(symbol="OTHER:USDso")])
    assert wrong_market.correlations[0].market_match is False
    invalid_quantity = OrderMetadataResolver(FixtureOrderMetadataTransport(_fixture()), symbol=SYMBOL, expected_account=OWNER).resolve_fills([_fill(quantity=Decimal("9"))])
    assert invalid_quantity.correlations[0].quantity_valid is False
    assert invalid_quantity.status == "partial_match"
    invalid_price = OrderMetadataResolver(FixtureOrderMetadataTransport(_fixture()), symbol=SYMBOL, expected_account=OWNER).resolve_fills([_fill(price=Decimal("999"))])
    assert invalid_price.correlations[0].price_valid is False


def test_missing_owner_or_side_stays_unresolved_and_no_secrets_are_logged():
    fixture = _fixture(orders=[{"id": "7", "symbol": SYMBOL, "price": "123.45", "quantity": "2", "remaining": "2"}, {"id": "8", "symbol": SYMBOL, "price": "123.45", "quantity": "2", "remaining": "2"}])
    report = OrderMetadataResolver(FixtureOrderMetadataTransport(fixture), symbol=SYMBOL, expected_account=OWNER).resolve_fills([_fill()])
    assert report.account_match is None
    assert report.status == "partial_match"
    assert OWNER not in repr(report)
    assert not hasattr(FixtureOrderMetadataTransport(fixture), "sign")
