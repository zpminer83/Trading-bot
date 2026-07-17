from collections.abc import Iterable
from collections.abc import Callable

from bot.execution.conservative_paper_broker import ConservativePaperBroker, PaperOrder
from bot.execution.order import OrderDecision


class OrderManager:
    """
    Manages open paper orders.

    Current conservative policy:
    - process existing orders first in the broker
    - then cancel remaining stale orders
    - then submit the new approved decisions

    This prevents old passive orders from accumulating forever.
    """

    def __init__(
        self,
        broker: ConservativePaperBroker,
        max_open_orders: int = 2,
    ):
        if max_open_orders <= 0:
            raise ValueError("max_open_orders must be greater than zero")

        self.broker = broker
        self.max_open_orders = max_open_orders

    def replace_orders(
        self,
        decisions: Iterable[OrderDecision],
    ) -> list[PaperOrder]:
        """
        Cancel all existing open orders and submit approved new orders.
        """
        self.cancel_all()

        submitted_orders: list[PaperOrder] = []

        for decision in decisions:
            if not decision.approved:
                continue

            if len(submitted_orders) >= self.max_open_orders:
                break

            order = self.broker.submit(decision)

            if order is not None:
                submitted_orders.append(order)

        return submitted_orders

    def cancel_all(self) -> None:
        self.broker.cancel_all()

    def cancel_all_except(
        self,
        keep: Callable[[PaperOrder], bool],
    ) -> None:
        self.broker.cancel_all_except(keep)

    def cancel_risk_increasing(self) -> None:
        """Cancel only orders that could increase current exposure.

        Reduce-only and explicit risk-exit orders remain under the existing
        broker lifecycle.  The long-only paper portfolio treats buys as
        exposure-increasing while inventory is non-negative and sells as
        exposure-increasing only for a short position.
        """
        position = self.broker.portfolio.base_position

        def keep(order: PaperOrder) -> bool:
            purpose = order.intent.purpose.value
            if purpose in {"risk_exit", "risk_reduction", "take_profit", "signal_exit", "stop_loss"}:
                return True
            if position > 0 and order.intent.side == "sell":
                return True
            if position < 0 and order.intent.side == "buy":
                return True
            return False

        self.broker.cancel_all_except(keep)

    @property
    def open_order_count(self) -> int:
        return len(self.broker.open_orders)
