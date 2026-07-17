from dataclasses import dataclass
from decimal import Decimal
from hashlib import sha256
import json

from bot.portfolio.portfolio_manager import PortfolioManager
from bot.risk.risk_manager import RiskLimits, RiskManager, RiskMode


def _decimal(value: object, field: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (ArithmeticError, ValueError, TypeError) as exc:
        raise ValueError(f"{field} must be a finite Decimal") from exc
    if not parsed.is_finite():
        raise ValueError(f"{field} must be a finite Decimal")
    return parsed


def _tuple(values: tuple[str, ...] | list[str] | None) -> tuple[str, ...]:
    return tuple(dict.fromkeys(str(value) for value in (values or ()) if str(value)))


@dataclass(frozen=True)
class PortfolioRiskLimits:
    """Hard portfolio and gap-risk policy.

    The defaults are an explicit conservative paper profile selected from the
    FAST_SELL_OFF audit: the observed 10% one-step drop receives a 20% stress
    uplift, a 1% reserve is kept untouched, and open passive exposure is capped
    at 15% of peak equity.  No assumption is silently replaced with zero.
    """

    max_drawdown: Decimal = Decimal("0.10")
    preemptive_drawdown: Decimal = Decimal("0.08")
    adverse_move_fraction_long: Decimal | None = Decimal("0.12")
    adverse_move_fraction_short: Decimal | None = Decimal("0.12")
    emergency_exit_slippage_fraction: Decimal | None = Decimal("0.02")
    emergency_exit_fee_fraction: Decimal | None = Decimal("0.002")
    minimum_drawdown_reserve_fraction: Decimal | None = Decimal("0.01")
    maximum_gap_risk_position_fraction: Decimal | None = Decimal("0.15")
    include_reserved_orders_in_gap_exposure: bool = True
    require_gap_risk_assumptions: bool = True
    authoritative: bool = True
    unresolved_reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        max_drawdown = _decimal(self.max_drawdown, "max_drawdown")
        if max_drawdown < 0 or max_drawdown > 1:
            raise ValueError("max_drawdown must be between 0 and 1 inclusive")
        preemptive_drawdown = _decimal(self.preemptive_drawdown, "preemptive_drawdown")
        if preemptive_drawdown < 0 or preemptive_drawdown >= max_drawdown:
            raise ValueError("preemptive_drawdown must be between 0 and max_drawdown")

        positive_fields = (
            "adverse_move_fraction_long",
            "adverse_move_fraction_short",
            "emergency_exit_slippage_fraction",
            "emergency_exit_fee_fraction",
            "minimum_drawdown_reserve_fraction",
            "maximum_gap_risk_position_fraction",
        )
        for name in positive_fields:
            value = getattr(self, name)
            # Optional assumptions are deliberately allowed to remain
            # unresolved.  The budget calculator records the missing/invalid
            # assumption and fail-closes risk-increasing orders; it never
            # substitutes an unsafe zero.
            if value is not None:
                try:
                    object.__setattr__(self, name, _decimal(value, name))
                except ValueError:
                    object.__setattr__(self, name, None)

        if not isinstance(self.include_reserved_orders_in_gap_exposure, bool):
            raise ValueError("include_reserved_orders_in_gap_exposure must be bool")
        if not isinstance(self.require_gap_risk_assumptions, bool):
            raise ValueError("require_gap_risk_assumptions must be bool")
        if not isinstance(self.authoritative, bool):
            raise ValueError("authoritative must be bool")
        if not isinstance(self.unresolved_reasons, (tuple, list)):
            raise ValueError("unresolved_reasons must be a sequence")
        object.__setattr__(self, "max_drawdown", max_drawdown)
        object.__setattr__(self, "preemptive_drawdown", preemptive_drawdown)
        object.__setattr__(self, "unresolved_reasons", _tuple(self.unresolved_reasons))


@dataclass(frozen=True)
class GapRiskBudget:
    peak_equity: Decimal
    current_equity: Decimal
    current_drawdown_value: Decimal
    remaining_hard_headroom: Decimal
    minimum_reserved_headroom: Decimal
    adverse_move_long: Decimal | None
    adverse_move_short: Decimal | None
    exit_slippage_buffer: Decimal
    fee_buffer: Decimal
    current_marked_exposure: Decimal
    reserved_risk_increasing_exposure: Decimal
    maximum_gap_safe_position_notional: Decimal
    proposed_post_fill_position_notional: Decimal | None
    projected_shocked_equity: Decimal | None
    projected_shocked_drawdown: Decimal | None
    projected_hard_limit_overshoot: Decimal | None
    gap_risk_budget_approved: bool
    blockers: tuple[str, ...] = ()
    maximum_gap_safe_long_notional: Decimal = Decimal("0")
    maximum_gap_safe_short_notional: Decimal = Decimal("0")

    @property
    def approved(self) -> bool:
        return self.gap_risk_budget_approved


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
    gap_risk_budget: GapRiskBudget | None = None


class PortfolioRiskGuard:
    """Single authoritative, latched portfolio drawdown controller."""

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
        self._last_gap_budget: GapRiskBudget | None = None

        if risk_manager is not None:
            if risk_manager.limits.max_drawdown != self.limits.max_drawdown:
                raise ValueError("risk_manager max_drawdown must match portfolio risk limit")
            self._risk_manager = risk_manager
        else:
            self._risk_manager = RiskManager(
                limits=RiskLimits(max_drawdown=self.limits.max_drawdown)
            )

    @staticmethod
    def is_risk_increasing(portfolio: PortfolioManager, side: str) -> bool:
        side = str(side).lower()
        if side == "buy":
            return portfolio.base_position >= 0
        if side == "sell":
            return portfolio.base_position <= 0
        return True

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

        gap_budget = self.calculate_gap_risk_budget(portfolio)
        blockers: tuple[str, ...] = ()
        if self._entry_halt_latched:
            blockers = ("portfolio_entry_halt_latched",)
        if self._latched:
            blockers = (*blockers, "portfolio_kill_switch_latched")
        fingerprint = sha256(json.dumps({
            "drawdown": str(drawdown),
            "peak_equity": str(portfolio.peak_equity),
            "equity": str(portfolio.equity),
            "preemptive": str(self.limits.preemptive_drawdown),
            "hard": str(self.limits.max_drawdown),
            "entry_halt": self._entry_halt_latched,
            "kill_switch": self._latched,
            "emergency_requested": self._emergency_exit_requested,
            "emergency_completed": self._emergency_exit_completed,
            "gap_budget": gap_budget.__dict__,
            "blockers": blockers,
        }, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()

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
            gap_risk_budget=gap_budget,
        )

    def calculate_gap_risk_budget(
        self,
        portfolio: PortfolioManager,
        *,
        side: str | None = None,
        price: Decimal | None = None,
        quantity: Decimal | None = None,
        notional: Decimal | None = None,
        reserved_order_exposure: Decimal = Decimal("0"),
        adverse_move_long: Decimal | None = None,
        adverse_move_short: Decimal | None = None,
        exit_slippage_fraction: Decimal | None = None,
        exit_fee_fraction: Decimal | None = None,
    ) -> GapRiskBudget:
        """Calculate a conservative post-shock budget without look-ahead."""
        peak = portfolio.peak_equity
        equity = portfolio.equity
        current_drawdown_value = max(peak - equity, Decimal("0"))
        remaining = max(self.limits.max_drawdown * peak - current_drawdown_value, Decimal("0"))
        reserve = Decimal("0")
        current_exposure = abs(portfolio.position_value)
        try:
            reserved = _decimal(reserved_order_exposure, "reserved_order_exposure")
        except ValueError:
            reserved = Decimal("0")
        if not self.limits.include_reserved_orders_in_gap_exposure:
            reserved = Decimal("0")

        proposed_notional: Decimal | None = None
        proposed_position_notional: Decimal | None = None
        projected_equity: Decimal | None = None
        projected_drawdown: Decimal | None = None
        overshoot: Decimal | None = None
        blockers: list[str] = []
        proposed_position = portfolio.base_position
        proposed_price = portfolio.last_price
        proposal_risk_increasing = False
        if side is not None:
            side = str(side).lower()
            try:
                if quantity is None or price is None:
                    raise ValueError
                qty = _decimal(quantity, "quantity")
                proposed_price = _decimal(price, "price")
                proposed_notional = (
                    _decimal(notional, "notional")
                    if notional is not None
                    else proposed_price * qty
                )
                if qty <= 0 or proposed_price <= 0 or proposed_notional <= 0:
                    raise ValueError
                proposed_position = (
                    portfolio.base_position + qty
                    if side == "buy"
                    else portfolio.base_position - qty
                    if side == "sell"
                    else portfolio.base_position
                )
                proposal_risk_increasing = self.is_risk_increasing(portfolio, side)
                proposed_position_notional = abs(proposed_position * proposed_price)
            except (ValueError, ArithmeticError, TypeError):
                blockers.append("gap_risk_order_inputs_invalid")

        risk_exposure = current_exposure + reserved
        if proposed_notional is not None and proposal_risk_increasing:
            risk_exposure += proposed_notional
        def assumption(value: object, name: str) -> Decimal | None:
            try:
                parsed = _decimal(value, name)
            except ValueError:
                blockers.append(f"{name}_unavailable")
                return None
            if parsed <= 0 or parsed > 1:
                blockers.append(f"{name}_invalid")
                return None
            return parsed

        stress_long = assumption(
            self.limits.adverse_move_fraction_long
            if adverse_move_long is None
            else adverse_move_long,
            "adverse_move_fraction_long",
        )
        stress_short = assumption(
            self.limits.adverse_move_fraction_short
            if adverse_move_short is None
            else adverse_move_short,
            "adverse_move_fraction_short",
        )
        exit_slippage_rate = assumption(
            self.limits.emergency_exit_slippage_fraction
            if exit_slippage_fraction is None
            else exit_slippage_fraction,
            "emergency_exit_slippage_fraction",
        )
        exit_fee_rate = assumption(
            self.limits.emergency_exit_fee_fraction
            if exit_fee_fraction is None
            else exit_fee_fraction,
            "emergency_exit_fee_fraction",
        )
        reserve_rate = assumption(
            self.limits.minimum_drawdown_reserve_fraction,
            "minimum_drawdown_reserve_fraction",
        )
        position_cap_rate = assumption(
            self.limits.maximum_gap_risk_position_fraction,
            "maximum_gap_risk_position_fraction",
        )
        if self.limits.require_gap_risk_assumptions and self.limits.unresolved_reasons:
            blockers.extend(self.limits.unresolved_reasons)
        reserve = peak * (reserve_rate or Decimal("0"))
        exit_fee_rate = exit_fee_rate or Decimal("0")
        exit_slippage_rate = exit_slippage_rate or Decimal("0")
        stress_fraction = (
            stress_long
            if proposed_position >= 0
            else stress_short
        ) or Decimal("0")
        fee_buffer = risk_exposure * exit_fee_rate
        if proposed_notional is not None:
            fee_buffer += proposed_notional * exit_fee_rate
        exit_slippage_buffer = risk_exposure * exit_slippage_rate
        usable = max(remaining - reserve - fee_buffer - exit_slippage_buffer, Decimal("0"))
        maximum_safe_long = min(
            usable / stress_long,
            peak * (position_cap_rate or Decimal("0")),
        ) if stress_long is not None and stress_long > 0 and position_cap_rate is not None and peak > 0 else Decimal("0")
        maximum_safe_short = min(
            usable / stress_short,
            peak * (position_cap_rate or Decimal("0")),
        ) if stress_short is not None and stress_short > 0 and position_cap_rate is not None and peak > 0 else Decimal("0")
        maximum_safe = maximum_safe_long if proposed_position >= 0 else maximum_safe_short

        if proposed_position_notional is not None and proposed_position_notional > maximum_safe:
            blockers.append("gap_position_capacity_exceeded")

        if self.limits.require_gap_risk_assumptions:
            if not self.limits.authoritative:
                blockers.append("gap_risk_assumptions_not_authoritative")
        if self._entry_halt_latched:
            blockers.append("portfolio_entry_halt_latched")
        if self._latched:
            blockers.append("portfolio_kill_switch_latched")
        if self._emergency_exit_requested and not self._emergency_exit_completed:
            blockers.append("emergency_exit_unresolved")

        if side is None:
            shocked_price = portfolio.last_price * (Decimal("1") - stress_fraction)
        elif proposed_position >= 0:
            shocked_price = proposed_price * (Decimal("1") - stress_fraction)
        else:
            shocked_price = proposed_price * (Decimal("1") + stress_fraction)
        if proposed_notional is not None and proposed_position >= 0:
            projected_cash = portfolio.cash_balance - proposed_notional
        elif proposed_notional is not None and proposed_position < 0:
            projected_cash = portfolio.cash_balance + proposed_notional
        else:
            projected_cash = portfolio.cash_balance
        projected_equity = projected_cash + proposed_position * shocked_price - fee_buffer - exit_slippage_buffer
        projected_drawdown = max(peak - projected_equity, Decimal("0")) / peak if peak > 0 else Decimal("1")
        overshoot = max(projected_drawdown - self.limits.max_drawdown, Decimal("0"))
        if projected_drawdown >= self.limits.max_drawdown:
            blockers.append("projected_shocked_drawdown_at_or_above_hard_limit")
        if usable <= 0 and (proposal_risk_increasing or side is None):
            blockers.append("gap_headroom_exhausted")
        if not self.limits.require_gap_risk_assumptions and side is not None:
            blockers = ["gap_risk_policy_disabled"]
        approved = not blockers or (
            not self.limits.require_gap_risk_assumptions and side is not None
        )
        budget = GapRiskBudget(
            peak_equity=peak,
            current_equity=equity,
            current_drawdown_value=current_drawdown_value,
            remaining_hard_headroom=remaining,
            minimum_reserved_headroom=reserve,
            adverse_move_long=stress_long,
            adverse_move_short=stress_short,
            exit_slippage_buffer=exit_slippage_buffer,
            fee_buffer=fee_buffer,
            current_marked_exposure=current_exposure,
            reserved_risk_increasing_exposure=reserved,
            maximum_gap_safe_position_notional=maximum_safe,
            proposed_post_fill_position_notional=proposed_position_notional,
            projected_shocked_equity=projected_equity,
            projected_shocked_drawdown=projected_drawdown,
            projected_hard_limit_overshoot=overshoot,
            gap_risk_budget_approved=approved,
            blockers=_tuple(blockers),
            maximum_gap_safe_long_notional=maximum_safe_long,
            maximum_gap_safe_short_notional=maximum_safe_short,
        )
        self._last_gap_budget = budget
        return budget

    def projected_order_allowed(
        self,
        portfolio: PortfolioManager,
        *,
        side: str,
        notional: Decimal,
        reserved_order_exposure: Decimal = Decimal("0"),
        adverse_move_ratio: Decimal | None = None,
        slippage_ratio: Decimal | None = None,
        fee_ratio: Decimal | None = None,
        price: Decimal | None = None,
        quantity: Decimal | None = None,
    ) -> tuple[bool, str]:
        """Compatibility wrapper returning the historical tuple result."""
        if not self.limits.require_gap_risk_assumptions:
            budget = GapRiskBudget(
                peak_equity=portfolio.peak_equity,
                current_equity=portfolio.equity,
                current_drawdown_value=max(portfolio.peak_equity - portfolio.equity, Decimal("0")),
                remaining_hard_headroom=max(self.limits.max_drawdown * portfolio.peak_equity - max(portfolio.peak_equity - portfolio.equity, Decimal("0")), Decimal("0")),
                minimum_reserved_headroom=portfolio.peak_equity * self.limits.minimum_drawdown_reserve_fraction,
                adverse_move_long=self.limits.adverse_move_fraction_long,
                adverse_move_short=self.limits.adverse_move_fraction_short,
                exit_slippage_buffer=Decimal("0"),
                fee_buffer=Decimal("0"),
                current_marked_exposure=abs(portfolio.position_value),
                reserved_risk_increasing_exposure=Decimal("0"),
                maximum_gap_safe_position_notional=portfolio.peak_equity * self.limits.maximum_gap_risk_position_fraction,
                proposed_post_fill_position_notional=None,
                projected_shocked_equity=None,
                projected_shocked_drawdown=None,
                projected_hard_limit_overshoot=None,
                gap_risk_budget_approved=True,
                blockers=("gap_risk_policy_disabled",),
            )
            self._last_gap_budget = budget
            return True, "gap_risk_policy_disabled"
        if adverse_move_ratio is None or slippage_ratio is None or fee_ratio is None:
            return False, "risk_budget_assumptions_required"
        if any(_decimal(value, name) <= 0 for value, name in ((adverse_move_ratio, "adverse_move_ratio"), (slippage_ratio, "slippage_ratio"), (fee_ratio, "fee_ratio"))):
            return False, "risk_budget_assumptions_required"
        budget = self.calculate_gap_risk_budget(
            portfolio,
            side=side,
            notional=notional,
            price=price or portfolio.last_price,
            quantity=quantity or (Decimal(str(notional)) / (price or portfolio.last_price)),
            reserved_order_exposure=reserved_order_exposure,
            adverse_move_long=adverse_move_ratio,
            adverse_move_short=adverse_move_ratio,
            exit_slippage_fraction=slippage_ratio,
            exit_fee_fraction=fee_ratio,
        )
        return budget.approved, ("risk_budget_approved" if budget.approved else budget.blockers[0])

    def mark_emergency_exit_completed(self, completed: bool = True) -> None:
        if completed:
            self._emergency_exit_completed = True
            self._emergency_exit_requested = False

    def reset(self) -> None:
        self._latched = False
        self._entry_halt_latched = False
        self._emergency_exit_requested = False
        self._emergency_exit_completed = False
        self._last_gap_budget = None

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

    @property
    def last_gap_budget(self) -> GapRiskBudget | None:
        return self._last_gap_budget


__all__ = ["GapRiskBudget", "PortfolioRiskDecision", "PortfolioRiskGuard", "PortfolioRiskLimits"]
