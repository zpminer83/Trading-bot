from bot.market.market_cache import MarketCache


class MarketService:

    def __init__(self):

        self.cache = MarketCache()

    def on_orderbook(self, orderbook):

        self.cache.update_orderbook(orderbook)

    def on_ticker(self, ticker):

        self.cache.update_ticker(ticker)