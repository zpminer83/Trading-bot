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