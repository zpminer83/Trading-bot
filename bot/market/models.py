from dataclasses import dataclass, field
from decimal import Decimal
from datetime import datetime


@dataclass(slots=True)
class OrderBookLevel:
    price: Decimal
    quantity: Decimal


@dataclass(slots=True)
class OrderBook:
    symbol: str
    bids: list[OrderBookLevel] = field(default_factory=list)
    asks: list[OrderBookLevel] = field(default_factory=list)
    timestamp: int = 0
    nonce: str = ""


@dataclass(slots=True)
class Ticker:
    symbol: str
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    timestamp: int


@dataclass(slots=True)
class Trade:
    price: Decimal
    quantity: Decimal
    side: str
    timestamp: int