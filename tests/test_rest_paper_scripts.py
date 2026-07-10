from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from bot.risk.market_freshness import (
    MarketFreshnessDecision,
    MarketFreshnessGuard,
    MarketFreshnessLimits,
)
from scripts import run_rest_paper_loop, run_rest_paper_once


@pytest.mark.parametrize(
    "build_engine",
    [
        run_rest_paper_once.build_engine,
        run_rest_paper_loop.build_engine,
    ],
)
def test_rest_paper_engine_uses_configured_market_freshness(build_engine):
    limits = MarketFreshnessLimits(
        max_exchange_age_seconds=Decimal("11"),
        max_unchanged_seconds=Decimal("12"),
        max_future_skew_seconds=Decimal("13"),
    )

    engine = build_engine(
        symbol="SOMI:USDso",
        market_cache=run_rest_paper_loop.MarketCache(),
        initial_cash=Decimal("150"),
        order_size_usd=Decimal("5"),
        max_open_orders=2,
        pair_boost=Decimal("1"),
        max_spread_percent=Decimal("0.02"),
        min_best_bid_quantity=Decimal("1"),
        min_best_ask_quantity=Decimal("1"),
        market_freshness_limits=limits,
    )

    assert isinstance(engine.market_freshness, MarketFreshnessGuard)
    assert engine.market_freshness.limits == limits


def test_loop_build_record_captures_market_freshness():
    freshness = MarketFreshnessDecision(
        fresh=False,
        reason="repeated_snapshot",
        exchange_age_seconds=Decimal("4.25"),
        unchanged_seconds=Decimal("31.5"),
    )
    result = SimpleNamespace(
        market_safety_decision=None,
        market_freshness_decision=freshness,
        intents=[],
        decisions=[],
        fills=[],
        submitted_orders=[],
    )
    snapshot = SimpleNamespace(
        best_bid=None,
        best_ask=None,
        mid_price=None,
        spread=None,
    )
    portfolio = SimpleNamespace(
        cash_balance=Decimal("150"),
        base_position=Decimal("0"),
        equity=Decimal("150"),
        realized_pnl=Decimal("0"),
        unrealized_pnl=Decimal("0"),
        drawdown=Decimal("0"),
        total_volume=Decimal("0"),
    )
    engine = SimpleNamespace(
        portfolio=portfolio,
        competition=None,
        broker=SimpleNamespace(open_orders=[]),
    )

    record = run_rest_paper_loop.build_record(
        timestamp=datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc),
        symbol="SOMI:USDso",
        snapshot=snapshot,
        result=result,
        engine=engine,
    )

    assert record.market_fresh is False
    assert record.market_freshness_reason == "repeated_snapshot"
    assert record.exchange_age_seconds == Decimal("4.25")
    assert record.unchanged_seconds == Decimal("31.5")
