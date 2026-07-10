from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum

from bot.market.models import OrderBook


class OrderBookSignalState(str, Enum):
    BULLISH = "bullish"
    BEARISH = "bearish"
    NEUTRAL = "neutral"
    WARMING_UP = "warming_up"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class OrderBookSignalSnapshot:
    timestamp: datetime
    symbol: str
    state: OrderBookSignalState
    reason: str
    sample_count: int
    best_bid: Decimal | None
    best_ask: Decimal | None
    mid_price: Decimal | None
    spread_bps: Decimal | None
    bid_depth: Decimal | None
    ask_depth: Decimal | None
    depth_imbalance: Decimal | None
    microprice: Decimal | None
    microprice_edge_bps: Decimal | None
    one_step_return_bps: Decimal | None
    rolling_momentum_bps: Decimal | None
    confidence: Decimal


@dataclass(frozen=True)
class OrderBookSignalLimits:
    top_levels: int = 5
    rolling_window: int = 12
    minimum_samples: int = 4
    imbalance_threshold: Decimal = Decimal("0.20")
    microprice_edge_threshold_bps: Decimal = Decimal("1")
    momentum_threshold_bps: Decimal = Decimal("2")
    maximum_signal_spread_bps: Decimal = Decimal("30")

    def __post_init__(self) -> None:
        if self.top_levels < 1:
            raise ValueError("top_levels must be >= 1")
        if self.rolling_window < 2:
            raise ValueError("rolling_window must be >= 2")
        if self.minimum_samples < 2:
            raise ValueError("minimum_samples must be >= 2")
        if self.minimum_samples > self.rolling_window:
            raise ValueError("minimum_samples must be <= rolling_window")

        imbalance = Decimal(str(self.imbalance_threshold))
        edge = Decimal(str(self.microprice_edge_threshold_bps))
        momentum = Decimal(str(self.momentum_threshold_bps))
        spread = Decimal(str(self.maximum_signal_spread_bps))
        if not imbalance.is_finite() or imbalance < 0 or imbalance > 1:
            raise ValueError("imbalance_threshold must be between 0 and 1")
        if not edge.is_finite() or edge < 0:
            raise ValueError("microprice_edge_threshold_bps must be >= 0")
        if not momentum.is_finite() or momentum < 0:
            raise ValueError("momentum_threshold_bps must be >= 0")
        if not spread.is_finite() or spread < 0:
            raise ValueError("maximum_signal_spread_bps must be >= 0")
        object.__setattr__(self, "imbalance_threshold", imbalance)
        object.__setattr__(self, "microprice_edge_threshold_bps", edge)
        object.__setattr__(self, "momentum_threshold_bps", momentum)
        object.__setattr__(self, "maximum_signal_spread_bps", spread)


class OrderBookSignalEngine:
    """Read-only rolling order-book diagnostics with per-symbol history."""

    def __init__(self, limits: OrderBookSignalLimits | None = None) -> None:
        self.limits = limits or OrderBookSignalLimits()
        self._mid_history: dict[str, deque[Decimal]] = {}

    def evaluate(
        self,
        orderbook: OrderBook,
        observed_at: datetime | None = None,
    ) -> OrderBookSignalSnapshot:
        timestamp = self._to_utc(observed_at)
        symbol = orderbook.symbol
        history = self._mid_history.setdefault(
            symbol,
            deque(maxlen=self.limits.rolling_window),
        )
        sample_count = len(history)

        if not orderbook.bids or not orderbook.asks:
            return self._unavailable(timestamp, symbol, sample_count, "missing_side")

        best_bid_level = orderbook.bids[0]
        best_ask_level = orderbook.asks[0]
        best_bid = best_bid_level.price
        best_ask = best_ask_level.price
        if (
            not best_bid.is_finite()
            or not best_ask.is_finite()
            or best_bid <= 0
            or best_ask <= 0
        ):
            return self._unavailable(
                timestamp,
                symbol,
                sample_count,
                "invalid_price",
                best_bid=best_bid,
                best_ask=best_ask,
            )
        if best_bid >= best_ask:
            return self._unavailable(
                timestamp,
                symbol,
                sample_count,
                "crossed_book",
                best_bid=best_bid,
                best_ask=best_ask,
            )

        bid_depth = sum(
            (level.quantity for level in orderbook.bids[: self.limits.top_levels]),
            Decimal("0"),
        )
        ask_depth = sum(
            (level.quantity for level in orderbook.asks[: self.limits.top_levels]),
            Decimal("0"),
        )
        top_quantity = best_bid_level.quantity + best_ask_level.quantity
        total_depth = bid_depth + ask_depth
        if (
            not bid_depth.is_finite()
            or not ask_depth.is_finite()
            or not top_quantity.is_finite()
            or bid_depth <= 0
            or ask_depth <= 0
            or total_depth <= 0
            or top_quantity <= 0
        ):
            return self._unavailable(
                timestamp,
                symbol,
                sample_count,
                "zero_depth",
                best_bid=best_bid,
                best_ask=best_ask,
                bid_depth=bid_depth,
                ask_depth=ask_depth,
            )

        mid_price = (best_bid + best_ask) / Decimal("2")
        spread_bps = (best_ask - best_bid) / mid_price * Decimal("10000")
        depth_imbalance = (bid_depth - ask_depth) / total_depth
        microprice = (
            best_ask * best_bid_level.quantity
            + best_bid * best_ask_level.quantity
        ) / top_quantity
        microprice_edge_bps = (
            (microprice - mid_price) / mid_price * Decimal("10000")
        )
        previous_mid = history[-1] if history else None
        one_step_return_bps = (
            (mid_price - previous_mid) / previous_mid * Decimal("10000")
            if previous_mid is not None
            else None
        )
        history.append(mid_price)
        sample_count = len(history)
        oldest_mid = history[0]
        rolling_momentum_bps = (
            (mid_price - oldest_mid) / oldest_mid * Decimal("10000")
        )

        if sample_count < self.limits.minimum_samples:
            return OrderBookSignalSnapshot(
                timestamp=timestamp,
                symbol=symbol,
                state=OrderBookSignalState.WARMING_UP,
                reason="minimum_samples_not_reached",
                sample_count=sample_count,
                best_bid=best_bid,
                best_ask=best_ask,
                mid_price=mid_price,
                spread_bps=spread_bps,
                bid_depth=bid_depth,
                ask_depth=ask_depth,
                depth_imbalance=depth_imbalance,
                microprice=microprice,
                microprice_edge_bps=microprice_edge_bps,
                one_step_return_bps=one_step_return_bps,
                rolling_momentum_bps=rolling_momentum_bps,
                confidence=Decimal("0"),
            )

        confidence = self._confidence(
            depth_imbalance,
            microprice_edge_bps,
            rolling_momentum_bps,
        )
        if spread_bps > self.limits.maximum_signal_spread_bps:
            state = OrderBookSignalState.NEUTRAL
            reason = "spread_too_wide"
        elif (
            depth_imbalance >= self.limits.imbalance_threshold
            and microprice_edge_bps >= self.limits.microprice_edge_threshold_bps
            and rolling_momentum_bps >= self.limits.momentum_threshold_bps
        ):
            state = OrderBookSignalState.BULLISH
            reason = "bullish_confirmation"
        elif (
            depth_imbalance <= -self.limits.imbalance_threshold
            and microprice_edge_bps <= -self.limits.microprice_edge_threshold_bps
            and rolling_momentum_bps <= -self.limits.momentum_threshold_bps
        ):
            state = OrderBookSignalState.BEARISH
            reason = "bearish_confirmation"
        else:
            state = OrderBookSignalState.NEUTRAL
            reason = "mixed_signals"

        return OrderBookSignalSnapshot(
            timestamp=timestamp,
            symbol=symbol,
            state=state,
            reason=reason,
            sample_count=sample_count,
            best_bid=best_bid,
            best_ask=best_ask,
            mid_price=mid_price,
            spread_bps=spread_bps,
            bid_depth=bid_depth,
            ask_depth=ask_depth,
            depth_imbalance=depth_imbalance,
            microprice=microprice,
            microprice_edge_bps=microprice_edge_bps,
            one_step_return_bps=one_step_return_bps,
            rolling_momentum_bps=rolling_momentum_bps,
            confidence=confidence,
        )

    def reset(self, symbol: str | None = None) -> None:
        if symbol is None:
            self._mid_history.clear()
            return
        self._mid_history.pop(symbol, None)

    def _unavailable(
        self,
        timestamp: datetime,
        symbol: str,
        sample_count: int,
        reason: str,
        *,
        best_bid: Decimal | None = None,
        best_ask: Decimal | None = None,
        bid_depth: Decimal | None = None,
        ask_depth: Decimal | None = None,
    ) -> OrderBookSignalSnapshot:
        return OrderBookSignalSnapshot(
            timestamp=timestamp,
            symbol=symbol,
            state=OrderBookSignalState.UNAVAILABLE,
            reason=reason,
            sample_count=sample_count,
            best_bid=best_bid,
            best_ask=best_ask,
            mid_price=None,
            spread_bps=None,
            bid_depth=bid_depth,
            ask_depth=ask_depth,
            depth_imbalance=None,
            microprice=None,
            microprice_edge_bps=None,
            one_step_return_bps=None,
            rolling_momentum_bps=None,
            confidence=Decimal("0"),
        )

    def _confidence(
        self,
        imbalance: Decimal,
        edge_bps: Decimal,
        momentum_bps: Decimal,
    ) -> Decimal:
        values = (
            self._normalized_magnitude(imbalance, self.limits.imbalance_threshold),
            self._normalized_magnitude(
                edge_bps,
                self.limits.microprice_edge_threshold_bps,
            ),
            self._normalized_magnitude(
                momentum_bps,
                self.limits.momentum_threshold_bps,
            ),
        )
        return sum(values, Decimal("0")) / Decimal(len(values))

    @staticmethod
    def _normalized_magnitude(value: Decimal, threshold: Decimal) -> Decimal:
        magnitude = abs(value)
        if threshold == 0:
            return Decimal("1") if magnitude > 0 else Decimal("0")
        return min(magnitude / threshold, Decimal("1"))

    @staticmethod
    def _to_utc(value: datetime | None) -> datetime:
        if value is None:
            return datetime.now(timezone.utc)
        if value.tzinfo is None or value.utcoffset() is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
