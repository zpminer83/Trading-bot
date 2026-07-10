from datetime import datetime, timedelta, timezone
from decimal import Decimal

from bot.market.market_cache import MarketCache
from bot.market.models import OrderBook, OrderBookLevel
from bot.risk.market_freshness import (
    MarketFreshnessGuard,
    MarketFreshnessLimits,
)


SYMBOL = "SOMI:USDso"

BASE_TIME = datetime(
    2026,
    7,
    13,
    12,
    0,
    tzinfo=timezone.utc,
)


def unix_seconds(value: datetime) -> int:
    return int(value.timestamp())


def unix_milliseconds(value: datetime) -> int:
    return int(value.timestamp() * 1000)


def set_market(
    market: MarketCache,
    *,
    timestamp: int,
    nonce: str = "1",
    bid: str = "1.00",
    ask: str = "1.01",
) -> None:
    market.update_orderbook(
        OrderBook(
            symbol=SYMBOL,
            bids=[
                OrderBookLevel(
                    price=Decimal(bid),
                    quantity=Decimal("100"),
                )
            ],
            asks=[
                OrderBookLevel(
                    price=Decimal(ask),
                    quantity=Decimal("100"),
                )
            ],
            timestamp=timestamp,
            nonce=nonce,
        )
    )


def test_market_freshness_accepts_fresh_snapshot():
    market = MarketCache()

    set_market(
        market,
        timestamp=unix_seconds(BASE_TIME),
    )

    guard = MarketFreshnessGuard()

    decision = guard.evaluate(
        market=market,
        symbol=SYMBOL,
        observed_at=BASE_TIME + timedelta(seconds=2),
    )

    assert decision.fresh is True
    assert decision.reason == "ok"
    assert decision.exchange_age_seconds == Decimal("2.0")


def test_market_freshness_supports_millisecond_timestamp():
    market = MarketCache()

    set_market(
        market,
        timestamp=unix_milliseconds(BASE_TIME),
    )

    guard = MarketFreshnessGuard()

    decision = guard.evaluate(
        market=market,
        symbol=SYMBOL,
        observed_at=BASE_TIME + timedelta(seconds=10),
    )

    assert decision.fresh is True
    assert decision.reason == "ok"
    assert decision.exchange_age_seconds == Decimal("10.0")


def test_market_freshness_rejects_missing_orderbook():
    market = MarketCache()
    guard = MarketFreshnessGuard()

    decision = guard.evaluate(
        market=market,
        symbol=SYMBOL,
        observed_at=BASE_TIME,
    )

    assert decision.fresh is False
    assert decision.reason == "missing_orderbook"


def test_market_freshness_rejects_missing_timestamp():
    market = MarketCache()

    set_market(
        market,
        timestamp=0,
    )

    guard = MarketFreshnessGuard()

    decision = guard.evaluate(
        market=market,
        symbol=SYMBOL,
        observed_at=BASE_TIME,
    )

    assert decision.fresh is False
    assert decision.reason == "missing_timestamp"


def test_market_freshness_rejects_stale_timestamp():
    market = MarketCache()

    set_market(
        market,
        timestamp=unix_seconds(BASE_TIME),
    )

    guard = MarketFreshnessGuard(
        limits=MarketFreshnessLimits(
            max_exchange_age_seconds=Decimal("5"),
        )
    )

    decision = guard.evaluate(
        market=market,
        symbol=SYMBOL,
        observed_at=BASE_TIME + timedelta(seconds=10),
    )

    assert decision.fresh is False
    assert decision.reason == "stale_timestamp"
    assert decision.exchange_age_seconds == Decimal("10.0")


def test_market_freshness_rejects_future_timestamp():
    market = MarketCache()

    set_market(
        market,
        timestamp=unix_seconds(
            BASE_TIME + timedelta(seconds=10)
        ),
    )

    guard = MarketFreshnessGuard(
        limits=MarketFreshnessLimits(
            max_future_skew_seconds=Decimal("2"),
        )
    )

    decision = guard.evaluate(
        market=market,
        symbol=SYMBOL,
        observed_at=BASE_TIME,
    )

    assert decision.fresh is False
    assert decision.reason == "future_timestamp"
    assert decision.exchange_age_seconds == Decimal("-10.0")


def test_market_freshness_rejects_repeated_snapshot():
    market = MarketCache()

    set_market(
        market,
        timestamp=unix_seconds(BASE_TIME),
        nonce="1",
    )

    guard = MarketFreshnessGuard(
        limits=MarketFreshnessLimits(
            max_exchange_age_seconds=Decimal("30"),
            max_unchanged_seconds=Decimal("5"),
        )
    )

    first = guard.evaluate(
        market=market,
        symbol=SYMBOL,
        observed_at=BASE_TIME,
    )

    second = guard.evaluate(
        market=market,
        symbol=SYMBOL,
        observed_at=BASE_TIME + timedelta(seconds=6),
    )

    assert first.fresh is True

    assert second.fresh is False
    assert second.reason == "repeated_snapshot"
    assert second.unchanged_seconds == Decimal("6.0")


def test_market_freshness_accepts_changed_nonce():
    market = MarketCache()

    set_market(
        market,
        timestamp=unix_seconds(BASE_TIME),
        nonce="1",
    )

    guard = MarketFreshnessGuard(
        limits=MarketFreshnessLimits(
            max_exchange_age_seconds=Decimal("30"),
            max_unchanged_seconds=Decimal("5"),
        )
    )

    guard.evaluate(
        market=market,
        symbol=SYMBOL,
        observed_at=BASE_TIME,
    )

    set_market(
        market,
        timestamp=unix_seconds(BASE_TIME),
        nonce="2",
    )

    decision = guard.evaluate(
        market=market,
        symbol=SYMBOL,
        observed_at=BASE_TIME + timedelta(seconds=4),
    )

    assert decision.fresh is True
    assert decision.reason == "ok"
    assert decision.nonce_changed is True
    assert decision.unchanged_seconds == Decimal("0")


def test_market_freshness_rejects_regressing_timestamp():
    market = MarketCache()

    set_market(
        market,
        timestamp=unix_seconds(BASE_TIME),
        nonce="10",
    )

    guard = MarketFreshnessGuard(
        limits=MarketFreshnessLimits(
            max_exchange_age_seconds=Decimal("30"),
        )
    )

    guard.evaluate(
        market=market,
        symbol=SYMBOL,
        observed_at=BASE_TIME,
    )

    set_market(
        market,
        timestamp=unix_seconds(
            BASE_TIME - timedelta(seconds=1)
        ),
        nonce="9",
    )

    decision = guard.evaluate(
        market=market,
        symbol=SYMBOL,
        observed_at=BASE_TIME + timedelta(seconds=1),
    )

    assert decision.fresh is False
    assert decision.reason == "timestamp_regressed"
    assert decision.timestamp_changed is True
    assert decision.nonce_changed is True