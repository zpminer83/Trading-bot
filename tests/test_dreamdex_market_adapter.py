from decimal import Decimal

import pytest

from bot.adapters.dreamdex_market_adapter import DreamDexMarketAdapter
from bot.market.market_cache import MarketCache


def test_parse_orderbook_from_list_levels():
    payload = {
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

    orderbook = DreamDexMarketAdapter.parse_orderbook(payload)

    assert orderbook.symbol == "SOMI:USDso"
    assert orderbook.timestamp == 12345
    assert orderbook.nonce == "abc"

    assert orderbook.bids[0].price == Decimal("1.00")
    assert orderbook.bids[0].quantity == Decimal("10")

    assert orderbook.asks[0].price == Decimal("1.02")
    assert orderbook.asks[0].quantity == Decimal("8")


def test_parse_orderbook_from_dict_levels():
    payload = {
        "symbol": "SOMI:USDso",
        "bids": [
            {
                "price": "1.00",
                "quantity": "10",
            }
        ],
        "asks": [
            {
                "price": "1.02",
                "size": "8",
            }
        ],
    }

    orderbook = DreamDexMarketAdapter.parse_orderbook(payload)

    assert orderbook.bids[0].price == Decimal("1.00")
    assert orderbook.bids[0].quantity == Decimal("10")

    assert orderbook.asks[0].price == Decimal("1.02")
    assert orderbook.asks[0].quantity == Decimal("8")


def test_parse_orderbook_sorts_bids_and_asks():
    payload = {
        "symbol": "SOMI:USDso",
        "bids": [
            ["0.98", "10"],
            ["1.00", "10"],
            ["0.99", "10"],
        ],
        "asks": [
            ["1.04", "10"],
            ["1.02", "10"],
            ["1.03", "10"],
        ],
    }

    orderbook = DreamDexMarketAdapter.parse_orderbook(payload)

    assert [level.price for level in orderbook.bids] == [
        Decimal("1.00"),
        Decimal("0.99"),
        Decimal("0.98"),
    ]

    assert [level.price for level in orderbook.asks] == [
        Decimal("1.02"),
        Decimal("1.03"),
        Decimal("1.04"),
    ]


def test_parse_orderbook_from_nested_ws_payload_with_default_symbol():
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

    orderbook = DreamDexMarketAdapter.parse_orderbook(
        payload=payload,
        default_symbol="SOMI:USDso",
    )

    assert orderbook.symbol == "SOMI:USDso"
    assert orderbook.timestamp == 999
    assert orderbook.nonce == "123"


def test_parse_orderbook_from_price_quantity_mapping():
    payload = {
        "symbol": "SOMI:USDso",
        "bids": {
            "1.00": "10",
            "0.99": "5",
        },
        "asks": {
            "1.02": "8",
            "1.03": "4",
        },
    }

    orderbook = DreamDexMarketAdapter.parse_orderbook(payload)

    assert len(orderbook.bids) == 2
    assert len(orderbook.asks) == 2

    assert orderbook.bids[0].price == Decimal("1.00")
    assert orderbook.bids[0].quantity == Decimal("10")

    assert orderbook.asks[0].price == Decimal("1.02")
    assert orderbook.asks[0].quantity == Decimal("8")


def test_parse_orderbook_skips_zero_quantity_levels():
    payload = {
        "symbol": "SOMI:USDso",
        "bids": [
            ["1.00", "0"],
            ["0.99", "5"],
        ],
        "asks": [
            ["1.02", "0"],
            ["1.03", "4"],
        ],
    }

    orderbook = DreamDexMarketAdapter.parse_orderbook(payload)

    assert len(orderbook.bids) == 1
    assert len(orderbook.asks) == 1

    assert orderbook.bids[0].price == Decimal("0.99")
    assert orderbook.asks[0].price == Decimal("1.03")


def test_update_cache_from_orderbook():
    market_cache = MarketCache()

    payload = {
        "symbol": "SOMI:USDso",
        "bids": [
            ["1.00", "10"],
        ],
        "asks": [
            ["1.02", "8"],
        ],
    }

    orderbook = DreamDexMarketAdapter.update_cache_from_orderbook(
        market_cache=market_cache,
        payload=payload,
    )

    cached = market_cache.get_orderbook("SOMI:USDso")

    assert cached == orderbook
    assert market_cache.best_bid("SOMI:USDso").price == Decimal("1.00")
    assert market_cache.best_ask("SOMI:USDso").price == Decimal("1.02")
    assert market_cache.mid_price("SOMI:USDso") == Decimal("1.01")


def test_parse_orderbook_rejects_missing_symbol():
    payload = {
        "bids": [
            ["1.00", "10"],
        ],
        "asks": [
            ["1.02", "8"],
        ],
    }

    with pytest.raises(ValueError):
        DreamDexMarketAdapter.parse_orderbook(payload)


def test_parse_orderbook_rejects_invalid_level():
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
        DreamDexMarketAdapter.parse_orderbook(payload)