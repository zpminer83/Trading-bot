from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from bot.market.models import OrderBook, OrderBookLevel
from bot.market.orderbook_signal import (
    OrderBookSignalEngine,
    OrderBookSignalLimits,
    OrderBookSignalState,
)


NOW = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)


def book(
    *,
    symbol: str = "SOMI:USDso",
    bid: str = "100",
    ask: str = "101",
    bid_quantity: str = "10",
    ask_quantity: str = "10",
    bid_levels: list[tuple[str, str]] | None = None,
    ask_levels: list[tuple[str, str]] | None = None,
) -> OrderBook:
    bids = bid_levels or [(bid, bid_quantity)]
    asks = ask_levels or [(ask, ask_quantity)]
    return OrderBook(
        symbol=symbol,
        bids=[OrderBookLevel(Decimal(price), Decimal(quantity)) for price, quantity in bids],
        asks=[OrderBookLevel(Decimal(price), Decimal(quantity)) for price, quantity in asks],
    )


def engine(**kwargs) -> OrderBookSignalEngine:
    return OrderBookSignalEngine(OrderBookSignalLimits(**kwargs))


def test_depth_spread_microprice_and_warming_up_calculations():
    signal_engine = engine(top_levels=2, rolling_window=4, minimum_samples=2)
    snapshot = signal_engine.evaluate(
        book(
            bid_quantity="10",
            ask_quantity="20",
            bid_levels=[("100", "10"), ("99", "5")],
            ask_levels=[("101", "20"), ("102", "5")],
        ),
        NOW,
    )

    assert snapshot.state is OrderBookSignalState.WARMING_UP
    assert snapshot.bid_depth == Decimal("15")
    assert snapshot.ask_depth == Decimal("25")
    assert snapshot.depth_imbalance == Decimal("-0.25")
    assert snapshot.mid_price == Decimal("100.5")
    assert snapshot.spread_bps == Decimal("1") / Decimal("100.5") * Decimal("10000")
    assert snapshot.microprice == Decimal("100") + Decimal("1") / Decimal("3")
    assert snapshot.microprice_edge_bps < 0
    assert snapshot.confidence == Decimal("0")


def test_balanced_bid_heavy_and_ask_heavy_depth_imbalances():
    assert engine().evaluate(book(), NOW).depth_imbalance == Decimal("0")
    assert (
        engine().evaluate(book(bid_quantity="30", ask_quantity="10"), NOW).depth_imbalance
        > 0
    )
    assert (
        engine().evaluate(book(bid_quantity="10", ask_quantity="30"), NOW).depth_imbalance
        < 0
    )


def test_returns_momentum_window_and_independent_histories():
    signal_engine = engine(rolling_window=3, minimum_samples=2)
    signal_engine.evaluate(book(bid="100", ask="101"), NOW)
    second = signal_engine.evaluate(book(bid="101", ask="102"), NOW + timedelta(seconds=1))
    third = signal_engine.evaluate(book(bid="102", ask="103"), NOW + timedelta(seconds=2))
    fourth = signal_engine.evaluate(book(bid="103", ask="104"), NOW + timedelta(seconds=3))
    other = signal_engine.evaluate(book(symbol="OTHER"), NOW)

    assert second.one_step_return_bps == (
        (Decimal("101.5") - Decimal("100.5")) / Decimal("100.5") * Decimal("10000")
    )
    assert third.rolling_momentum_bps == (
        (Decimal("102.5") - Decimal("100.5")) / Decimal("100.5") * Decimal("10000")
    )
    assert fourth.sample_count == 3
    assert fourth.rolling_momentum_bps == (
        (Decimal("103.5") - Decimal("101.5")) / Decimal("101.5") * Decimal("10000")
    )
    assert other.sample_count == 1

    signal_engine.reset("SOMI:USDso")
    assert signal_engine.evaluate(book(), NOW).sample_count == 1
    signal_engine.reset()
    assert signal_engine.evaluate(book(symbol="OTHER"), NOW).sample_count == 1


@pytest.mark.parametrize(
    "orderbook, reason",
    [
        (OrderBook(symbol="SOMI:USDso", bids=[], asks=[]), "missing_side"),
        (book(bid="101", ask="101"), "crossed_book"),
        (book(bid_quantity="0", ask_quantity="0"), "zero_depth"),
    ],
)
def test_unavailable_books_do_not_create_signal(orderbook, reason):
    snapshot = engine().evaluate(orderbook, NOW)
    assert snapshot.state is OrderBookSignalState.UNAVAILABLE
    assert snapshot.reason == reason
    assert snapshot.confidence == Decimal("0")


def test_bullish_bearish_and_conservative_neutral_classification():
    bullish = engine(
        rolling_window=3,
        minimum_samples=2,
        maximum_signal_spread_bps=Decimal("200"),
    )
    bullish.evaluate(book(bid="100", ask="101"), NOW)
    bullish_snapshot = bullish.evaluate(
        book(bid="101", ask="102", bid_quantity="30", ask_quantity="10"),
        NOW + timedelta(seconds=1),
    )
    assert bullish_snapshot.state is OrderBookSignalState.BULLISH

    bearish = engine(
        rolling_window=3,
        minimum_samples=2,
        maximum_signal_spread_bps=Decimal("200"),
    )
    bearish.evaluate(book(bid="101", ask="102"), NOW)
    bearish_snapshot = bearish.evaluate(
        book(bid="100", ask="101", bid_quantity="10", ask_quantity="30"),
        NOW + timedelta(seconds=1),
    )
    assert bearish_snapshot.state is OrderBookSignalState.BEARISH

    neutral = engine(
        rolling_window=3,
        minimum_samples=2,
        maximum_signal_spread_bps=Decimal("200"),
    )
    neutral.evaluate(book(bid="100", ask="101"), NOW)
    neutral_snapshot = neutral.evaluate(
        book(
            bid="101",
            ask="102",
            bid_levels=[("101", "10"), ("100", "100")],
            ask_levels=[("102", "30")],
        ),
        NOW + timedelta(seconds=1),
    )
    assert neutral_snapshot.state is OrderBookSignalState.NEUTRAL
    assert neutral_snapshot.reason == "mixed_signals"


def test_neutral_wide_spread_confidence_and_time_ordering():
    signal_engine = engine(
        rolling_window=3,
        minimum_samples=2,
        maximum_signal_spread_bps=Decimal("10"),
    )
    signal_engine.evaluate(book(bid="100", ask="101"), NOW)
    snapshot = signal_engine.evaluate(
        book(bid="100", ask="102", bid_quantity="30", ask_quantity="10"),
        NOW - timedelta(days=1),
    )

    assert snapshot.state is OrderBookSignalState.NEUTRAL
    assert snapshot.reason == "spread_too_wide"
    assert Decimal("0") <= snapshot.confidence <= Decimal("1")
    assert snapshot.one_step_return_bps is not None


@pytest.mark.parametrize(
    "kwargs",
    [
        {"top_levels": 0},
        {"rolling_window": 1},
        {"minimum_samples": 1},
        {"rolling_window": 2, "minimum_samples": 3},
        {"imbalance_threshold": Decimal("1.1")},
        {"microprice_edge_threshold_bps": Decimal("-1")},
    ],
)
def test_signal_limits_validate(kwargs):
    with pytest.raises(ValueError):
        OrderBookSignalLimits(**kwargs)
