from decimal import Decimal

from bot.market.market_cache import MarketCache
from bot.market.models import OrderBook, OrderBookLevel
from bot.portfolio.portfolio_manager import PortfolioManager
from bot.strategy.passive_market_maker import PassiveMarketMakerStrategy


def make_market_cache() -> MarketCache:
    cache = MarketCache()

    cache.update_orderbook(
        OrderBook(
            symbol="SOMI:USDso",
            bids=[
                OrderBookLevel(price=Decimal("1.00"), quantity=Decimal("100")),
            ],
            asks=[
                OrderBookLevel(price=Decimal("1.02"), quantity=Decimal("100")),
            ],
            timestamp=12345,
        )
    )

    return cache


def test_strategy_generates_buy_order_without_inventory():
    market = make_market_cache()
    portfolio = PortfolioManager(initial_cash=Decimal("150"))

    strategy = PassiveMarketMakerStrategy(
        symbol="SOMI:USDso",
        order_size_usd=Decimal("5"),
    )

    orders = strategy.generate_orders(market=market, portfolio=portfolio)

    assert len(orders) == 1

    buy_order = orders[0]

    assert buy_order.side == "buy"
    assert buy_order.order_type == "limit"
    assert buy_order.price == Decimal("1.00")
    assert buy_order.notional == Decimal("5")


def test_strategy_generates_buy_and_sell_with_inventory():
    market = make_market_cache()
    portfolio = PortfolioManager(initial_cash=Decimal("150"))

    portfolio.buy(price=Decimal("1.00"), quantity=Decimal("10"))

    strategy = PassiveMarketMakerStrategy(
        symbol="SOMI:USDso",
        order_size_usd=Decimal("5"),
    )

    orders = strategy.generate_orders(market=market, portfolio=portfolio)

    assert len(orders) == 2

    buy_order = orders[0]
    sell_order = orders[1]

    assert buy_order.side == "buy"
    assert sell_order.side == "sell"

    assert buy_order.price == Decimal("1.00")
    assert sell_order.price == Decimal("1.02")

    assert buy_order.notional == Decimal("5")
    assert sell_order.notional <= Decimal("5")


def test_strategy_returns_no_orders_without_orderbook():
    market = MarketCache()
    portfolio = PortfolioManager(initial_cash=Decimal("150"))

    strategy = PassiveMarketMakerStrategy(
        symbol="SOMI:USDso",
        order_size_usd=Decimal("5"),
    )

    orders = strategy.generate_orders(market=market, portfolio=portfolio)

    assert orders == []