from decimal import Decimal

from bot.market.market_cache import MarketCache
from bot.market.models import OrderBook, OrderBookLevel


def make_orderbook() -> OrderBook:
    return OrderBook(
        symbol="SOMI:USDso",
        bids=[
            OrderBookLevel(price=Decimal("1.20"), quantity=Decimal("100")),
            OrderBookLevel(price=Decimal("1.19"), quantity=Decimal("200")),
        ],
        asks=[
            OrderBookLevel(price=Decimal("1.22"), quantity=Decimal("150")),
            OrderBookLevel(price=Decimal("1.23"), quantity=Decimal("250")),
        ],
        timestamp=12345,
    )


def test_update_orderbook():
    cache = MarketCache()
    orderbook = make_orderbook()

    cache.update_orderbook(orderbook)

    assert cache.get_orderbook("SOMI:USDso") == orderbook


def test_best_bid():
    cache = MarketCache()
    cache.update_orderbook(make_orderbook())

    best_bid = cache.best_bid("SOMI:USDso")

    assert best_bid is not None
    assert best_bid.price == Decimal("1.20")
    assert best_bid.quantity == Decimal("100")


def test_best_ask():
    cache = MarketCache()
    cache.update_orderbook(make_orderbook())

    best_ask = cache.best_ask("SOMI:USDso")

    assert best_ask is not None
    assert best_ask.price == Decimal("1.22")
    assert best_ask.quantity == Decimal("150")


def test_spread():
    cache = MarketCache()
    cache.update_orderbook(make_orderbook())

    assert cache.spread("SOMI:USDso") == Decimal("0.02")


def test_mid_price():
    cache = MarketCache()
    cache.update_orderbook(make_orderbook())

    assert cache.mid_price("SOMI:USDso") == Decimal("1.21")