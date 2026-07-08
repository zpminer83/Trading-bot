from dataclasses import dataclass
from decimal import Decimal

from bot.execution.order import OrderDecision, OrderIntent
from bot.portfolio.portfolio_manager import PortfolioManager


@dataclass(frozen=True)
class PaperFill:
    symbol: str
    side: str
    price: Decimal
    quantity: Decimal
    notional: Decimal


class PaperBroker:
    """
    Simulates order execution without sending real orders.

    This is intentionally simple for now:
    - approved buy order is filled at intent.price
    - approved sell order is filled at intent.price
    - rejected orders are never executed
    """

    def __init__(self, portfolio: PortfolioManager):
        self.portfolio = portfolio
        self.fills: list[PaperFill] = []

    def execute(self, decision: OrderDecision) -> PaperFill | None:
        if not decision.approved:
            return None

        intent = decision.intent

        if intent.side == "buy":
            self.portfolio.buy(
                price=intent.price,
                quantity=intent.quantity,
            )

        elif intent.side == "sell":
            self.portfolio.sell(
                price=intent.price,
                quantity=intent.quantity,
            )

        else:
            raise ValueError(f"Unsupported order side: {intent.side}")

        fill = self._create_fill(intent)
        self.fills.append(fill)

        return fill

    def _create_fill(self, intent: OrderIntent) -> PaperFill:
        return PaperFill(
            symbol=intent.symbol,
            side=intent.side,
            price=intent.price,
            quantity=intent.quantity,
            notional=intent.notional,
        )