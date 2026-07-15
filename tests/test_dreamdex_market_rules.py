from decimal import Decimal

import pytest

from bot.integrations.dreamdex_market_rules import fingerprint_market_payload, parse_market_trading_rules
from bot.integrations.dreamdex_read_only import FixtureTransport, MarketReadOnlySource


BASE = {
    "symbol": "SOMI:USDso",
    "contract": "0x035De7403eac6872787779CCA7CCF1b4CDb61379",
    "base": "0x28f34DeFd2b4CB48d9eE6d89f2Be4Bc601694c00",
    "quote": "0x00000022dA000002656c64D9eA6011ea952D008A",
    "baseDecimals": 18,
    "quoteDecimals": 18,
    "tickSize": "0.0001",
    "lotSize": "0.01",
    "minQuantity": "1",
    "stopRegistry": "0x68c8f6fb1EA19A28F25358Ff00b8Ed8E1216df30",
}


def test_live_markets_mapping_is_decimal_and_separates_unavailable_rules():
    rules = parse_market_trading_rules(BASE, symbol="SOMI:USDso")
    assert rules.market_address == BASE["contract"]
    assert rules.base_token_address == BASE["base"]
    assert rules.quote_token_address == BASE["quote"]
    assert rules.stop_registry == BASE["stopRegistry"]
    assert rules.tick_size == Decimal("0.0001")
    assert rules.quantity_step == Decimal("0.01")
    assert rules.minimum_quantity == Decimal("1")
    assert rules.minimum_notional is None
    assert rules.market_status is None
    assert rules.status_for("minimum_notional") == "unavailable"
    assert rules.status_for("market_status") == "unavailable"
    assert rules.evidence_for("tick_size").source == "both_confirmed"


def test_missing_and_unknown_status_are_fail_closed():
    missing = parse_market_trading_rules(BASE, symbol="SOMI:USDso")
    assert missing.trading_enabled is False
    unknown = parse_market_trading_rules({**BASE, "status": "maintenance"}, symbol="SOMI:USDso")
    assert unknown.market_status == "unknown"
    assert unknown.trading_enabled is False
    assert unknown.status_for("market_status") == "unsupported"


@pytest.mark.parametrize("field,value", [("tickSize", "0"), ("lotSize", "-1"), ("minQuantity", "0"), ("baseDecimals", True)])
def test_invalid_rule_values_are_rejected(field, value):
    with pytest.raises(ValueError):
        parse_market_trading_rules({**BASE, field: value}, symbol="SOMI:USDso")


def test_conflicting_duplicate_market_rows_are_not_silently_selected():
    first = FixtureTransport({"markets": {"markets": [BASE, {**BASE, "tickSize": "0.0002"}]}})
    metadata = MarketReadOnlySource(first, "SOMI:USDso").metadata()
    assert metadata.trading_rules is not None
    assert metadata.trading_rules.source_status == "conflicting"
    assert "tickSize" in metadata.trading_rules.conflicts
    assert metadata.trading_rules.status_for("tick_size") == "conflicting"


def test_legacy_fixture_alias_is_not_used_by_production_parser():
    alias_row = {key: value for key, value in BASE.items() if key not in {"lotSize", "tickSize"}}
    alias_row["quantityStepSize"] = "0.01"
    alias_row["tick_size"] = "0.0001"
    rules = parse_market_trading_rules(alias_row, symbol="SOMI:USDso")
    assert rules.status_for("tick_size") == "unavailable"
    assert rules.status_for("quantity_step") == "unavailable"
    fixture = FixtureTransport({"markets": {"markets": [alias_row]}})
    metadata = MarketReadOnlySource(fixture, "SOMI:USDso").metadata()
    assert metadata.price_tick_size == Decimal("0.0001")
    assert metadata.quantity_step == Decimal("0.01")


def test_public_schema_fingerprint_contains_structure_only_and_depth_limit():
    fingerprint = fingerprint_market_payload({"markets": [{"symbol": "SOMI:USDso", "contract": "0xsecret", "deep": {"a": {"b": {"c": {"d": "hidden"}}}}}]})
    rendered = repr(fingerprint)
    assert fingerprint.endpoint_name == "/markets"
    assert fingerprint.top_level_type == "object"
    assert "0xsecret" not in rendered and "hidden" not in rendered
    assert any(name.endswith("markets[0].symbol") for name in fingerprint.nested_field_names)
    assert not any(name.endswith(".d") for name in fingerprint.nested_field_names)
