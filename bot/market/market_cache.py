from decimal import Decimal
from bot.market.models import OrderBook, Ticker


class MarketCache:

    def __init__(self):
        self.orderbooks: dict[str, OrderBook] = {}
        self.tickers: dict[str, Ticker] = {}

    def update_orderbook(self, orderbook: OrderBook):
        self.orderbooks[orderbook.symbol] = orderbook

    def get_orderbook(self, symbol: str):
        return self.orderbooks.get(symbol)

    def update_ticker(self, ticker: Ticker):
        self.tickers[ticker.symbol] = ticker

    def get_ticker(self, symbol: str):
        return self.tickers.get(symbol)

    # -------------------------
    # Удобные методы
    # -------------------------

    def best_bid(self, symbol: str):
        ob = self.get_orderbook(symbol)

        if ob is None or not ob.bids:
            return None

        return ob.bids[0]

    def best_ask(self, symbol: str):
        ob = self.get_orderbook(symbol)

        if ob is None or not ob.asks:
            return None

        return ob.asks[0]

    def spread(self, symbol: str):
        bid = self.best_bid(symbol)
        ask = self.best_ask(symbol)

        if bid is None or ask is None:
            return None

        return ask.price - bid.price

    def mid_price(self, symbol: str):
        bid = self.best_bid(symbol)
        ask = self.best_ask(symbol)

        if bid is None or ask is None:
            return None

        return (ask.price + bid.price) / Decimal("2")