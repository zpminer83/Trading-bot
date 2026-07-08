from dataclasses import dataclass
from decimal import Decimal
from typing import Literal


OrderSide = Literal["buy", "sell"]
OrderType = Literal["limit", "market"]


@dataclass(frozen=True)
class OrderIntent:
    symbol: str
    side: OrderSide
    order_type: OrderType
    price: Decimal
    quantity: Decimal

    @property
    def notional(self) -> Decimal:
        return self.price * self.quantity


@dataclass(frozen=True)
class OrderDecision:
    approved: bool
    reason: str
    intent: OrderIntent