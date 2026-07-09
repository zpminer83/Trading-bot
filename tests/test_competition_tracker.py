from datetime import datetime, timezone
from decimal import Decimal

import pytest

from bot.competition.competition_tracker import (
    CompetitionConfig,
    CompetitionTracker,
)


def utc_dt(
    year: int,
    month: int,
    day: int,
    hour: int = 0,
    minute: int = 0,
) -> datetime:
    return datetime(
        year,
        month,
        day,
        hour,
        minute,
        tzinfo=timezone.utc,
    )


def test_competition_tracker_records_weekly_volume():
    tracker = CompetitionTracker(now=utc_dt(2026, 7, 13))

    tracker.record_trade(
        symbol="SOMI:USDso",
        notional=Decimal("100"),
        timestamp=utc_dt(2026, 7, 13, 12),
    )

    tracker.record_trade(
        symbol="SOMI:USDso",
        notional=Decimal("50"),
        timestamp=utc_dt(2026, 7, 14, 12),
    )

    assert tracker.weekly_volume == Decimal("150")
    assert tracker.pair_volumes["SOMI:USDso"] == Decimal("150")


def test_competition_tracker_estimates_score_with_default_boost():
    tracker = CompetitionTracker(now=utc_dt(2026, 7, 13))

    tracker.record_trade(
        symbol="SOMI:USDso",
        notional=Decimal("100"),
        timestamp=utc_dt(2026, 7, 13),
    )

    assert tracker.estimated_score == Decimal("100")


def test_competition_tracker_applies_pair_boost():
    tracker = CompetitionTracker(now=utc_dt(2026, 7, 13))

    tracker.set_pair_boost(
        symbol="SOMI:USDso",
        boost=Decimal("1.5"),
    )

    tracker.record_trade(
        symbol="SOMI:USDso",
        notional=Decimal("100"),
        timestamp=utc_dt(2026, 7, 13),
    )

    assert tracker.weekly_volume == Decimal("100")
    assert tracker.estimated_score == Decimal("150.0")


def test_competition_tracker_applies_challenge_multiplier():
    tracker = CompetitionTracker(now=utc_dt(2026, 7, 13))

    tracker.set_pair_boost(
        symbol="SOMI:USDso",
        boost=Decimal("1.2"),
    )

    tracker.set_challenge_multiplier(Decimal("1.1"))

    tracker.record_trade(
        symbol="SOMI:USDso",
        notional=Decimal("100"),
        timestamp=utc_dt(2026, 7, 13),
    )

    assert tracker.estimated_score == Decimal("132.00")


def test_competition_tracker_calculates_raffle_tickets():
    tracker = CompetitionTracker(now=utc_dt(2026, 7, 13))

    tracker.record_trade(
        symbol="SOMI:USDso",
        notional=Decimal("2499"),
        timestamp=utc_dt(2026, 7, 13),
    )

    assert tracker.raffle_tickets == 0

    tracker.record_trade(
        symbol="SOMI:USDso",
        notional=Decimal("1"),
        timestamp=utc_dt(2026, 7, 13),
    )

    assert tracker.raffle_tickets == 1


def test_competition_tracker_caps_raffle_tickets():
    tracker = CompetitionTracker(now=utc_dt(2026, 7, 13))

    tracker.record_trade(
        symbol="SOMI:USDso",
        notional=Decimal("300000"),
        timestamp=utc_dt(2026, 7, 13),
    )

    assert tracker.raffle_tickets == 100


def test_competition_tracker_resets_on_new_week():
    tracker = CompetitionTracker(now=utc_dt(2026, 7, 13))

    tracker.record_trade(
        symbol="SOMI:USDso",
        notional=Decimal("100"),
        timestamp=utc_dt(2026, 7, 13),
    )

    assert tracker.weekly_volume == Decimal("100")

    tracker.record_trade(
        symbol="SOMI:USDso",
        notional=Decimal("50"),
        timestamp=utc_dt(2026, 7, 20),
    )

    assert tracker.weekly_volume == Decimal("50")
    assert tracker.pair_volumes["SOMI:USDso"] == Decimal("50")


def test_competition_tracker_week_starts_on_monday_utc():
    tracker = CompetitionTracker(now=utc_dt(2026, 7, 15, 12))

    assert tracker.week_start == utc_dt(2026, 7, 13)


def test_competition_tracker_snapshot_contains_current_metrics():
    tracker = CompetitionTracker(now=utc_dt(2026, 7, 13))

    tracker.set_pair_boost(
        symbol="SOMI:USDso",
        boost=Decimal("1.5"),
    )

    tracker.record_trade(
        symbol="SOMI:USDso",
        notional=Decimal("100"),
        timestamp=utc_dt(2026, 7, 13),
    )

    snapshot = tracker.snapshot()

    assert snapshot.week_start == utc_dt(2026, 7, 13)
    assert snapshot.weekly_volume == Decimal("100")
    assert snapshot.estimated_score == Decimal("150.0")
    assert snapshot.raffle_tickets == 0
    assert snapshot.pair_volumes["SOMI:USDso"] == Decimal("100")
    assert snapshot.pair_boosts["SOMI:USDso"] == Decimal("1.5")


def test_competition_tracker_rejects_invalid_trade():
    tracker = CompetitionTracker(now=utc_dt(2026, 7, 13))

    with pytest.raises(ValueError):
        tracker.record_trade(
            symbol="SOMI:USDso",
            notional=Decimal("0"),
            timestamp=utc_dt(2026, 7, 13),
        )


def test_competition_tracker_rejects_invalid_boost():
    tracker = CompetitionTracker(now=utc_dt(2026, 7, 13))

    with pytest.raises(ValueError):
        tracker.set_pair_boost(
            symbol="SOMI:USDso",
            boost=Decimal("0"),
        )


def test_competition_tracker_supports_custom_config():
    config = CompetitionConfig(
        raffle_volume_per_ticket=Decimal("100"),
        max_raffle_tickets=3,
    )

    tracker = CompetitionTracker(
        config=config,
        now=utc_dt(2026, 7, 13),
    )

    tracker.record_trade(
        symbol="SOMI:USDso",
        notional=Decimal("1000"),
        timestamp=utc_dt(2026, 7, 13),
    )

    assert tracker.raffle_tickets == 3