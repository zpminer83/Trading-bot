from decimal import Decimal

from bot.market.market_cache import MarketCache
from bot.market.models import OrderBook, OrderBookLevel
from bot.risk.market_safety import MarketSafety, MarketSafetyLimits


SYMBOL = "SOMI:USDso"


def make_market(
    bid: str = "1.00",
    ask: str = "1.01",
    bid_qty: str = "100",
    ask_qty: str = "100",
) -> MarketCache:
    market = MarketCache()

    market.update_orderbook(
        OrderBook(
            symbol=SYMBOL,
            bids=[
                OrderBookLevel(
                    price=Decimal(bid),
                    quantity=Decimal(bid_qty),
                )
            ],
            asks=[
                OrderBookLevel(
                    price=Decimal(ask),
                    quantity=Decimal(ask_qty),
                )
            ],
            timestamp=12345,
        )
    )

    return market


def test_market_safety_accepts_normal_market():
    market = make_market()
    safety = MarketSafety()

    decision = safety.evaluate(
        market=market,
        symbol=SYMBOL,
    )

    assert decision.safe is True
    assert decision.reason == "ok"
    assert decision.spread_percent == Decimal("0.01") / Decimal("1.005")


def test_market_safety_rejects_missing_orderbook():
    market = MarketCache()
    safety = MarketSafety()

    decision = safety.evaluate(
        market=market,
        symbol=SYMBOL,
    )

    assert decision.safe is False
    assert decision.reason == "missing_orderbook"


def test_market_safety_rejects_missing_bid():
    market = MarketCache()

    market.update_orderbook(
        OrderBook(
            symbol=SYMBOL,
            bids=[],
            asks=[
                OrderBookLevel(
                    price=Decimal("1.01"),
                    quantity=Decimal("100"),
                )
            ],
            timestamp=12345,
        )
    )

    safety = MarketSafety()

    decision = safety.evaluate(
        market=market,
        symbol=SYMBOL,
    )

    assert decision.safe is False
    assert decision.reason == "missing_bid_or_ask"


def test_market_safety_rejects_missing_ask():
    market = MarketCache()

    market.update_orderbook(
        OrderBook(
            symbol=SYMBOL,
            bids=[
                OrderBookLevel(
                    price=Decimal("1.00"),
                    quantity=Decimal("100"),
                )
            ],
            asks=[],
            timestamp=12345,
        )
    )

    safety = MarketSafety()

    decision = safety.evaluate(
        market=market,
        symbol=SYMBOL,
    )

    assert decision.safe is False
    assert decision.reason == "missing_bid_or_ask"


def test_market_safety_rejects_crossed_orderbook():
    market = make_market(
        bid="1.02",
        ask="1.00",
    )

    safety = MarketSafety()

    decision = safety.evaluate(
        market=market,
        symbol=SYMBOL,
    )

    assert decision.safe is False
    assert decision.reason == "crossed_orderbook"


def test_market_safety_rejects_wide_spread():
    market = make_market(
        bid="1.00",
        ask="1.20",
    )

    safety = MarketSafety(
        limits=MarketSafetyLimits(
            max_spread_percent=Decimal("0.05"),
        )
    )

    decision = safety.evaluate(
        market=market,
        symbol=SYMBOL,
    )

    assert decision.safe is False
    assert decision.reason == "spread_too_wide"
    assert decision.spread_percent is not None
    assert decision.spread_percent > Decimal("0.05")


def test_market_safety_rejects_insufficient_bid_liquidity():
    market = make_market(
        bid_qty="0.5",
        ask_qty="100",
    )

    safety = MarketSafety(
        limits=MarketSafetyLimits(
            min_best_bid_quantity=Decimal("1"),
            min_best_ask_quantity=Decimal("1"),
        )
    )

    decision = safety.evaluate(
        market=market,
        symbol=SYMBOL,
    )

    assert decision.safe is False
    assert decision.reason == "insufficient_bid_liquidity"


def test_market_safety_rejects_insufficient_ask_liquidity():
    market = make_market(
        bid_qty="100",
        ask_qty="0.5",
    )

    safety = MarketSafety(
        limits=MarketSafetyLimits(
            min_best_bid_quantity=Decimal("1"),
            min_best_ask_quantity=Decimal("1"),
        )
    )

    decision = safety.evaluate(
        market=market,
        symbol=SYMBOL,
    )

    assert decision.safe is False
    assert decision.reason == "insufficient_ask_liquidity"