from decimal import Decimal

from bot.market.market_cache import MarketCache
from bot.market.models import OrderBook, OrderBookLevel


def test_update_orderbook():

    cache = MarketCache()

    orderbook = OrderBook(
        symbol="SOMI:USDso",
        bids=[
            OrderBookLevel(
                price=Decimal("1.20"),
                quantity=Decimal("100")
            )
        ],
        asks=[
            OrderBookLevel(
                price=Decimal("1.22"),
                quantity=Decimal("200")
            )
        ],
        timestamp=12345,
    )

    cache.update_orderbook(orderbook)

    assert cache.get_orderbook("SOMI:USDso") == orderbook