from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Literal

from bot.portfolio.portfolio_manager import PortfolioManager


OrderSide = Literal["buy", "sell"]


class RiskMode(Enum):
    NORMAL = "normal"
    CAUTION = "caution"
    DEFENSIVE = "defensive"
    SURVIVAL = "survival"
    STOP = "stop"


@dataclass(frozen=True)
class RiskLimits:
    caution_drawdown: Decimal = Decimal("0.03")
    defensive_drawdown: Decimal = Decimal("0.05")
    survival_drawdown: Decimal = Decimal("0.07")
    max_drawdown: Decimal = Decimal("0.10")

    max_position_percent: Decimal = Decimal("0.40")
    survival_position_percent: Decimal = Decimal("0.50")

    base_order_size_usd: Decimal = Decimal("5")


@dataclass(frozen=True)
class RiskDecision:
    mode: RiskMode
    allow_buy: bool
    allow_sell: bool
    order_size_multiplier: Decimal
    max_order_notional: Decimal
    reasons: list[str] = field(default_factory=list)


class RiskManager:
    """
    Adaptive risk manager.

    It should reduce aggression before stopping the bot.
    STOP is the last resort, not the first reaction.
    """

    def __init__(self, limits: RiskLimits | None = None):
        self.limits = limits or RiskLimits()

    def evaluate(self, portfolio: PortfolioManager) -> RiskDecision:
        reasons: list[str] = []

        drawdown = portfolio.drawdown
        position_exposure = self._position_exposure(portfolio)

        if drawdown >= self.limits.max_drawdown:
            reasons.append("max_drawdown_reached")

            return RiskDecision(
                mode=RiskMode.STOP,
                allow_buy=False,
                allow_sell=False,
                order_size_multiplier=Decimal("0"),
                max_order_notional=Decimal("0"),
                reasons=reasons,
            )

        if (
            drawdown >= self.limits.survival_drawdown
            or position_exposure >= self.limits.survival_position_percent
        ):
            reasons.append("survival_threshold_reached")

            return RiskDecision(
                mode=RiskMode.SURVIVAL,
                allow_buy=False,
                allow_sell=True,
                order_size_multiplier=Decimal("0"),
                max_order_notional=Decimal("0"),
                reasons=reasons,
            )

        if (
            drawdown >= self.limits.defensive_drawdown
            or position_exposure >= self.limits.max_position_percent
        ):
            reasons.append("defensive_threshold_reached")

            return RiskDecision(
                mode=RiskMode.DEFENSIVE,
                allow_buy=position_exposure < self.limits.max_position_percent,
                allow_sell=True,
                order_size_multiplier=Decimal("0.40"),
                max_order_notional=self.limits.base_order_size_usd * Decimal("0.40"),
                reasons=reasons,
            )

        if drawdown >= self.limits.caution_drawdown:
            reasons.append("caution_threshold_reached")

            return RiskDecision(
                mode=RiskMode.CAUTION,
                allow_buy=True,
                allow_sell=True,
                order_size_multiplier=Decimal("0.70"),
                max_order_notional=self.limits.base_order_size_usd * Decimal("0.70"),
                reasons=reasons,
            )

        return RiskDecision(
            mode=RiskMode.NORMAL,
            allow_buy=True,
            allow_sell=True,
            order_size_multiplier=Decimal("1"),
            max_order_notional=self.limits.base_order_size_usd,
            reasons=reasons,
        )

    def can_submit_order(
        self,
        portfolio: PortfolioManager,
        side: OrderSide,
        notional: Decimal,
    ) -> tuple[bool, str]:
        if notional <= 0:
            return False, "order_notional_must_be_positive"

        decision = self.evaluate(portfolio)

        if decision.mode == RiskMode.STOP:
            return False, "bot_is_stopped_by_risk_manager"

        if side == "buy":
            if not decision.allow_buy:
                return False, "buy_orders_are_disabled"

            if notional > decision.max_order_notional:
                return False, "order_notional_exceeds_risk_limit"

            if notional > portfolio.cash_balance:
                return False, "insufficient_cash_balance"

            projected_position_value = portfolio.position_value + notional
            projected_equity = portfolio.equity

            if projected_equity <= 0:
                return False, "invalid_projected_equity"

            projected_exposure = projected_position_value / projected_equity

            if projected_exposure > self.limits.max_position_percent:
                return False, "projected_position_exceeds_limit"

            return True, "approved"

        if side == "sell":
            if not decision.allow_sell:
                return False, "sell_orders_are_disabled"

            if portfolio.position_value <= 0:
                return False, "no_position_to_sell"

            return True, "approved"

        return False, "unknown_order_side"

    def _position_exposure(self, portfolio: PortfolioManager) -> Decimal:
        equity = portfolio.equity

        if equity <= 0:
            return Decimal("1")

        return portfolio.position_value / equity