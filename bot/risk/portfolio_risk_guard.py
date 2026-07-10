from dataclasses import dataclass
from decimal import Decimal

from bot.portfolio.portfolio_manager import PortfolioManager
from bot.risk.risk_manager import RiskLimits, RiskManager, RiskMode


@dataclass(frozen=True)
class PortfolioRiskLimits:
    max_drawdown: Decimal = Decimal("0.10")

    def __post_init__(self) -> None:
        max_drawdown = Decimal(str(self.max_drawdown))

        if (
            not max_drawdown.is_finite()
            or max_drawdown < 0
            or max_drawdown > 1
        ):
            raise ValueError("max_drawdown must be between 0 and 1 inclusive")

        object.__setattr__(self, "max_drawdown", max_drawdown)


@dataclass(frozen=True)
class PortfolioRiskDecision:
    allowed: bool
    reason: str
    latched: bool
    drawdown: Decimal
    max_drawdown: Decimal
    equity: Decimal
    peak_equity: Decimal


class PortfolioRiskGuard:
    """Latched engine-level stop based on RiskManager's STOP mode."""

    def __init__(
        self,
        limits: PortfolioRiskLimits | None = None,
        risk_manager: RiskManager | None = None,
    ):
        self.limits = limits or PortfolioRiskLimits()
        self._latched = False

        if risk_manager is not None:
            if risk_manager.limits.max_drawdown != self.limits.max_drawdown:
                raise ValueError(
                    "risk_manager max_drawdown must match portfolio risk limit"
                )

            self._risk_manager = risk_manager
        else:
            self._risk_manager = RiskManager(
                limits=RiskLimits(max_drawdown=self.limits.max_drawdown)
            )

    def evaluate(self, portfolio: PortfolioManager) -> PortfolioRiskDecision:
        risk_decision = self._risk_manager.evaluate(portfolio)
        stop_triggered = risk_decision.mode == RiskMode.STOP

        if stop_triggered:
            self._latched = True

        if stop_triggered:
            reason = "max_drawdown_reached"
        elif self._latched:
            reason = "max_drawdown_latched"
        else:
            reason = "ok"

        return PortfolioRiskDecision(
            allowed=not self._latched,
            reason=reason,
            latched=self._latched,
            drawdown=portfolio.drawdown,
            max_drawdown=self.limits.max_drawdown,
            equity=portfolio.equity,
            peak_equity=portfolio.peak_equity,
        )

    def reset(self) -> None:
        self._latched = False

    @property
    def latched(self) -> bool:
        return self._latched
