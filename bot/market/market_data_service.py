from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from bot.adapters.dreamdex_market_adapter import DreamDexMarketAdapter
from bot.market.market_cache import MarketCache
from bot.market.models import OrderBook, OrderBookLevel


@dataclass(frozen=True)
class MarketDataSnapshot:
    symbol: str
    orderbook: OrderBook
    best_bid: OrderBookLevel | None
    best_ask: OrderBookLevel | None
    spread: Decimal | None
    mid_price: Decimal | None


class MarketDataService:
    """
    Owns market data ingestion into MarketCache.

    Current responsibility:
    - accept raw DreamDEX-like orderbook payloads
    - parse them through DreamDexMarketAdapter
    - update MarketCache
    - return a clean snapshot for the rest of the bot

    Strategy and trading engines should not care whether data came from:
    - REST
    - WebSocket
    - tests
    - local demo scenarios
    """

    def __init__(
        self,
        market_cache: MarketCache,
        adapter: type[DreamDexMarketAdapter] = DreamDexMarketAdapter,
    ):
        self.market_cache = market_cache
        self.adapter = adapter

    def handle_orderbook_payload(
        self,
        payload: dict[str, Any],
        default_symbol: str | None = None,
    ) -> MarketDataSnapshot:
        orderbook = self.adapter.update_cache_from_orderbook(
            market_cache=self.market_cache,
            payload=payload,
            default_symbol=default_symbol,
        )

        return self.snapshot(orderbook.symbol)

    def snapshot(self, symbol: str) -> MarketDataSnapshot:
        orderbook = self.market_cache.get_orderbook(symbol)

        if orderbook is None:
            raise ValueError(f"market data is missing for symbol: {symbol}")

        return MarketDataSnapshot(
            symbol=symbol,
            orderbook=orderbook,
            best_bid=self.market_cache.best_bid(symbol),
            best_ask=self.market_cache.best_ask(symbol),
            spread=self.market_cache.spread(symbol),
            mid_price=self.market_cache.mid_price(symbol),
        )

    def has_market(self, symbol: str) -> bool:
        return self.market_cache.get_orderbook(symbol) is not None