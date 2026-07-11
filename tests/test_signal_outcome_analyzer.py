import argparse
import json
import socket
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from bot.analytics.signal_outcome_analyzer import SignalOutcomeAnalyzer
from bot.portfolio.portfolio_manager import PortfolioManager
from scripts.analyze_signal_outcomes import parse_horizons, print_analysis


START = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)


def record(
    index: int,
    mid: str | None,
    state: str | None,
    *,
    confidence: str | None = "0.5",
    iteration_ok=True,
) -> dict:
    return {
        "timestamp": (START + timedelta(seconds=index * 10)).isoformat(),
        "symbol": "SOMI:USDso",
        "iteration_ok": iteration_ok,
        "mid_price": mid,
        "signal_state": state,
        "signal_reason": f"{state}_reason" if state else None,
        "signal_confidence": confidence,
        "signal_depth_imbalance": "0.25",
        "signal_microprice_edge_bps": "2",
        "signal_rolling_momentum_bps": "3",
    }


def write_jsonl(path, records) -> None:
    with path.open("w", encoding="utf-8") as file:
        for item in records:
            file.write(json.dumps(item))
            file.write("\n")


def test_forward_returns_horizons_elapsed_hits_misses_and_median(tmp_path):
    path = tmp_path / "signals.jsonl"
    write_jsonl(
        path,
        [
            record(0, "100", "bullish", confidence="0.49"),
            record(1, "101", "bullish", confidence="0.60"),
            record(2, "99", "bearish", confidence="0.80"),
            record(3, "102", "bearish", confidence="0.80"),
            record(4, "100", "neutral"),
        ],
    )

    analysis = SignalOutcomeAnalyzer().analyze_files([path], horizons=(1, 3))
    bullish_one = analysis.stats_for("bullish", 1)
    bearish_one = analysis.stats_for("bearish", 1)
    neutral_one = analysis.stats_for("neutral", 1)

    expected_first = Decimal("100")
    expected_second = (Decimal("99") - Decimal("101")) / Decimal("101") * 10000
    assert bullish_one.observation_count == 2
    assert bullish_one.average_forward_return_bps == (
        expected_first + expected_second
    ) / 2
    assert bullish_one.median_forward_return_bps == (
        expected_first + expected_second
    ) / 2
    assert bullish_one.directional_hit_count == 1
    assert bullish_one.directional_miss_count == 1
    assert bullish_one.directional_hit_rate == Decimal("0.5")
    assert bearish_one.directional_hit_count == 1
    assert bearish_one.directional_miss_count == 1
    assert neutral_one.directional_hit_rate is None

    first_horizon_three = next(
        item
        for item in analysis.observations
        if item.timestamp == START and item.horizon_records == 3
    )
    assert first_horizon_three.forward_return_bps == Decimal("200")
    assert first_horizon_three.elapsed_seconds == Decimal("30.0")


def test_bullish_and_bearish_favorable_adverse_excursions(tmp_path):
    bullish_path = tmp_path / "bullish.jsonl"
    write_jsonl(
        bullish_path,
        [
            record(0, "100", "bullish"),
            record(1, "90", "neutral"),
            record(2, "110", "neutral"),
        ],
    )
    bullish = SignalOutcomeAnalyzer().analyze_files([bullish_path], horizons=(2,))
    bullish_observation = bullish.observations[0]
    assert bullish_observation.maximum_favorable_excursion_bps == Decimal("1000")
    assert bullish_observation.maximum_adverse_excursion_bps == Decimal("-1000")
    bullish_stats = bullish.stats_for("bullish", 2)
    assert bullish_stats.average_favorable_excursion_bps == Decimal("1000")
    assert bullish_stats.median_adverse_excursion_bps == Decimal("-1000")

    bearish_path = tmp_path / "bearish.jsonl"
    write_jsonl(
        bearish_path,
        [
            record(0, "100", "bearish"),
            record(1, "110", "neutral"),
            record(2, "90", "neutral"),
        ],
    )
    bearish = SignalOutcomeAnalyzer().analyze_files([bearish_path], horizons=(2,))
    bearish_observation = bearish.observations[0]
    assert bearish_observation.maximum_favorable_excursion_bps == Decimal("1000")
    assert bearish_observation.maximum_adverse_excursion_bps == Decimal("1000")


def test_confidence_buckets_and_console_warnings(tmp_path, capsys):
    path = tmp_path / "confidence.jsonl"
    write_jsonl(
        path,
        [
            record(0, "100", "bullish", confidence="0.49"),
            record(1, "101", "bullish", confidence="0.50"),
            record(2, "102", "bearish", confidence="0.75"),
            record(3, "101", "neutral"),
        ],
    )
    analysis = SignalOutcomeAnalyzer().analyze_files([path], horizons=(1,))
    populated = {
        (item.state, item.confidence_bucket): item
        for item in analysis.confidence_bucket_stats
        if item.observation_count
    }
    assert populated[("bullish", "low")].observation_count == 1
    assert populated[("bullish", "medium")].observation_count == 1
    assert populated[("bearish", "high")].directional_hit_rate == Decimal("1")

    print_analysis(analysis)
    output = capsys.readouterr().out
    assert "record horizons are not guaranteed wall-clock durations" in output
    assert "Signal confidence is uncalibrated" in output
    assert "not independent observations" in output


def test_skips_failed_missing_unknown_and_insufficient_records(tmp_path):
    path = tmp_path / "skips.jsonl"
    write_jsonl(
        path,
        [
            record(0, "100", "bullish"),
            record(1, "101", "bullish", iteration_ok=False),
            record(2, None, "bearish"),
            record(3, "102", None),
            record(4, "103", "neutral"),
            record(5, "not-a-number", "bullish"),
        ],
    )
    analysis = SignalOutcomeAnalyzer().analyze_files([path], horizons=(1, 6))
    assert analysis.raw_record_count == 6
    assert analysis.valid_record_count == 2
    assert analysis.skipped_record_count == 4
    assert all(item.horizon_records == 1 for item in analysis.observations)
    assert analysis.stats_for("neutral", 6).observation_count == 0


def test_old_jsonl_multiple_files_do_not_cross_boundaries(tmp_path):
    first = tmp_path / "first.jsonl"
    second = tmp_path / "second.jsonl"
    write_jsonl(first, [record(0, "100", "bullish"), record(1, "101", "neutral")])
    write_jsonl(second, [record(0, "200", "bearish"), record(1, "198", "neutral")])
    analysis = SignalOutcomeAnalyzer().analyze_files([first, second], horizons=(1,))
    assert len(analysis.observations) == 2
    assert analysis.stats_for("bullish", 1).directional_hit_rate == Decimal("1")
    assert analysis.stats_for("bearish", 1).directional_hit_rate == Decimal("1")

    old = tmp_path / "old.jsonl"
    write_jsonl(old, [{"timestamp": START.isoformat(), "mid_price": "100"}])
    old_analysis = SignalOutcomeAnalyzer().analyze_files([old], horizons=(1,))
    assert old_analysis.valid_record_count == 0
    assert old_analysis.skipped_record_count == 1


@pytest.mark.parametrize("horizons", [(), (0,), (-1,), (1.5,), (True,)])
def test_invalid_horizons_are_rejected(horizons):
    with pytest.raises(ValueError, match="horizon"):
        SignalOutcomeAnalyzer.validate_horizons(horizons)

    if horizons:
        with pytest.raises(argparse.ArgumentTypeError):
            parse_horizons(",".join(str(value) for value in horizons))


def test_analyzer_is_offline_and_does_not_mutate_trading_state(tmp_path, monkeypatch):
    path = tmp_path / "offline.jsonl"
    write_jsonl(path, [record(0, "100", "bullish"), record(1, "101", "neutral")])
    portfolio = PortfolioManager(initial_cash=Decimal("150"))
    before = (portfolio.cash_balance, portfolio.base_position, portfolio.total_volume)
    monkeypatch.setattr(
        socket,
        "create_connection",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("outcome analysis must not make network calls")
        ),
    )
    SignalOutcomeAnalyzer().analyze_files([path], horizons=(1,))
    assert (portfolio.cash_balance, portfolio.base_position, portfolio.total_volume) == before
