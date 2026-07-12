import json
import socket
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from bot.analytics.depth_outcome_analyzer import (
    INCLUSIVE_SIGN_PAIRS,
    REGIMES,
    DepthOutcomeAnalyzer,
    classify_regime,
    classify_inclusive_sign_pair,
    l5_magnitude_bucket,
)
from bot.portfolio.portfolio_manager import PortfolioManager


START = datetime(2026, 7, 12, tzinfo=timezone.utc)


def record(index, mid, l1, l5, *, l2=None, l3=None, l10=None, ok=True):
    return {
        "timestamp": (START + timedelta(seconds=index)).isoformat(),
        "iteration_ok": ok,
        "mid_price": str(mid),
        "depth_imbalance_l1": str(l1),
        "depth_imbalance_l2": None if l2 is None else str(l2),
        "depth_imbalance_l3": None if l3 is None else str(l3),
        "depth_imbalance_l5": str(l5),
        "depth_imbalance_l10": None if l10 is None else str(l10),
        "ask_depth_concentration_l2_to_l5": "0.5",
        "bid_depth_concentration_l2_to_l5": "0.5",
    }


def write_jsonl(path, records):
    path.write_text("\n".join(json.dumps(item) for item in records) + "\n", encoding="utf-8")


def test_regime_classification_and_l5_buckets():
    assert classify_regime(record(0, 100, "0.1", "-0.3")) == "L1_POSITIVE_L5_NEGATIVE"
    assert classify_regime(record(0, 100, "-0.1", "-0.3")) == "L1_NEGATIVE_L5_NEGATIVE"
    assert classify_regime(record(0, 100, "0.1", "0.3")) == "L1_POSITIVE_L5_POSITIVE"
    assert classify_regime(record(0, 100, "-0.1", "0.3")) == "L1_NEGATIVE_L5_POSITIVE"
    assert classify_regime(record(0, 100, "0.1", "0.3", l2="0.2", l3="0.1", l10="0.4")) == "SAME_SIGN_POSITIVE"
    assert classify_regime(record(0, 100, "-0.1", "-0.3", l2="-0.2", l3="-0.1", l10="-0.4")) == "SAME_SIGN_NEGATIVE"
    assert classify_regime(record(0, 100, "0", "0")) == "UNKNOWN"
    assert classify_inclusive_sign_pair(record(0, 100, "0.1", "-0.3", l2="0.2", l3="0.2", l10="0.2")) == "L1_POSITIVE_L5_NEGATIVE"
    assert classify_inclusive_sign_pair(record(0, 100, "-0.1", "-0.3", l2="0.2", l3="0.2", l10="0.2")) == "L1_NEGATIVE_L5_NEGATIVE"
    assert classify_inclusive_sign_pair(record(0, 100, "0", "-0.3")) == "L1_OR_L5_ZERO"
    assert l5_magnitude_bucket(Decimal("0.19")) == "mild"
    assert l5_magnitude_bucket(Decimal("0.20")) == "medium"
    assert l5_magnitude_bucket(Decimal("0.399")) == "medium"
    assert l5_magnitude_bucket(Decimal("0.40")) == "strong"


def test_forward_returns_horizons_and_excursions(tmp_path):
    path = tmp_path / "run.jsonl"
    records = [
        record(0, "100", "0.1", "-0.3"),
        record(1, "101", "0.1", "-0.3"),
        record(2, "99", "0.1", "-0.3"),
        record(3, "102", "0.1", "-0.3"),
        record(4, "100", "0.1", "-0.3"),
    ]
    write_jsonl(path, records)
    analysis = DepthOutcomeAnalyzer().analyze_files([path], horizons=(1, 3))
    horizon1 = analysis.metrics_for("L1_POSITIVE_L5_NEGATIVE", 1)
    assert horizon1.observation_count == 4
    assert horizon1.positive_return_count == 2
    assert horizon1.negative_return_count == 2
    assert horizon1.zero_return_count == 0
    assert horizon1.average_forward_return_bps > 0
    assert horizon1.minimum_forward_return_bps < 0
    assert horizon1.maximum_forward_return_bps > 0
    assert horizon1.maximum_favorable_excursion_bps > 0
    assert horizon1.maximum_adverse_excursion_bps > 0
    assert horizon1.zero_return_rate == Decimal("0")
    assert horizon1.nonzero_observation_count == 4
    assert horizon1.average_nonzero_forward_return_bps == horizon1.average_forward_return_bps
    assert horizon1.median_nonzero_forward_return_bps == horizon1.median_forward_return_bps
    assert analysis.metrics_for("L1_POSITIVE_L5_NEGATIVE", 3).observation_count == 2
    assert analysis.observations[0].elapsed_seconds == Decimal("1.0")


def test_file_boundaries_failed_and_incomplete_records_are_ignored(tmp_path):
    first = tmp_path / "first.jsonl"
    second = tmp_path / "second.jsonl"
    first_records = [
        record(0, "100", "0.1", "-0.3"),
        record(1, "101", "0.1", "-0.3"),
        record(2, "102", "0.1", "-0.3"),
        record(3, "103", "0.1", "-0.3", ok=False),
        {"timestamp": START.isoformat(), "mid_price": "100"},
    ]
    second_records = [record(0, "200", "0.1", "-0.3")]
    write_jsonl(first, first_records)
    write_jsonl(second, second_records)
    analysis = DepthOutcomeAnalyzer().analyze_files([first, second], horizons=(3,))
    assert analysis.raw_record_count == 6
    assert analysis.valid_record_count == 4
    assert analysis.skipped_record_count == 2
    assert len(analysis.observations) == 0


def test_regime_counts_comparison_and_old_json_compatibility(tmp_path, monkeypatch):
    monkeypatch.setattr(socket, "create_connection", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("offline only")))
    path = tmp_path / "mixed.jsonl"
    records = [
        record(0, 100, "0.1", "-0.3"),
        record(1, 101, "-0.1", "-0.3"),
        record(2, 102, "0.1", "0.3"),
        record(3, 103, "-0.1", "0.3"),
        record(4, 104, "0.1", "0.3", l2="0.2", l3="0.1", l10="0.4"),
        record(5, 105, "-0.1", "-0.3", l2="-0.2", l3="-0.1", l10="-0.4"),
        record(6, 106, "0", "0"),
    ]
    write_jsonl(path, records)
    analysis = DepthOutcomeAnalyzer().analyze_files([path], horizons=(1,))
    assert analysis.regime_counts["L1_POSITIVE_L5_NEGATIVE"] == 1
    assert analysis.regime_counts["L1_NEGATIVE_L5_NEGATIVE"] == 1
    assert analysis.regime_counts["L1_POSITIVE_L5_POSITIVE"] == 1
    assert analysis.regime_counts["L1_NEGATIVE_L5_POSITIVE"] == 1
    assert analysis.regime_counts["SAME_SIGN_POSITIVE"] == 1
    assert analysis.regime_counts["SAME_SIGN_NEGATIVE"] == 1
    assert analysis.regime_counts["UNKNOWN"] == 1
    assert analysis.inclusive_sign_pair_counts["L1_NEGATIVE_L5_NEGATIVE"] == 2
    assert analysis.regime_counts["L1_NEGATIVE_L5_NEGATIVE"] == 1
    assert analysis.inclusive_metrics_for("L1_NEGATIVE_L5_NEGATIVE", 1).observation_count == 2
    assert len(analysis.per_file_inclusive_metrics) == 1
    assert len(analysis.per_file_inclusive_metrics[0].metrics) == len(INCLUSIVE_SIGN_PAIRS)
    comparison = analysis.comparisons[0]
    assert comparison.positive_l1_negative_l5.observation_count == 1
    assert comparison.negative_l1_negative_l5.observation_count == 1
    inclusive_comparison = analysis.inclusive_comparisons[0]
    assert inclusive_comparison.negative_l1_negative_l5.observation_count == 2
    assert inclusive_comparison.average_return_difference_bps is not None

    same_prices = tmp_path / "same.jsonl"
    write_jsonl(same_prices, [record(index, 100, "0.1", "-0.3") for index in range(4)])
    same_analysis = DepthOutcomeAnalyzer().analyze_files([same_prices], horizons=(1,))
    same_metrics = same_analysis.inclusive_metrics_for("L1_POSITIVE_L5_NEGATIVE", 1)
    assert same_metrics.zero_return_rate == Decimal("1")
    assert same_metrics.nonzero_observation_count == 0
    assert same_metrics.average_nonzero_forward_return_bps is None
    assert same_metrics.median_nonzero_forward_return_bps is None

    old = tmp_path / "old.jsonl"
    write_jsonl(old, [{"timestamp": START.isoformat(), "mid_price": "100"}])
    old_analysis = DepthOutcomeAnalyzer().analyze_files([old])
    assert old_analysis.valid_record_count == 0
    assert old_analysis.skipped_record_count == 1
    assert all(old_analysis.regime_counts[regime] == 0 for regime in REGIMES)


def test_analysis_does_not_mutate_portfolio_state(tmp_path):
    path = tmp_path / "run.jsonl"
    write_jsonl(path, [record(0, 100, "0.1", "-0.3"), record(1, 101, "0.1", "-0.3")])
    portfolio = PortfolioManager(initial_cash=Decimal("150"))
    before = (portfolio.cash_balance, portfolio.base_position, portfolio.total_volume)
    DepthOutcomeAnalyzer().analyze_files([path])
    assert (portfolio.cash_balance, portfolio.base_position, portfolio.total_volume) == before
