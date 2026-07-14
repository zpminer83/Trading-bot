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
from bot.competition.trade_intent_ledger import (
    TradeIntentEvent,
    TradeIntentLedger,
)
from bot.execution.conservative_paper_broker import (
    ConservativePaperBroker,
    PaperOrder,
)
from bot.execution.execution_manager import ExecutionManager
from bot.execution.order import OrderDecision, OrderIntent, OrderPurpose
from bot.execution.order_manager import OrderManager
from bot.execution.paper_broker import PaperFill
from bot.market.market_cache import MarketCache
from bot.market.orderbook_signal import (
    OrderBookSignalEngine,
    OrderBookSignalSnapshot,
)
from bot.market.orderbook_depth_diagnostics import (
    OrderBookDepthDiagnostics,
    calculate_orderbook_depth_diagnostics,
)
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
    risk_exit_enabled: bool | None = None
    risk_exit_intents_count: int = 0
    risk_exit_fills_count: int = 0
    risk_exit_reason: str | None = None
    trade_intent_events: list[TradeIntentEvent] = field(default_factory=list)
    purpose_counts: dict[str, int] = field(default_factory=dict)
    orderbook_signal: OrderBookSignalSnapshot | None = None
    orderbook_depth_diagnostics: OrderBookDepthDiagnostics | None = None


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
        trade_intent_ledger: TradeIntentLedger | None = None,
        orderbook_signal_engine: OrderBookSignalEngine | None = None,
        paper_risk_exit_enabled: bool = False,
        risk_exit_enabled: bool | None = None,
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
        self.trade_intent_ledger = trade_intent_ledger
        self.orderbook_signal_engine = orderbook_signal_engine
        self.paper_risk_exit_enabled = bool(
            paper_risk_exit_enabled
            if risk_exit_enabled is None
            else risk_exit_enabled
        )
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
                risk_exit_enabled=self.paper_risk_exit_enabled,
                competition_snapshot=self._competition_snapshot(),
                market_freshness_decision=freshness_decision,
            )

        safety_decision = self._evaluate_market_safety()

        if safety_decision is not None and not safety_decision.safe:
            self.order_manager.cancel_all()

            return ConservativePaperTradingStepResult(
                mid_price=mid_price,
                risk_exit_enabled=self.paper_risk_exit_enabled,
                competition_snapshot=self._competition_snapshot(),
                market_safety_decision=safety_decision,
                market_freshness_decision=freshness_decision,
            )

        orderbook_depth_diagnostics: OrderBookDepthDiagnostics | None = None
        orderbook = self.market.get_orderbook(self.symbol)
        if orderbook is not None:
            orderbook_depth_diagnostics = calculate_orderbook_depth_diagnostics(
                orderbook,
                observed_at=timestamp,
            )

        orderbook_signal: OrderBookSignalSnapshot | None = None
        if self.orderbook_signal_engine is not None:
            if orderbook is not None:
                orderbook_signal = self.orderbook_signal_engine.evaluate(
                    orderbook,
                    observed_at=timestamp,
                )

        if mid_price is not None:
            self.portfolio.update_market_price(mid_price)

        portfolio_risk_decision = self.portfolio_risk_guard.evaluate(
            self.portfolio
        )

        if not portfolio_risk_decision.allowed:
            if not self.paper_risk_exit_enabled:
                self.order_manager.cancel_all()

                return ConservativePaperTradingStepResult(
                    mid_price=mid_price,
                    risk_exit_enabled=self.paper_risk_exit_enabled,
                    competition_snapshot=self._competition_snapshot(),
                    market_safety_decision=safety_decision,
                    market_freshness_decision=freshness_decision,
                    portfolio_risk_decision=portfolio_risk_decision,
                    orderbook_signal=orderbook_signal,
                    orderbook_depth_diagnostics=orderbook_depth_diagnostics,
                )

            return self._handle_latched_risk_exit(
                mid_price=mid_price,
                safety_decision=safety_decision,
                freshness_decision=freshness_decision,
                portfolio_risk_decision=portfolio_risk_decision,
                orderbook_signal=orderbook_signal,
                orderbook_depth_diagnostics=orderbook_depth_diagnostics,
                timestamp=timestamp,
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
            source_orders_by_fill_id = {
                id(fill): source_order
                for fill in fills
                if (
                    source_order := self.broker.source_order_for_fill(fill)
                ) is not None
            }
            confirmed_fill_events = self.confirmed_fill_ledger.record_fills(
                fills=fills,
                starting_position=position_before_fills,
                timestamp=timestamp,
                source_orders_by_fill_id=source_orders_by_fill_id,
            )

        fair_play_status: FairPlayDecision | None = None
        if self.fair_play_guard is not None:
            fair_play_status = self.fair_play_guard.consume(confirmed_fill_events)
            if fair_play_status.latched:
                self.order_manager.cancel_all()
                return ConservativePaperTradingStepResult(
                    mid_price=mid_price,
                    risk_exit_enabled=self.paper_risk_exit_enabled,
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
                    orderbook_signal=orderbook_signal,
                    orderbook_depth_diagnostics=orderbook_depth_diagnostics,
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

        fair_play_by_intent_id = {
            id(intent): decision
            for intent, decision in zip(intents, fair_play_decisions)
        }
        execution_by_intent_id = {
            id(decision.intent): decision for decision in decisions
        }
        submitted_by_intent_id = {
            id(order.intent): order for order in submitted_orders
        }
        trade_intent_events: list[TradeIntentEvent] = []
        if self.trade_intent_ledger is not None:
            trade_intent_events = [
                self.trade_intent_ledger.record_intent(
                    intent,
                    timestamp=timestamp,
                    fair_play_decision=fair_play_by_intent_id.get(id(intent)),
                    execution_decision=execution_by_intent_id.get(id(intent)),
                    submitted_order=submitted_by_intent_id.get(id(intent)),
                )
                for intent in intents
            ]
        purpose_counts: dict[str, int] = {}
        for intent in intents:
            purpose = intent.purpose.value
            purpose_counts[purpose] = purpose_counts.get(purpose, 0) + 1

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
            trade_intent_events=trade_intent_events,
            purpose_counts=purpose_counts,
            orderbook_signal=orderbook_signal,
            orderbook_depth_diagnostics=orderbook_depth_diagnostics,
        )

    def _handle_latched_risk_exit(
        self,
        *,
        mid_price: Decimal | None,
        safety_decision: MarketSafetyDecision | None,
        freshness_decision: MarketFreshnessDecision | None,
        portfolio_risk_decision: PortfolioRiskDecision,
        orderbook_signal: OrderBookSignalSnapshot | None,
        orderbook_depth_diagnostics: OrderBookDepthDiagnostics | None,
        timestamp: datetime | None,
    ) -> ConservativePaperTradingStepResult:
        """Handle the explicitly enabled paper-only capital-protection exit.

        Ordinary strategy orders are cancelled and never regenerated after a
        latched risk stop.  A single already-open RISK_EXIT may remain so the
        existing conservative broker can confirm it on a later snapshot.
        """
        cancel_all_except = getattr(self.order_manager, "cancel_all_except", None)
        if callable(cancel_all_except):
            cancel_all_except(
                lambda order: order.intent.purpose == OrderPurpose.RISK_EXIT
            )
        else:
            # Compatibility fallback for narrow test doubles.  Production
            # OrderManager exposes cancel_all_except above.
            self.order_manager.cancel_all()
        existing_risk_exit_orders = [
            order
            for order in self.broker.open_orders
            if order.intent.purpose == OrderPurpose.RISK_EXIT
        ]

        position_before_fills = self.portfolio.base_position
        fills: list[PaperFill] = []
        if existing_risk_exit_orders:
            fills = self.broker.process_market(self.market)

        confirmed_fill_events = self._record_confirmed_fills(
            fills=fills,
            starting_position=position_before_fills,
            timestamp=timestamp,
        )

        intents: list[OrderIntent] = []
        decisions: list[OrderDecision] = []
        submitted_orders: list[PaperOrder] = []
        trade_intent_events: list[TradeIntentEvent] = []
        if (
            not existing_risk_exit_orders
            and self.portfolio.base_position > 0
        ):
            best_bid = self.market.best_bid(self.symbol)
            if best_bid is not None and best_bid.price > 0:
                intent = OrderIntent(
                    symbol=self.symbol,
                    side="sell",
                    order_type="limit",
                    # A low paper-only limit is marketable for the current
                    # book and remains reduce-only through the broker's
                    # min(quantity, base_position) sell rule.
                    price=best_bid.price * Decimal("0.5"),
                    quantity=self.portfolio.base_position,
                    purpose=OrderPurpose.RISK_EXIT,
                    strategy_name="paper_emergency_risk_exit",
                    rationale=(
                        "emergency capital-protection action; "
                        "paper-only reduce-only exit"
                    ),
                )
                decision = OrderDecision(
                    approved=True,
                    reason="paper_risk_exit_approved",
                    intent=intent,
                )
                intents.append(intent)
                decisions.append(decision)
                submitted_orders = self.order_manager.replace_orders([decision])

                if self.trade_intent_ledger is not None:
                    trade_intent_events = [
                        self.trade_intent_ledger.record_intent(
                            intent,
                            timestamp=timestamp,
                            execution_decision=decision,
                            submitted_order=(
                                submitted_orders[0]
                                if submitted_orders
                                else None
                            ),
                        )
                    ]

        fair_play_status = None
        if self.fair_play_guard is not None:
            fair_play_status = self.fair_play_guard.consume(confirmed_fill_events)
        risk_exit_reason = "risk_exit_emergency_capital_protection"
        risk_exit_fill_count = sum(
            event.purpose == OrderPurpose.RISK_EXIT.value
            for event in confirmed_fill_events
        )
        purpose_counts = {
            OrderPurpose.RISK_EXIT.value: len(intents),
        } if intents else {}

        return ConservativePaperTradingStepResult(
            mid_price=mid_price,
            risk_exit_enabled=self.paper_risk_exit_enabled,
            intents=intents,
            decisions=decisions,
            fills=fills,
            submitted_orders=submitted_orders,
            competition_snapshot=self._competition_snapshot(),
            market_safety_decision=safety_decision,
            market_freshness_decision=freshness_decision,
            portfolio_risk_decision=portfolio_risk_decision,
            confirmed_fill_events=confirmed_fill_events,
            fair_play_allowed=None,
            fair_play_reason=risk_exit_reason,
            fair_play_latched=(
                fair_play_status.latched if fair_play_status is not None else None
            ),
            risk_exit_intents_count=len(intents),
            risk_exit_fills_count=risk_exit_fill_count,
            risk_exit_reason=risk_exit_reason,
            trade_intent_events=trade_intent_events,
            purpose_counts=purpose_counts,
            orderbook_signal=orderbook_signal,
            orderbook_depth_diagnostics=orderbook_depth_diagnostics,
        )

    def _record_confirmed_fills(
        self,
        *,
        fills: list[PaperFill],
        starting_position: Decimal,
        timestamp: datetime | None,
    ) -> list[ConfirmedFillEvent]:
        if self.competition is not None:
            for fill in fills:
                self.competition.record_trade(
                    symbol=fill.symbol,
                    notional=fill.notional,
                    timestamp=timestamp,
                )

        if self.confirmed_fill_ledger is None:
            return []

        source_orders_by_fill_id = {
            id(fill): source_order
            for fill in fills
            if (
                source_order := self.broker.source_order_for_fill(fill)
            ) is not None
        }
        return self.confirmed_fill_ledger.record_fills(
            fills=fills,
            starting_position=starting_position,
            timestamp=timestamp,
            source_orders_by_fill_id=source_orders_by_fill_id,
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
