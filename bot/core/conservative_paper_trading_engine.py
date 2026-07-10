from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from bot.competition.competition_tracker import (
    CompetitionSnapshot,
    CompetitionTracker,
)
from bot.competition.confirmed_fill_ledger import (
    ConfirmedFillEvent,
    ConfirmedFillLedger,
    ConfirmedFillLedgerLimits,
)
from bot.competition.fair_play_guard import (
    FairPlayDecision,
    FairPlayGuard,
)
from bot.execution.conservative_paper_broker import (
    ConservativePaperBroker,
    PaperOrder,
)
from bot.execution.execution_manager import ExecutionManager
from bot.execution.order import OrderDecision, OrderIntent
from bot.execution.order_manager import OrderManager
from bot.execution.paper_broker import PaperFill
from bot.market.market_cache import MarketCache
from bot.portfolio.portfolio_manager import PortfolioManager
from bot.risk.portfolio_risk_guard import (
    PortfolioRiskDecision,
    PortfolioRiskGuard,
    PortfolioRiskLimits,
)
from bot.risk.market_freshness import (
    MarketFreshnessDecision,
    MarketFreshnessGuard,
)
from bot.risk.market_safety import (
    MarketSafety,
    MarketSafetyDecision,
)
from bot.strategy.passive_market_maker import (
    PassiveMarketMakerStrategy,
)


@dataclass(frozen=True)
class ConservativePaperTradingStepResult:
    mid_price: Decimal | None
    intents: list[OrderIntent] = field(default_factory=list)
    decisions: list[OrderDecision] = field(default_factory=list)
    fills: list[PaperFill] = field(default_factory=list)
    submitted_orders: list[PaperOrder] = field(default_factory=list)

    competition_snapshot: CompetitionSnapshot | None = None
    market_safety_decision: MarketSafetyDecision | None = None
    market_freshness_decision: MarketFreshnessDecision | None = None
    portfolio_risk_decision: PortfolioRiskDecision | None = None
    confirmed_fill_events: list[ConfirmedFillEvent] = field(default_factory=list)
    fair_play_decisions: list[FairPlayDecision] = field(default_factory=list)
    fair_play_allowed: bool | None = None
    fair_play_reason: str | None = None
    fair_play_latched: bool | None = None
    fair_play_blocked_intents_count: int = 0
    short_window_round_trip_count: int = 0
    near_flat_cycle_count: int = 0


class ConservativePaperTradingEngine:
    """
    Runs one conservative paper-trading cycle.

    Processing order:

    1. check market-data freshness
    2. check market safety
    3. update portfolio mark price
    4. evaluate the latched portfolio risk guard
    5. capture the position before paper fills
    6. process existing paper orders
    7. record confirmed-fill competition volume
    8. audit confirmed fills in the ledger
    9. update the fair-play guard
    10. generate strategy intents
    11. review fair-play-approved intents through execution and risk checks
    12. replace stale orders with approved new orders

    If freshness or safety checks fail:
    - existing open orders are cancelled
    - fills are not simulated against unsafe data
    - no new intents or orders are created
    """

    def __init__(
        self,
        symbol: str,
        market: MarketCache,
        portfolio: PortfolioManager,
        strategy: PassiveMarketMakerStrategy,
        execution: ExecutionManager,
        broker: ConservativePaperBroker,
        order_manager: OrderManager,
        competition: CompetitionTracker | None = None,
        market_safety: MarketSafety | None = None,
        market_freshness: MarketFreshnessGuard | None = None,
        portfolio_risk_guard: PortfolioRiskGuard | None = None,
        confirmed_fill_ledger: ConfirmedFillLedger | None = None,
        fair_play_guard: FairPlayGuard | None = None,
    ):
        self.symbol = symbol
        self.market = market
        self.portfolio = portfolio
        self.strategy = strategy
        self.execution = execution
        self.broker = broker
        self.order_manager = order_manager
        self.competition = competition
        self.market_safety = market_safety
        self.market_freshness = market_freshness
        self.fair_play_guard = fair_play_guard
        self.confirmed_fill_ledger = confirmed_fill_ledger
        if self.confirmed_fill_ledger is None and fair_play_guard is not None:
            self.confirmed_fill_ledger = ConfirmedFillLedger(
                limits=ConfirmedFillLedgerLimits(
                    short_window_seconds=fair_play_guard.limits.short_window_seconds,
                    quantity_tolerance_ratio=(
                        fair_play_guard.limits.quantity_tolerance_ratio
                    ),
                    near_flat_ratio=fair_play_guard.limits.near_flat_ratio,
                    minimum_meaningful_exposure_notional=(
                        fair_play_guard.limits.minimum_meaningful_exposure_notional
                    ),
                )
            )
        self.portfolio_risk_guard = portfolio_risk_guard or PortfolioRiskGuard(
            limits=PortfolioRiskLimits(
                max_drawdown=execution.risk_manager.limits.max_drawdown
            ),
            risk_manager=execution.risk_manager,
        )

    def step(
        self,
        timestamp: datetime | None = None,
    ) -> ConservativePaperTradingStepResult:
        mid_price = self.market.mid_price(self.symbol)

        freshness_decision = self._evaluate_market_freshness(
            observed_at=timestamp,
        )

        if freshness_decision is not None and not freshness_decision.fresh:
            self.order_manager.cancel_all()

            return ConservativePaperTradingStepResult(
                mid_price=mid_price,
                competition_snapshot=self._competition_snapshot(),
                market_freshness_decision=freshness_decision,
            )

        safety_decision = self._evaluate_market_safety()

        if safety_decision is not None and not safety_decision.safe:
            self.order_manager.cancel_all()

            return ConservativePaperTradingStepResult(
                mid_price=mid_price,
                competition_snapshot=self._competition_snapshot(),
                market_safety_decision=safety_decision,
                market_freshness_decision=freshness_decision,
            )

        if mid_price is not None:
            self.portfolio.update_market_price(mid_price)

        portfolio_risk_decision = self.portfolio_risk_guard.evaluate(
            self.portfolio
        )

        if not portfolio_risk_decision.allowed:
            self.order_manager.cancel_all()

            return ConservativePaperTradingStepResult(
                mid_price=mid_price,
                competition_snapshot=self._competition_snapshot(),
                market_safety_decision=safety_decision,
                market_freshness_decision=freshness_decision,
                portfolio_risk_decision=portfolio_risk_decision,
            )

        position_before_fills = self.portfolio.base_position
        fills = self.broker.process_market(self.market)

        if self.competition is not None:
            for fill in fills:
                self.competition.record_trade(
                    symbol=fill.symbol,
                    notional=fill.notional,
                    timestamp=timestamp,
                )

        confirmed_fill_events: list[ConfirmedFillEvent] = []
        if self.confirmed_fill_ledger is not None:
            confirmed_fill_events = self.confirmed_fill_ledger.record_fills(
                fills=fills,
                starting_position=position_before_fills,
                timestamp=timestamp,
            )

        fair_play_status: FairPlayDecision | None = None
        if self.fair_play_guard is not None:
            fair_play_status = self.fair_play_guard.consume(confirmed_fill_events)
            if fair_play_status.latched:
                self.order_manager.cancel_all()
                return ConservativePaperTradingStepResult(
                    mid_price=mid_price,
                    fills=fills,
                    competition_snapshot=self._competition_snapshot(),
                    market_safety_decision=safety_decision,
                    market_freshness_decision=freshness_decision,
                    portfolio_risk_decision=portfolio_risk_decision,
                    confirmed_fill_events=confirmed_fill_events,
                    fair_play_allowed=False,
                    fair_play_reason=fair_play_status.reason,
                    fair_play_latched=True,
                    short_window_round_trip_count=(
                        fair_play_status.short_window_round_trip_count
                    ),
                    near_flat_cycle_count=fair_play_status.near_flat_cycle_count,
                )

        intents = self.strategy.generate_orders(
            market=self.market,
            portfolio=self.portfolio,
        )

        approved_intents = intents
        fair_play_decisions: list[FairPlayDecision] = []
        fair_play_blocked_intents_count = 0
        if self.fair_play_guard is not None:
            approved_intents = []
            for intent in intents:
                fair_play_decision = self.fair_play_guard.review_intent(
                    intent,
                    timestamp=timestamp,
                    current_position=self.portfolio.base_position,
                )
                fair_play_decisions.append(fair_play_decision)
                if fair_play_decision.allowed:
                    approved_intents.append(intent)
                else:
                    fair_play_blocked_intents_count += 1

            fair_play_status = self.fair_play_guard.status()

        decisions = [self.execution.review_order(intent) for intent in approved_intents]

        submitted_orders = self.order_manager.replace_orders(
            decisions,
        )

        return ConservativePaperTradingStepResult(
            mid_price=mid_price,
            intents=intents,
            decisions=decisions,
            fills=fills,
            submitted_orders=submitted_orders,
            competition_snapshot=self._competition_snapshot(),
            market_safety_decision=safety_decision,
            market_freshness_decision=freshness_decision,
            portfolio_risk_decision=portfolio_risk_decision,
            confirmed_fill_events=confirmed_fill_events,
            fair_play_decisions=fair_play_decisions,
            fair_play_allowed=(
                None
                if fair_play_status is None
                else not fair_play_status.latched
                and fair_play_blocked_intents_count == 0
            ),
            fair_play_reason=(
                None
                if fair_play_status is None
                else (
                    next(
                        (
                            decision.reason
                            for decision in fair_play_decisions
                            if not decision.allowed
                        ),
                        fair_play_status.reason,
                    )
                )
            ),
            fair_play_latched=(
                None if fair_play_status is None else fair_play_status.latched
            ),
            fair_play_blocked_intents_count=fair_play_blocked_intents_count,
            short_window_round_trip_count=(
                0
                if fair_play_status is None
                else fair_play_status.short_window_round_trip_count
            ),
            near_flat_cycle_count=(
                0 if fair_play_status is None else fair_play_status.near_flat_cycle_count
            ),
        )

    def _evaluate_market_freshness(
        self,
        observed_at: datetime | None,
    ) -> MarketFreshnessDecision | None:
        if self.market_freshness is None:
            return None

        return self.market_freshness.evaluate(
            market=self.market,
            symbol=self.symbol,
            observed_at=observed_at,
        )

    def _evaluate_market_safety(
        self,
    ) -> MarketSafetyDecision | None:
        if self.market_safety is None:
            return None

        return self.market_safety.evaluate(
            market=self.market,
            symbol=self.symbol,
        )

    def _competition_snapshot(
        self,
    ) -> CompetitionSnapshot | None:
        if self.competition is None:
            return None

        return self.competition.snapshot()
