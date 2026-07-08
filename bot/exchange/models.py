from pydantic import BaseModel


class MarketInfo(BaseModel):
    symbol: str
    base: str
    quote: str
    contract: str
    lotSize: str
    tickSize: str
    minQuantity: str
    baseDecimals: int
    quoteDecimals: int
    stopRegistry: str


class MarketsResponse(BaseModel):
    markets: list[MarketInfo]