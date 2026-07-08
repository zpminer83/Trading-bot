from dataclasses import dataclass, field

from bot.execution.execution_manager import ExecutionManager
from bot.execution.order import OrderDecision, OrderIntent
from bot.execution.paper_broker import PaperBroker, PaperFill
from bot.market.market_cache import MarketCache
from bot.portfolio.portfolio_manager import PortfolioManager
from bot.strategy.passive_market_maker import PassiveMarketMakerStrategy


@dataclass(frozen=True)
class PaperTradingStepResult:
    intents: list[OrderIntent] = field(default_factory=list)
    decisions: list[OrderDecision] = field(default_factory=list)
    fills: list[PaperFill] = field(default_factory=list)


class PaperTradingEngine:
    """
    Runs one safe paper-trading cycle:

    MarketCache -> Strategy -> OrderIntent -> ExecutionManager -> RiskManager -> PaperBroker
    """

    def __init__(
        self,
        symbol: str,
        market: MarketCache,
        portfolio: PortfolioManager,
        strategy: PassiveMarketMakerStrategy,
        execution: ExecutionManager,
        broker: PaperBroker,
    ):
        self.symbol = symbol
        self.market = market
        self.portfolio = portfolio
        self.strategy = strategy
        self.execution = execution
        self.broker = broker

    def step(self) -> PaperTradingStepResult:
        mid_price = self.market.mid_price(self.symbol)

        if mid_price is not None:
            self.portfolio.update_market_price(mid_price)

        intents = self.strategy.generate_orders(
            market=self.market,
            portfolio=self.portfolio,
        )

        decisions: list[OrderDecision] = []
        fills: list[PaperFill] = []

        for intent in intents:
            decision = self.execution.review_order(intent)
            decisions.append(decision)

            fill = self.broker.execute(decision)

            if fill is not None:
                fills.append(fill)

        return PaperTradingStepResult(
            intents=intents,
            decisions=decisions,
            fills=fills,
        )