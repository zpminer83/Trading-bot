import json
import socket
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from bot.analytics.signal_threshold_study import (
    CandidateDevelopmentEvaluation,
    DirectionalThresholdMetrics,
    SignalThresholdCandidate,
    SignalThresholdStudy,
    SignalThresholdStudyConfig,
    _StudyRecord,
)
from bot.portfolio.portfolio_manager import PortfolioManager


START = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)


def config(**overrides) -> SignalThresholdStudyConfig:
    values = {
        "imbalance_thresholds": (Decimal("0.1"),),
        "microprice_edge_thresholds_bps": (Decimal("0.5"),),
        "momentum_thresholds_bps": (Decimal("0.5"),),
        "maximum_spread_thresholds_bps": (Decimal("20"),),
        "horizons": (1,),
        "split_percentages": (60, 20, 20),
        "minimum_training_samples": 1,
        "minimum_validation_samples": 1,
        "top_per_interpretation": 1,
    }
    values.update(overrides)
    return SignalThresholdStudyConfig(**values)


def candidate(interpretation="aligned", horizon=1) -> SignalThresholdCandidate:
    return SignalThresholdCandidate(
        interpretation=interpretation,
        imbalance_threshold=Decimal("0.1"),
        microprice_edge_threshold_bps=Decimal("0.5"),
        momentum_threshold_bps=Decimal("0.5"),
        maximum_spread_bps=Decimal("20"),
        horizon_records=horizon,
    )


def study_record(
    source: Path,
    index: int,
    mid: str,
    metric_sign: int,
) -> _StudyRecord:
    sign = Decimal(metric_sign)
    return _StudyRecord(
        source_file=source,
        timestamp=START + timedelta(seconds=index * 10),
        symbol="SOMI:USDso",
        mid_price=Decimal(mid),
        spread_bps=Decimal("10"),
        depth_imbalance=sign * Decimal("0.2"),
        microprice_edge_bps=sign * Decimal("1"),
        rolling_momentum_bps=sign * Decimal("1"),
    )


def json_record(index: int, mid: str, metric_sign: int, *, ok=True) -> dict:
    sign = Decimal(metric_sign)
    return {
        "timestamp": (START + timedelta(seconds=index * 10)).isoformat(),
        "symbol": "SOMI:USDso",
        "iteration_ok": ok,
        "mid_price": mid,
        "signal_spread_bps": "10",
        "signal_depth_imbalance": str(sign * Decimal("0.2")),
        "signal_microprice_edge_bps": str(sign),
        "signal_rolling_momentum_bps": str(sign),
    }


def write_jsonl(path: Path, records: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as file:
        for record in records:
            file.write(json.dumps(record))
            file.write("\n")


def test_candidate_grid_and_directional_classification():
    study = SignalThresholdStudy(config(horizons=(1, 3)))
    candidates = study.generate_candidates()
    assert len(candidates) == 4
    aligned = candidate("aligned")
    contrarian = candidate("contrarian")
    positive = dict(
        spread_bps=Decimal("10"),
        depth_imbalance=Decimal("0.2"),
        microprice_edge_bps=Decimal("1"),
        rolling_momentum_bps=Decimal("1"),
    )
    negative = {key: -value for key, value in positive.items() if key != "spread_bps"}
    negative["spread_bps"] = Decimal("10")
    assert study.classify(aligned, **positive) == "bullish"
    assert study.classify(aligned, **negative) == "bearish"
    assert study.classify(contrarian, **positive) == "bearish"
    assert study.classify(contrarian, **negative) == "bullish"


def test_chronological_split_and_development_never_reads_test_metrics(monkeypatch):
    study = SignalThresholdStudy(config())
    values = list(range(10))
    split = study.chronological_split(values)  # type: ignore[arg-type]
    assert split["training"] == list(range(6))
    assert split["validation"] == [6, 7]
    assert split["test"] == [8, 9]

    calls = []
    original = study._calculate_direction_metrics

    def track(candidate_value, direction, split_name, records_by_file):
        calls.append(split_name)
        return original(candidate_value, direction, split_name, records_by_file)

    monkeypatch.setattr(study, "_calculate_direction_metrics", track)
    empty = {"training": {}, "validation": {}, "test": {}}
    study._evaluate_development(candidate(), empty)
    assert set(calls) == {"training", "validation"}


def test_coverage_hits_excursions_and_file_consistency():
    study = SignalThresholdStudy(config(horizons=(2,)))
    first = Path("first.jsonl")
    second = Path("second.jsonl")
    records = {
        first: [
            study_record(first, 0, "100", 1),
            study_record(first, 1, "99", 0),
            study_record(first, 2, "102", 0),
        ],
        second: [
            study_record(second, 0, "200", 1),
            study_record(second, 1, "198", 0),
            study_record(second, 2, "204", 0),
        ],
    }
    metrics = study._calculate_direction_metrics(
        candidate(horizon=2), "bullish", "validation", records
    )
    assert metrics.observation_count == 2
    assert metrics.available_outcome_count == 2
    assert metrics.coverage_ratio == Decimal("1")
    assert metrics.directional_hit_rate == Decimal("1")
    assert metrics.average_favorable_excursion_bps == Decimal("200")
    assert metrics.average_adverse_excursion_bps == Decimal("100")
    assert metrics.favorable_adverse_excursion_ratio == Decimal("2")
    assert metrics.file_consistency_ratio == Decimal("1")


def test_forward_outcomes_never_cross_files_or_split_boundaries():
    study = SignalThresholdStudy(config())
    first = Path("first.jsonl")
    second = Path("second.jsonl")
    metrics = study._calculate_direction_metrics(
        candidate(),
        "bullish",
        "training",
        {
            first: [study_record(first, 0, "100", 1)],
            second: [study_record(second, 0, "200", 0)],
        },
    )
    assert metrics.available_outcome_count == 0
    assert metrics.observation_count == 0


def empty_metrics(direction: str, *, count=20, average="1", hit="0.6", files=2):
    return DirectionalThresholdMetrics(
        split_name="validation",
        direction=direction,
        horizon_records=1,
        observation_count=count,
        available_outcome_count=count,
        coverage_ratio=Decimal("1"),
        average_forward_return_bps=Decimal(average),
        median_forward_return_bps=Decimal(average),
        directional_hit_rate=Decimal(hit),
        average_favorable_excursion_bps=Decimal("2"),
        average_adverse_excursion_bps=Decimal("1"),
        favorable_adverse_excursion_ratio=Decimal("2"),
        standard_deviation_forward_return_bps=Decimal("1"),
        contributing_file_count=files,
        file_consistency_ratio=Decimal("1") if files > 1 else Decimal("0"),
        results_by_file=(),
    )


def test_minimum_samples_and_held_out_validation_reasons():
    study = SignalThresholdStudy(config(minimum_training_samples=2, minimum_validation_samples=2))
    development = CandidateDevelopmentEvaluation(
        candidate=candidate(),
        training_metrics=(empty_metrics("bullish", count=1), empty_metrics("bearish", count=1, average="-1")),
        validation_metrics=(empty_metrics("bullish", count=1), empty_metrics("bearish", count=1, average="-1")),
        eligible=False,
        ranking_score=(),
    )
    assert study.select_candidates([development]) == ()

    source = Path("only.jsonl")
    test_records = {
        source: [
            study_record(source, 0, "100", 1),
            study_record(source, 1, "99", -1),
            study_record(source, 2, "100", 0),
        ]
    }
    selected = study._evaluate_selected_on_test(development, test_records)
    assert selected.validation_status == "NOT VALIDATED"
    assert any("sample_too_small" in reason for reason in selected.validation_reasons)
    assert any("wrong_average_return_sign" in reason for reason in selected.validation_reasons)
    assert any("unstable_across_files" in reason for reason in selected.validation_reasons)


def test_json_export_missing_fields_and_no_trading_mutation(tmp_path, monkeypatch):
    first = tmp_path / "first.jsonl"
    second = tmp_path / "second.jsonl"
    records = []
    for index in range(20):
        sign = 1 if index % 2 == 0 else -1
        mid = Decimal("100") + Decimal(index % 3)
        records.append(json_record(index, str(mid), sign))
    records.append({"timestamp": START.isoformat(), "mid_price": "100"})
    records.append(json_record(21, "100", 1, ok=False))
    write_jsonl(first, records)
    write_jsonl(second, records[:-2])

    portfolio = PortfolioManager(initial_cash=Decimal("150"))
    before = (portfolio.cash_balance, portfolio.base_position, portfolio.total_volume)
    monkeypatch.setattr(
        socket,
        "create_connection",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("threshold study must remain offline")
        ),
    )
    study = SignalThresholdStudy(config())
    result = study.analyze_files([first, second])
    output = study.export_json(result, tmp_path / "nested" / "study.json")
    exported = json.loads(output.read_text(encoding="utf-8"))
    assert exported["candidate_combination_count"] == 2
    assert exported["skipped_record_count"] == 2
    assert isinstance(exported["selected_candidates"], list)
    assert (portfolio.cash_balance, portfolio.base_position, portfolio.total_volume) == before


@pytest.mark.parametrize(
    "overrides",
    [
        {"split_percentages": (60, 40, 10)},
        {"split_percentages": (100, 0, 0)},
        {"horizons": (0,)},
        {"minimum_training_samples": 0},
        {"minimum_validation_samples": 0},
        {"top_per_interpretation": 0},
        {"imbalance_thresholds": (Decimal("-1"),)},
    ],
)
def test_invalid_configuration_is_rejected(overrides):
    with pytest.raises(ValueError):
        config(**overrides)
