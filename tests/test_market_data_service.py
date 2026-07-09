from decimal import Decimal

import pytest

from bot.market.market_cache import MarketCache
from bot.market.market_data_service import MarketDataService


def make_payload():
    return {
        "symbol": "SOMI:USDso",
        "bids": [
            ["1.00", "10"],
            ["0.99", "5"],
        ],
        "asks": [
            ["1.02", "8"],
            ["1.03", "4"],
        ],
        "timestamp": 12345,
        "nonce": "abc",
    }


def test_market_data_service_updates_cache_from_orderbook_payload():
    market_cache = MarketCache()
    service = MarketDataService(market_cache=market_cache)

    snapshot = service.handle_orderbook_payload(make_payload())

    assert snapshot.symbol == "SOMI:USDso"
    assert snapshot.orderbook.symbol == "SOMI:USDso"

    cached = market_cache.get_orderbook("SOMI:USDso")

    assert cached is not None
    assert cached == snapshot.orderbook


def test_market_data_service_returns_clean_snapshot():
    market_cache = MarketCache()
    service = MarketDataService(market_cache=market_cache)

    snapshot = service.handle_orderbook_payload(make_payload())

    assert snapshot.best_bid is not None
    assert snapshot.best_ask is not None

    assert snapshot.best_bid.price == Decimal("1.00")
    assert snapshot.best_ask.price == Decimal("1.02")
    assert snapshot.spread == Decimal("0.02")
    assert snapshot.mid_price == Decimal("1.01")


def test_market_data_service_supports_default_symbol_for_ws_payload():
    market_cache = MarketCache()
    service = MarketDataService(market_cache=market_cache)

    payload = {
        "type": "orderbook",
        "data": {
            "bids": [
                ["1.00", "10"],
            ],
            "asks": [
                ["1.02", "8"],
            ],
            "ts": 999,
            "sequence": 123,
        },
    }

    snapshot = service.handle_orderbook_payload(
        payload=payload,
        default_symbol="SOMI:USDso",
    )

    assert snapshot.symbol == "SOMI:USDso"
    assert snapshot.orderbook.timestamp == 999
    assert snapshot.orderbook.nonce == "123"
    assert service.has_market("SOMI:USDso") is True


def test_market_data_service_replaces_existing_orderbook():
    market_cache = MarketCache()
    service = MarketDataService(market_cache=market_cache)

    service.handle_orderbook_payload(make_payload())

    updated_payload = {
        "symbol": "SOMI:USDso",
        "bids": [
            ["1.10", "20"],
        ],
        "asks": [
            ["1.12", "30"],
        ],
        "timestamp": 222,
        "nonce": "new",
    }

    snapshot = service.handle_orderbook_payload(updated_payload)

    assert snapshot.best_bid is not None
    assert snapshot.best_ask is not None

    assert snapshot.best_bid.price == Decimal("1.10")
    assert snapshot.best_bid.quantity == Decimal("20")
    assert snapshot.best_ask.price == Decimal("1.12")
    assert snapshot.best_ask.quantity == Decimal("30")
    assert snapshot.orderbook.timestamp == 222
    assert snapshot.orderbook.nonce == "new"


def test_market_data_service_snapshot_rejects_missing_market():
    market_cache = MarketCache()
    service = MarketDataService(market_cache=market_cache)

    with pytest.raises(ValueError):
        service.snapshot("SOMI:USDso")


def test_market_data_service_propagates_invalid_payload_errors():
    market_cache = MarketCache()
    service = MarketDataService(market_cache=market_cache)

    payload = {
        "symbol": "SOMI:USDso",
        "bids": [
            ["bad-price", "10"],
        ],
        "asks": [
            ["1.02", "8"],
        ],
    }

    with pytest.raises(ValueError):
        service.handle_orderbook_payload(payload)