from decimal import Decimal

from bot.execution.order import OrderIntent
from bot.market.market_cache import MarketCache
from bot.portfolio.portfolio_manager import PortfolioManager


class PassiveMarketMakerStrategy:
    """
    Very conservative market-making strategy.

    It does not submit orders.
    It only proposes OrderIntent objects.

    ExecutionManager and RiskManager decide whether these orders are allowed.
    """

    def __init__(
        self,
        symbol: str,
        order_size_usd: Decimal,
    ):
        self.symbol = symbol
        self.order_size_usd = order_size_usd

    def generate_orders(
        self,
        market: MarketCache,
        portfolio: PortfolioManager,
    ) -> list[OrderIntent]:
        best_bid = market.best_bid(self.symbol)
        best_ask = market.best_ask(self.symbol)

        if best_bid is None or best_ask is None:
            return []

        orders: list[OrderIntent] = []

        # Passive buy at best bid
        buy_quantity = self.order_size_usd / best_bid.price

        orders.append(
            OrderIntent(
                symbol=self.symbol,
                side="buy",
                order_type="limit",
                price=best_bid.price,
                quantity=buy_quantity,
            )
        )

        # Passive sell at best ask, only if we already have inventory
        if portfolio.base_position > 0:
            sell_quantity = min(
                portfolio.base_position,
                self.order_size_usd / best_ask.price,
            )

            if sell_quantity > 0:
                orders.append(
                    OrderIntent(
                        symbol=self.symbol,
                        side="sell",
                        order_type="limit",
                        price=best_ask.price,
                        quantity=sell_quantity,
                    )
                )

        return orders