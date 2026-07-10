from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from bot.competition.competition_tracker import (
    CompetitionSnapshot,
    CompetitionTracker,
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


class ConservativePaperTradingEngine:
    """
    Runs one conservative paper-trading cycle.

    Processing order:

    1. check market-data freshness
    2. check market safety
    3. update portfolio mark price
    4. process existing paper orders
    5. record fills in competition tracker
    6. generate strategy intents
    7. run execution and risk checks
    8. replace stale orders with approved new orders

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

        fills = self.broker.process_market(self.market)

        if self.competition is not None:
            for fill in fills:
                self.competition.record_trade(
                    symbol=fill.symbol,
                    notional=fill.notional,
                    timestamp=timestamp,
                )

        intents = self.strategy.generate_orders(
            market=self.market,
            portfolio=self.portfolio,
        )

        decisions = [
            self.execution.review_order(intent)
            for intent in intents
        ]

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