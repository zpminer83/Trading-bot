from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from bot.execution.order import OrderDecision, OrderIntent
from bot.execution.paper_broker import PaperFill
from bot.market.market_cache import MarketCache
from bot.portfolio.portfolio_manager import PortfolioManager


PaperOrderStatus = Literal["open", "filled", "cancelled"]


@dataclass
class PaperOrder:
    order_id: int
    intent: OrderIntent
    status: PaperOrderStatus = "open"


class ConservativePaperBroker:
    """
    More realistic paper broker.

    It does not instantly fill passive limit orders.

    Fill rules:
    - buy limit fills only if limit price >= current best ask
    - sell limit fills only if limit price <= current best bid
    """

    def __init__(self, portfolio: PortfolioManager):
        self.portfolio = portfolio
        self.open_orders: list[PaperOrder] = []
        self.fills: list[PaperFill] = []
        self._next_order_id = 1

    def submit(self, decision: OrderDecision) -> PaperOrder | None:
        if not decision.approved:
            return None

        order = PaperOrder(
            order_id=self._next_order_id,
            intent=decision.intent,
        )

        self._next_order_id += 1
        self.open_orders.append(order)

        return order

    def process_market(self, market: MarketCache) -> list[PaperFill]:
        new_fills: list[PaperFill] = []
        still_open: list[PaperOrder] = []

        for order in self.open_orders:
            fill = self._try_fill_order(order=order, market=market)

            if fill is None:
                still_open.append(order)
                continue

            order.status = "filled"
            self.fills.append(fill)
            new_fills.append(fill)

        self.open_orders = still_open

        return new_fills

    def cancel_all(self) -> None:
        for order in self.open_orders:
            order.status = "cancelled"

        self.open_orders.clear()

    def _try_fill_order(
        self,
        order: PaperOrder,
        market: MarketCache,
    ) -> PaperFill | None:
        intent = order.intent

        if intent.side == "buy":
            best_ask = market.best_ask(intent.symbol)

            if best_ask is None:
                return None

            if intent.price < best_ask.price:
                return None

            fill_price = best_ask.price
            quantity = intent.quantity

            self.portfolio.buy(price=fill_price, quantity=quantity)

            return PaperFill(
                symbol=intent.symbol,
                side="buy",
                price=fill_price,
                quantity=quantity,
                notional=fill_price * quantity,
            )

        if intent.side == "sell":
            best_bid = market.best_bid(intent.symbol)

            if best_bid is None:
                return None

            if intent.price > best_bid.price:
                return None

            quantity = min(intent.quantity, self.portfolio.base_position)

            if quantity <= 0:
                return None

            fill_price = best_bid.price

            self.portfolio.sell(price=fill_price, quantity=quantity)

            return PaperFill(
                symbol=intent.symbol,
                side="sell",
                price=fill_price,
                quantity=quantity,
                notional=fill_price * quantity,
            )

        raise ValueError(f"Unsupported order side: {intent.side}")