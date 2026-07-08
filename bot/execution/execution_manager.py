from bot.execution.order import OrderDecision, OrderIntent
from bot.portfolio.portfolio_manager import PortfolioManager
from bot.risk.risk_manager import RiskManager


class ExecutionManager:
    """
    Gatekeeper for all order submissions.

    Strategy creates OrderIntent.
    ExecutionManager checks it with RiskManager.
    Only approved orders may be sent to the exchange adapter.
    """

    def __init__(
        self,
        portfolio: PortfolioManager,
        risk_manager: RiskManager,
    ):
        self.portfolio = portfolio
        self.risk_manager = risk_manager

    def review_order(self, intent: OrderIntent) -> OrderDecision:
        approved, reason = self.risk_manager.can_submit_order(
            portfolio=self.portfolio,
            side=intent.side,
            notional=intent.notional,
        )

        return OrderDecision(
            approved=approved,
            reason=reason,
            intent=intent,
        )