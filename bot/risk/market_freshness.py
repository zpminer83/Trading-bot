from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from bot.market.market_cache import MarketCache
from bot.market.models import OrderBook


@dataclass(frozen=True)
class MarketFreshnessLimits:
    """
    Time limits for market-data freshness checks.

    Values are expressed in seconds.
    """

    max_exchange_age_seconds: Decimal = Decimal("30")
    max_unchanged_seconds: Decimal = Decimal("30")
    max_future_skew_seconds: Decimal = Decimal("5")
    require_timestamp: bool = True


@dataclass(frozen=True)
class MarketFreshnessDecision:
    fresh: bool
    reason: str

    exchange_age_seconds: Decimal | None = None
    unchanged_seconds: Decimal = Decimal("0")

    timestamp_changed: bool | None = None
    nonce_changed: bool | None = None

    details: list[str] = field(default_factory=list)


@dataclass
class _FreshnessState:
    timestamp: int
    nonce: str
    fingerprint: tuple[Any, ...]
    last_change_at: datetime
    last_seen_at: datetime


class MarketFreshnessGuard:
    """
    Detects stale, repeated, future-dated, or regressing orderbooks.

    Supported exchange timestamp formats:
    - Unix seconds
    - Unix milliseconds
    - Unix microseconds
    - Unix nanoseconds

    The guard also remembers the last snapshot for each symbol.
    If exactly the same snapshot is observed for too long, it is
    considered stale even if the program itself is still running.
    """

    def __init__(
        self,
        limits: MarketFreshnessLimits | None = None,
    ):
        self.limits = limits or MarketFreshnessLimits()
        self._states: dict[str, _FreshnessState] = {}

    def evaluate(
        self,
        market: MarketCache,
        symbol: str,
        observed_at: datetime | None = None,
    ) -> MarketFreshnessDecision:
        now = self._to_utc(
            observed_at or datetime.now(timezone.utc)
        )

        orderbook = market.get_orderbook(symbol)

        if orderbook is None:
            return MarketFreshnessDecision(
                fresh=False,
                reason="missing_orderbook",
                details=[f"no orderbook for symbol {symbol}"],
            )

        timestamp = int(orderbook.timestamp or 0)
        nonce = str(orderbook.nonce or "")

        if timestamp <= 0 and self.limits.require_timestamp:
            return MarketFreshnessDecision(
                fresh=False,
                reason="missing_timestamp",
                details=["orderbook timestamp is missing or non-positive"],
            )

        exchange_age_seconds: Decimal | None = None

        if timestamp > 0:
            try:
                exchange_time = self._timestamp_to_datetime(timestamp)
            except (OverflowError, OSError, ValueError):
                return MarketFreshnessDecision(
                    fresh=False,
                    reason="invalid_timestamp",
                    details=[f"invalid orderbook timestamp: {timestamp}"],
                )

            exchange_age_seconds = Decimal(
                str((now - exchange_time).total_seconds())
            )

            if (
                exchange_age_seconds
                > self.limits.max_exchange_age_seconds
            ):
                return MarketFreshnessDecision(
                    fresh=False,
                    reason="stale_timestamp",
                    exchange_age_seconds=exchange_age_seconds,
                    details=[
                        f"exchange_age_seconds={exchange_age_seconds}",
                        (
                            "max_exchange_age_seconds="
                            f"{self.limits.max_exchange_age_seconds}"
                        ),
                    ],
                )

            if (
                exchange_age_seconds
                < -self.limits.max_future_skew_seconds
            ):
                return MarketFreshnessDecision(
                    fresh=False,
                    reason="future_timestamp",
                    exchange_age_seconds=exchange_age_seconds,
                    details=[
                        f"exchange_age_seconds={exchange_age_seconds}",
                        (
                            "max_future_skew_seconds="
                            f"{self.limits.max_future_skew_seconds}"
                        ),
                    ],
                )

        fingerprint = self._build_fingerprint(orderbook)
        previous = self._states.get(symbol)

        if previous is None:
            self._states[symbol] = _FreshnessState(
                timestamp=timestamp,
                nonce=nonce,
                fingerprint=fingerprint,
                last_change_at=now,
                last_seen_at=now,
            )

            return MarketFreshnessDecision(
                fresh=True,
                reason="ok",
                exchange_age_seconds=exchange_age_seconds,
            )

        timestamp_changed = timestamp != previous.timestamp
        nonce_changed = nonce != previous.nonce

        if (
            timestamp > 0
            and previous.timestamp > 0
            and timestamp < previous.timestamp
        ):
            previous.last_seen_at = now

            return MarketFreshnessDecision(
                fresh=False,
                reason="timestamp_regressed",
                exchange_age_seconds=exchange_age_seconds,
                timestamp_changed=timestamp_changed,
                nonce_changed=nonce_changed,
                details=[
                    f"previous_timestamp={previous.timestamp}",
                    f"current_timestamp={timestamp}",
                ],
            )

        snapshot_changed = fingerprint != previous.fingerprint

        if snapshot_changed:
            self._states[symbol] = _FreshnessState(
                timestamp=timestamp,
                nonce=nonce,
                fingerprint=fingerprint,
                last_change_at=now,
                last_seen_at=now,
            )

            return MarketFreshnessDecision(
                fresh=True,
                reason="ok",
                exchange_age_seconds=exchange_age_seconds,
                unchanged_seconds=Decimal("0"),
                timestamp_changed=timestamp_changed,
                nonce_changed=nonce_changed,
            )

        unchanged_seconds = Decimal(
            str((now - previous.last_change_at).total_seconds())
        )

        previous.last_seen_at = now

        if unchanged_seconds > self.limits.max_unchanged_seconds:
            return MarketFreshnessDecision(
                fresh=False,
                reason="repeated_snapshot",
                exchange_age_seconds=exchange_age_seconds,
                unchanged_seconds=unchanged_seconds,
                timestamp_changed=False,
                nonce_changed=False,
                details=[
                    f"unchanged_seconds={unchanged_seconds}",
                    (
                        "max_unchanged_seconds="
                        f"{self.limits.max_unchanged_seconds}"
                    ),
                ],
            )

        return MarketFreshnessDecision(
            fresh=True,
            reason="ok",
            exchange_age_seconds=exchange_age_seconds,
            unchanged_seconds=unchanged_seconds,
            timestamp_changed=False,
            nonce_changed=False,
        )

    def reset(self, symbol: str | None = None) -> None:
        """
        Clears stored freshness history.

        If symbol is omitted, all tracked symbols are cleared.
        """
        if symbol is None:
            self._states.clear()
            return

        self._states.pop(symbol, None)

    @staticmethod
    def _build_fingerprint(
        orderbook: OrderBook,
    ) -> tuple[Any, ...]:
        bids = tuple(
            (level.price, level.quantity)
            for level in orderbook.bids
        )

        asks = tuple(
            (level.price, level.quantity)
            for level in orderbook.asks
        )

        return (
            orderbook.timestamp,
            orderbook.nonce,
            bids,
            asks,
        )

    @staticmethod
    def _timestamp_to_datetime(timestamp: int) -> datetime:
        """
        Converts common Unix timestamp units into UTC datetime.
        """
        absolute_timestamp = abs(timestamp)

        if absolute_timestamp >= 10**17:
            seconds = Decimal(timestamp) / Decimal("1000000000")
        elif absolute_timestamp >= 10**14:
            seconds = Decimal(timestamp) / Decimal("1000000")
        elif absolute_timestamp >= 10**11:
            seconds = Decimal(timestamp) / Decimal("1000")
        else:
            seconds = Decimal(timestamp)

        return datetime.fromtimestamp(
            float(seconds),
            tz=timezone.utc,
        )

    @staticmethod
    def _to_utc(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)

        return value.astimezone(timezone.utc)