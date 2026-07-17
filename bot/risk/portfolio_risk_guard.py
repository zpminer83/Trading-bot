from dataclasses import dataclass
from decimal import Decimal
from hashlib import sha256
import json

from bot.portfolio.portfolio_manager import PortfolioManager
from bot.risk.risk_manager import RiskLimits, RiskManager, RiskMode


@dataclass(frozen=True)
class PortfolioRiskLimits:
    max_drawdown: Decimal = Decimal("0.10")
    preemptive_drawdown: Decimal = Decimal("0.08")

    def __post_init__(self) -> None:
        max_drawdown = Decimal(str(self.max_drawdown))

        if (
            not max_drawdown.is_finite()
            or max_drawdown < 0
            or max_drawdown > 1
        ):
            raise ValueError("max_drawdown must be between 0 and 1 inclusive")

        preemptive_drawdown = Decimal(str(self.preemptive_drawdown))
        if (
            not preemptive_drawdown.is_finite()
            or preemptive_drawdown < 0
            or preemptive_drawdown >= max_drawdown
        ):
            raise ValueError("preemptive_drawdown must be between 0 and max_drawdown")

        object.__setattr__(self, "max_drawdown", max_drawdown)
        object.__setattr__(self, "preemptive_drawdown", preemptive_drawdown)


@dataclass(frozen=True)
class PortfolioRiskDecision:
    allowed: bool
    reason: str
    latched: bool
    drawdown: Decimal
    max_drawdown: Decimal
    equity: Decimal
    peak_equity: Decimal
    preemptive_drawdown: Decimal = Decimal("0.08")
    entry_halt_latched: bool = False
    emergency_exit_requested: bool = False
    emergency_exit_completed: bool = False
    risk_state_fingerprint: str = ""
    blockers: tuple[str, ...] = ()


class PortfolioRiskGuard:
    """Single authoritative, fail-closed portfolio drawdown controller."""

    def __init__(
        self,
        limits: PortfolioRiskLimits | None = None,
        risk_manager: RiskManager | None = None,
    ):
        self.limits = limits or PortfolioRiskLimits()
        self._latched = False
        self._entry_halt_latched = False
        self._emergency_exit_requested = False
        self._emergency_exit_completed = False

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
        drawdown = portfolio.drawdown

        if stop_triggered:
            self._latched = True
            self._entry_halt_latched = True
            self._emergency_exit_requested = portfolio.base_position > 0
        elif drawdown >= self.limits.preemptive_drawdown:
            self._entry_halt_latched = True
            self._emergency_exit_requested = portfolio.base_position > 0

        if stop_triggered:
            reason = "max_drawdown_reached"
        elif self._latched:
            reason = "max_drawdown_latched"
        elif self._entry_halt_latched:
            reason = "preemptive_drawdown_halt"
        else:
            reason = "ok"

        blockers: tuple[str, ...] = ()
        if self._entry_halt_latched:
            blockers = ("portfolio_entry_halt_latched",)
        if self._latched:
            blockers = (*blockers, "portfolio_kill_switch_latched")
        fingerprint = sha256(json.dumps({
            "drawdown": str(drawdown), "peak_equity": str(portfolio.peak_equity),
            "equity": str(portfolio.equity), "preemptive": str(self.limits.preemptive_drawdown),
            "hard": str(self.limits.max_drawdown), "entry_halt": self._entry_halt_latched,
            "kill_switch": self._latched, "emergency_requested": self._emergency_exit_requested,
            "emergency_completed": self._emergency_exit_completed,
            "blockers": blockers,
        }, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

        return PortfolioRiskDecision(
            allowed=not self._entry_halt_latched,
            reason=reason,
            latched=self._latched,
            drawdown=drawdown,
            max_drawdown=self.limits.max_drawdown,
            equity=portfolio.equity,
            peak_equity=portfolio.peak_equity,
            preemptive_drawdown=self.limits.preemptive_drawdown,
            entry_halt_latched=self._entry_halt_latched,
            emergency_exit_requested=self._emergency_exit_requested,
            emergency_exit_completed=self._emergency_exit_completed,
            risk_state_fingerprint=fingerprint,
            blockers=blockers,
        )

    def mark_emergency_exit_completed(self, completed: bool = True) -> None:
        if completed:
            self._emergency_exit_completed = True
            self._emergency_exit_requested = False

    def projected_order_allowed(
        self,
        portfolio: PortfolioManager,
        *,
        side: str,
        notional: Decimal,
        reserved_order_exposure: Decimal = Decimal("0"),
        adverse_move_ratio: Decimal = Decimal("0.02"),
        slippage_ratio: Decimal = Decimal("0.01"),
        fee_ratio: Decimal = Decimal("0.001"),
    ) -> tuple[bool, str]:
        """Bound a candidate against remaining hard-drawdown headroom.

        Assumptions are explicit arguments; callers cannot silently replace
        missing volatility/slippage data with zero.
        """
        try:
            notional = Decimal(str(notional))
            reserved_order_exposure = Decimal(str(reserved_order_exposure))
            adverse_move_ratio = Decimal(str(adverse_move_ratio))
            slippage_ratio = Decimal(str(slippage_ratio))
            fee_ratio = Decimal(str(fee_ratio))
        except (ArithmeticError, ValueError, TypeError):
            return False, "risk_budget_assumptions_required"
        values = (notional, reserved_order_exposure, adverse_move_ratio, slippage_ratio, fee_ratio)
        if any(not value.is_finite() or value < 0 for value in values) or notional <= 0:
            return False, "risk_budget_inputs_invalid"
        if adverse_move_ratio == 0 or slippage_ratio == 0 or fee_ratio == 0:
            return False, "risk_budget_assumptions_required"
        if self._entry_halt_latched:
            return False, "portfolio_entry_halt_latched"
        current_equity = portfolio.equity
        if current_equity <= 0 or portfolio.peak_equity <= 0:
            return False, "invalid_risk_budget_equity"
        drawdown_headroom = self.limits.max_drawdown * portfolio.peak_equity - (
            portfolio.peak_equity - current_equity
        )
        projected_exposure = abs(portfolio.position_value) + reserved_order_exposure + notional
        projected_loss = projected_exposure * (adverse_move_ratio + slippage_ratio) + notional * fee_ratio
        if projected_loss >= drawdown_headroom:
            return False, "projected_risk_exceeds_drawdown_headroom"
        return True, "risk_budget_approved"

    def reset(self) -> None:
        self._latched = False
        self._entry_halt_latched = False
        self._emergency_exit_requested = False
        self._emergency_exit_completed = False

    @property
    def latched(self) -> bool:
        return self._latched

    @property
    def entry_halt_latched(self) -> bool:
        return self._entry_halt_latched

    @property
    def emergency_exit_requested(self) -> bool:
        return self._emergency_exit_requested

    @property
    def emergency_exit_completed(self) -> bool:
        return self._emergency_exit_completed
