from dataclasses import dataclass, field
from decimal import Decimal

from bot.market.market_cache import MarketCache


@dataclass(frozen=True)
class MarketSafetyLimits:
    max_spread_percent: Decimal = Decimal("0.01")
    min_best_bid_quantity: Decimal = Decimal("1")
    min_best_ask_quantity: Decimal = Decimal("1")


@dataclass(frozen=True)
class MarketSafetyDecision:
    safe: bool
    reason: str
    spread_percent: Decimal | None = None
    details: list[str] = field(default_factory=list)


class MarketSafety:
    """
    Validates whether current market conditions are safe enough
    for the bot to generate new orders.

    This is not PnL risk management.
    This is market-data / microstructure safety.

    It protects against:
    - missing orderbook
    - missing bid/ask
    - invalid or crossed prices
    - too-wide spread
    - insufficient top-of-book liquidity
    """

    def __init__(
        self,
        limits: MarketSafetyLimits | None = None,
    ):
        self.limits = limits or MarketSafetyLimits()

    def evaluate(
        self,
        market: MarketCache,
        symbol: str,
    ) -> MarketSafetyDecision:
        orderbook = market.get_orderbook(symbol)

        if orderbook is None:
            return MarketSafetyDecision(
                safe=False,
                reason="missing_orderbook",
                details=[f"no orderbook for symbol {symbol}"],
            )

        best_bid = market.best_bid(symbol)
        best_ask = market.best_ask(symbol)

        if best_bid is None or best_ask is None:
            return MarketSafetyDecision(
                safe=False,
                reason="missing_bid_or_ask",
                details=["best bid or best ask is missing"],
            )

        if best_bid.price <= 0 or best_ask.price <= 0:
            return MarketSafetyDecision(
                safe=False,
                reason="invalid_price",
                details=["best bid or best ask price is non-positive"],
            )

        spread = best_ask.price - best_bid.price

        if spread < 0:
            return MarketSafetyDecision(
                safe=False,
                reason="crossed_orderbook",
                details=["best ask is lower than best bid"],
            )

        mid_price = market.mid_price(symbol)

        if mid_price is None or mid_price <= 0:
            return MarketSafetyDecision(
                safe=False,
                reason="invalid_mid_price",
                details=["mid price is missing or non-positive"],
            )

        spread_percent = spread / mid_price

        if spread_percent > self.limits.max_spread_percent:
            return MarketSafetyDecision(
                safe=False,
                reason="spread_too_wide",
                spread_percent=spread_percent,
                details=[
                    f"spread_percent={spread_percent}",
                    f"max_spread_percent={self.limits.max_spread_percent}",
                ],
            )

        if best_bid.quantity < self.limits.min_best_bid_quantity:
            return MarketSafetyDecision(
                safe=False,
                reason="insufficient_bid_liquidity",
                spread_percent=spread_percent,
                details=[
                    f"best_bid_quantity={best_bid.quantity}",
                    f"min_best_bid_quantity={self.limits.min_best_bid_quantity}",
                ],
            )

        if best_ask.quantity < self.limits.min_best_ask_quantity:
            return MarketSafetyDecision(
                safe=False,
                reason="insufficient_ask_liquidity",
                spread_percent=spread_percent,
                details=[
                    f"best_ask_quantity={best_ask.quantity}",
                    f"min_best_ask_quantity={self.limits.min_best_ask_quantity}",
                ],
            )

        return MarketSafetyDecision(
            safe=True,
            reason="ok",
            spread_percent=spread_percent,
        )