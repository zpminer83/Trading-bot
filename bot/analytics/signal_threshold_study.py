from __future__ import annotations

import json
from dataclasses import asdict, dataclass, is_dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from itertools import product
from pathlib import Path
from typing import Any, Iterable


DIRECTIONS = ("bullish", "bearish")
INTERPRETATIONS = ("aligned", "contrarian")
SPLIT_NAMES = ("training", "validation", "test")


@dataclass(frozen=True)
class SignalThresholdStudyConfig:
    imbalance_thresholds: tuple[Decimal, ...] = (
        Decimal("0.05"), Decimal("0.10"), Decimal("0.15"),
        Decimal("0.20"), Decimal("0.30"),
    )
    microprice_edge_thresholds_bps: tuple[Decimal, ...] = (
        Decimal("0.25"), Decimal("0.50"), Decimal("1.00"),
        Decimal("1.50"), Decimal("2.00"),
    )
    momentum_thresholds_bps: tuple[Decimal, ...] = (
        Decimal("0.25"), Decimal("0.50"), Decimal("1.00"),
        Decimal("2.00"), Decimal("3.00"),
    )
    maximum_spread_thresholds_bps: tuple[Decimal, ...] = (
        Decimal("10"), Decimal("20"), Decimal("30"), Decimal("50"),
    )
    horizons: tuple[int, ...] = (1, 3, 6, 12)
    split_percentages: tuple[int, int, int] = (60, 20, 20)
    minimum_training_samples: int = 30
    minimum_validation_samples: int = 10
    top_per_interpretation: int = 5

    def __post_init__(self) -> None:
        for field_name in (
            "imbalance_thresholds",
            "microprice_edge_thresholds_bps",
            "momentum_thresholds_bps",
            "maximum_spread_thresholds_bps",
        ):
            values = tuple(Decimal(str(value)) for value in getattr(self, field_name))
            if not values or any(not value.is_finite() or value < 0 for value in values):
                raise ValueError(f"{field_name} must contain non-negative finite values")
            if field_name == "imbalance_thresholds" and any(value > 1 for value in values):
                raise ValueError("imbalance_thresholds must not exceed 1")
            object.__setattr__(self, field_name, values)
        if any(isinstance(value, bool) for value in self.horizons):
            raise ValueError("horizons must contain positive integers")
        horizons = tuple(int(value) for value in self.horizons)
        if (
            not horizons
            or any(value < 1 for value in horizons)
            or any(horizon != original for horizon, original in zip(horizons, self.horizons))
        ):
            raise ValueError("horizons must contain positive integers")
        object.__setattr__(self, "horizons", tuple(sorted(set(horizons))))
        if (
            len(self.split_percentages) != 3
            or any(value <= 0 for value in self.split_percentages)
            or sum(self.split_percentages) != 100
        ):
            raise ValueError("split percentages must be three positive values totaling 100")
        if self.minimum_training_samples < 1:
            raise ValueError("minimum_training_samples must be >= 1")
        if self.minimum_validation_samples < 1:
            raise ValueError("minimum_validation_samples must be >= 1")
        if self.top_per_interpretation < 1:
            raise ValueError("top_per_interpretation must be >= 1")


@dataclass(frozen=True)
class SignalThresholdCandidate:
    interpretation: str
    imbalance_threshold: Decimal
    microprice_edge_threshold_bps: Decimal
    momentum_threshold_bps: Decimal
    maximum_spread_bps: Decimal
    horizon_records: int


@dataclass(frozen=True)
class FileDirectionalMetrics:
    source_file: Path
    observation_count: int
    average_forward_return_bps: Decimal | None
    directional_hit_rate: Decimal | None
    directionally_correct: bool


@dataclass(frozen=True)
class DirectionalThresholdMetrics:
    split_name: str
    direction: str
    horizon_records: int
    observation_count: int
    available_outcome_count: int
    coverage_ratio: Decimal
    average_forward_return_bps: Decimal | None
    median_forward_return_bps: Decimal | None
    directional_hit_rate: Decimal | None
    average_favorable_excursion_bps: Decimal | None
    average_adverse_excursion_bps: Decimal | None
    favorable_adverse_excursion_ratio: Decimal | None
    standard_deviation_forward_return_bps: Decimal | None
    contributing_file_count: int
    file_consistency_ratio: Decimal
    results_by_file: tuple[FileDirectionalMetrics, ...]


@dataclass(frozen=True)
class CandidateDevelopmentEvaluation:
    candidate: SignalThresholdCandidate
    training_metrics: tuple[DirectionalThresholdMetrics, ...]
    validation_metrics: tuple[DirectionalThresholdMetrics, ...]
    eligible: bool
    ranking_score: tuple[Decimal, ...]
    horizon_consistency_ratio: Decimal = Decimal("0")

    def metrics_for(self, split_name: str, direction: str) -> DirectionalThresholdMetrics:
        metrics = (
            self.training_metrics if split_name == "training" else self.validation_metrics
        )
        return next(item for item in metrics if item.direction == direction)


@dataclass(frozen=True)
class SelectedCandidateResult:
    candidate: SignalThresholdCandidate
    training_metrics: tuple[DirectionalThresholdMetrics, ...]
    validation_metrics: tuple[DirectionalThresholdMetrics, ...]
    test_metrics: tuple[DirectionalThresholdMetrics, ...]
    validation_status: str
    validation_reasons: tuple[str, ...]


@dataclass(frozen=True)
class StudySplitSizes:
    training: int
    validation: int
    test: int


@dataclass(frozen=True)
class SignalThresholdStudyResult:
    files: tuple[Path, ...]
    valid_record_count: int
    skipped_record_count: int
    split_sizes: StudySplitSizes
    candidate_combination_count: int
    eligible_candidate_count: int
    selected_candidates: tuple[SelectedCandidateResult, ...]


@dataclass(frozen=True)
class _StudyRecord:
    source_file: Path
    timestamp: datetime
    symbol: str
    mid_price: Decimal
    spread_bps: Decimal
    depth_imbalance: Decimal
    microprice_edge_bps: Decimal
    rolling_momentum_bps: Decimal


class SignalThresholdStudy:
    def __init__(self, config: SignalThresholdStudyConfig | None = None) -> None:
        self.config = config or SignalThresholdStudyConfig()

    def generate_candidates(self) -> tuple[SignalThresholdCandidate, ...]:
        return tuple(
            SignalThresholdCandidate(
                interpretation=interpretation,
                imbalance_threshold=imbalance,
                microprice_edge_threshold_bps=edge,
                momentum_threshold_bps=momentum,
                maximum_spread_bps=spread,
                horizon_records=horizon,
            )
            for interpretation, imbalance, edge, momentum, spread, horizon in product(
                INTERPRETATIONS,
                self.config.imbalance_thresholds,
                self.config.microprice_edge_thresholds_bps,
                self.config.momentum_thresholds_bps,
                self.config.maximum_spread_thresholds_bps,
                self.config.horizons,
            )
        )

    def classify(
        self,
        candidate: SignalThresholdCandidate,
        *,
        spread_bps: Decimal,
        depth_imbalance: Decimal,
        microprice_edge_bps: Decimal,
        rolling_momentum_bps: Decimal,
    ) -> str | None:
        if spread_bps > candidate.maximum_spread_bps:
            return None
        positive = (
            depth_imbalance >= candidate.imbalance_threshold
            and microprice_edge_bps >= candidate.microprice_edge_threshold_bps
            and rolling_momentum_bps >= candidate.momentum_threshold_bps
        )
        negative = (
            depth_imbalance <= -candidate.imbalance_threshold
            and microprice_edge_bps <= -candidate.microprice_edge_threshold_bps
            and rolling_momentum_bps <= -candidate.momentum_threshold_bps
        )
        if positive == negative:
            return None
        aligned = "bullish" if positive else "bearish"
        if candidate.interpretation == "aligned":
            return aligned
        return "bearish" if aligned == "bullish" else "bullish"

    def analyze_files(self, paths: Iterable[str | Path]) -> SignalThresholdStudyResult:
        files = tuple(Path(path) for path in paths)
        if not files:
            raise ValueError("at least one JSONL file is required")
        split_records: dict[str, dict[Path, list[_StudyRecord]]] = {
            name: {} for name in SPLIT_NAMES
        }
        valid_count = 0
        skipped_count = 0
        for path in files:
            raw_records = self._load_jsonl(path)
            valid_records: list[_StudyRecord] = []
            for record in raw_records:
                parsed = self._parse_record(record, path)
                if parsed is None:
                    skipped_count += 1
                else:
                    valid_count += 1
                    valid_records.append(parsed)
            valid_records.sort(key=lambda item: item.timestamp)
            file_splits = self.chronological_split(valid_records)
            for split_name, values in file_splits.items():
                split_records[split_name][path] = values

        split_sizes = StudySplitSizes(
            training=sum(len(values) for values in split_records["training"].values()),
            validation=sum(len(values) for values in split_records["validation"].values()),
            test=sum(len(values) for values in split_records["test"].values()),
        )
        if min(split_sizes.training, split_sizes.validation, split_sizes.test) == 0:
            raise ValueError("chronological split produced an empty split")

        candidates = self.generate_candidates()
        development = tuple(
            self._evaluate_development(candidate, split_records)
            for candidate in candidates
        )
        development = self._apply_horizon_consistency(development)
        eligible = tuple(item for item in development if item.eligible)
        selected_development = self.select_candidates(eligible)
        selected = tuple(
            self._evaluate_selected_on_test(item, split_records["test"])
            for item in selected_development
        )
        return SignalThresholdStudyResult(
            files=files,
            valid_record_count=valid_count,
            skipped_record_count=skipped_count,
            split_sizes=split_sizes,
            candidate_combination_count=len(candidates),
            eligible_candidate_count=len(eligible),
            selected_candidates=selected,
        )

    def chronological_split(
        self,
        records: list[_StudyRecord],
    ) -> dict[str, list[_StudyRecord]]:
        training_pct, validation_pct, _ = self.config.split_percentages
        ordered = sorted(records, key=lambda item: getattr(item, "timestamp", item))
        count = len(ordered)
        training_end = count * training_pct // 100
        validation_end = training_end + count * validation_pct // 100
        return {
            "training": ordered[:training_end],
            "validation": ordered[training_end:validation_end],
            "test": ordered[validation_end:],
        }

    def select_candidates(
        self,
        eligible: Iterable[CandidateDevelopmentEvaluation],
    ) -> tuple[CandidateDevelopmentEvaluation, ...]:
        selected: list[CandidateDevelopmentEvaluation] = []
        values = tuple(eligible)
        for interpretation in INTERPRETATIONS:
            matching = [
                item
                for item in values
                if item.eligible and item.candidate.interpretation == interpretation
            ]
            matching.sort(key=lambda item: item.ranking_score, reverse=True)
            selected.extend(matching[: self.config.top_per_interpretation])
        return tuple(selected)

    def export_json(self, result: SignalThresholdStudyResult, path: str | Path) -> Path:
        output_path = Path(path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with output_path.open("w", encoding="utf-8") as file:
            json.dump(self._serialize(result), file, indent=2, ensure_ascii=False)
            file.write("\n")
        return output_path

    def _evaluate_development(
        self,
        candidate: SignalThresholdCandidate,
        split_records: dict[str, dict[Path, list[_StudyRecord]]],
    ) -> CandidateDevelopmentEvaluation:
        training = tuple(
            self._calculate_direction_metrics(
                candidate, direction, "training", split_records["training"]
            )
            for direction in DIRECTIONS
        )
        validation = tuple(
            self._calculate_direction_metrics(
                candidate, direction, "validation", split_records["validation"]
            )
            for direction in DIRECTIONS
        )
        eligible = all(
            item.observation_count >= self.config.minimum_training_samples
            for item in training
        ) and all(
            item.observation_count >= self.config.minimum_validation_samples
            for item in validation
        )
        return CandidateDevelopmentEvaluation(
            candidate=candidate,
            training_metrics=training,
            validation_metrics=validation,
            eligible=eligible,
            ranking_score=self._ranking_score(validation),
        )

    def _evaluate_selected_on_test(
        self,
        development: CandidateDevelopmentEvaluation,
        test_records: dict[Path, list[_StudyRecord]],
    ) -> SelectedCandidateResult:
        test_metrics = tuple(
            self._calculate_direction_metrics(
                development.candidate,
                direction,
                "test",
                test_records,
            )
            for direction in DIRECTIONS
        )
        reasons: list[str] = []
        for metrics in test_metrics:
            prefix = metrics.direction
            if metrics.observation_count < self.config.minimum_validation_samples:
                reasons.append(f"{prefix}:held_out_sample_too_small")
            if metrics.directional_hit_rate is None or metrics.directional_hit_rate <= Decimal("0.5"):
                reasons.append(f"{prefix}:hit_rate_not_above_50_percent")
            if not self._correct_sign(metrics.direction, metrics.average_forward_return_bps):
                reasons.append(f"{prefix}:wrong_average_return_sign")
            if (
                metrics.average_favorable_excursion_bps is None
                or metrics.average_adverse_excursion_bps is None
                or metrics.average_adverse_excursion_bps
                >= metrics.average_favorable_excursion_bps
            ):
                reasons.append(f"{prefix}:adverse_not_smaller_than_favorable")
            if (
                metrics.contributing_file_count < 2
                or metrics.file_consistency_ratio < Decimal("0.5")
            ):
                reasons.append(f"{prefix}:unstable_across_files")
        return SelectedCandidateResult(
            candidate=development.candidate,
            training_metrics=development.training_metrics,
            validation_metrics=development.validation_metrics,
            test_metrics=test_metrics,
            validation_status="VALIDATED" if not reasons else "NOT VALIDATED",
            validation_reasons=tuple(reasons),
        )

    def _apply_horizon_consistency(
        self,
        development: tuple[CandidateDevelopmentEvaluation, ...],
    ) -> tuple[CandidateDevelopmentEvaluation, ...]:
        families: dict[tuple[Any, ...], list[CandidateDevelopmentEvaluation]] = {}
        for item in development:
            candidate = item.candidate
            key = (
                candidate.interpretation,
                candidate.imbalance_threshold,
                candidate.microprice_edge_threshold_bps,
                candidate.momentum_threshold_bps,
                candidate.maximum_spread_bps,
            )
            families.setdefault(key, []).append(item)

        updated: list[CandidateDevelopmentEvaluation] = []
        for family in families.values():
            stable_horizons = sum(
                all(
                    metric.directional_hit_rate is not None
                    and metric.directional_hit_rate > Decimal("0.5")
                    and self._correct_sign(
                        metric.direction,
                        metric.average_forward_return_bps,
                    )
                    and metric.average_favorable_excursion_bps is not None
                    and metric.average_adverse_excursion_bps is not None
                    and metric.average_favorable_excursion_bps
                    > metric.average_adverse_excursion_bps
                    for metric in item.validation_metrics
                )
                for item in family
            )
            consistency = Decimal(stable_horizons) / Decimal(len(family))
            for item in family:
                score = (
                    item.ranking_score[:3]
                    + (consistency,)
                    + item.ranking_score[3:]
                )
                updated.append(
                    replace(
                        item,
                        ranking_score=score,
                        horizon_consistency_ratio=consistency,
                    )
                )
        return tuple(updated)

    def _calculate_direction_metrics(
        self,
        candidate: SignalThresholdCandidate,
        direction: str,
        split_name: str,
        records_by_file: dict[Path, list[_StudyRecord]],
    ) -> DirectionalThresholdMetrics:
        all_returns: list[Decimal] = []
        all_favorable: list[Decimal] = []
        all_adverse: list[Decimal] = []
        available_count = 0
        file_metrics: list[FileDirectionalMetrics] = []
        for source_file, records in records_by_file.items():
            file_returns: list[Decimal] = []
            horizon = candidate.horizon_records
            available_count += max(0, len(records) - horizon)
            for index in range(max(0, len(records) - horizon)):
                current = records[index]
                classified = self.classify(
                    candidate,
                    spread_bps=current.spread_bps,
                    depth_imbalance=current.depth_imbalance,
                    microprice_edge_bps=current.microprice_edge_bps,
                    rolling_momentum_bps=current.rolling_momentum_bps,
                )
                if classified != direction:
                    continue
                future_records = records[index + 1 : index + horizon + 1]
                returns = [
                    self._return_bps(current.mid_price, item.mid_price)
                    for item in future_records
                ]
                final_return = returns[-1]
                favorable, adverse = self._excursions(direction, returns)
                file_returns.append(final_return)
                all_returns.append(final_return)
                all_favorable.append(favorable)
                all_adverse.append(adverse)
            if file_returns:
                average = self._average(file_returns)
                hits = sum(self._is_hit(direction, value) for value in file_returns)
                file_metrics.append(
                    FileDirectionalMetrics(
                        source_file=source_file,
                        observation_count=len(file_returns),
                        average_forward_return_bps=average,
                        directional_hit_rate=Decimal(hits) / Decimal(len(file_returns)),
                        directionally_correct=self._correct_sign(direction, average),
                    )
                )
        observation_count = len(all_returns)
        average_favorable = self._average(all_favorable)
        average_adverse = self._average(all_adverse)
        hits = sum(self._is_hit(direction, value) for value in all_returns)
        consistent_files = sum(
            item.directionally_correct
            and item.directional_hit_rate is not None
            and item.directional_hit_rate > Decimal("0.5")
            for item in file_metrics
        )
        file_count = len(file_metrics)
        return DirectionalThresholdMetrics(
            split_name=split_name,
            direction=direction,
            horizon_records=candidate.horizon_records,
            observation_count=observation_count,
            available_outcome_count=available_count,
            coverage_ratio=(
                Decimal(observation_count) / Decimal(available_count)
                if available_count
                else Decimal("0")
            ),
            average_forward_return_bps=self._average(all_returns),
            median_forward_return_bps=self._median(all_returns),
            directional_hit_rate=(
                Decimal(hits) / Decimal(observation_count)
                if observation_count
                else None
            ),
            average_favorable_excursion_bps=average_favorable,
            average_adverse_excursion_bps=average_adverse,
            favorable_adverse_excursion_ratio=(
                average_favorable / average_adverse
                if average_favorable is not None
                and average_adverse is not None
                and average_adverse > 0
                else None
            ),
            standard_deviation_forward_return_bps=self._population_stddev(all_returns),
            contributing_file_count=file_count,
            file_consistency_ratio=(
                Decimal(consistent_files) / Decimal(file_count)
                if file_count > 1
                else Decimal("0")
            ),
            results_by_file=tuple(file_metrics),
        )

    def _ranking_score(
        self,
        validation: tuple[DirectionalThresholdMetrics, ...],
    ) -> tuple[Decimal, ...]:
        hit_above_half = sum(
            item.directional_hit_rate is not None
            and item.directional_hit_rate > Decimal("0.5")
            for item in validation
        )
        correct_signs = sum(
            self._correct_sign(item.direction, item.average_forward_return_bps)
            for item in validation
        )
        favorable_wins = sum(
            item.average_favorable_excursion_bps is not None
            and item.average_adverse_excursion_bps is not None
            and item.average_favorable_excursion_bps > item.average_adverse_excursion_bps
            for item in validation
        )
        average_hit_rate = self._average(
            [
                item.directional_hit_rate
                for item in validation
                if item.directional_hit_rate is not None
            ]
        ) or Decimal("0")
        minimum_consistency = min(
            (item.file_consistency_ratio for item in validation),
            default=Decimal("0"),
        )
        return (
            Decimal(hit_above_half),
            Decimal(correct_signs),
            Decimal(favorable_wins),
            minimum_consistency,
            average_hit_rate,
            Decimal(sum(item.observation_count for item in validation)),
        )

    @staticmethod
    def _correct_sign(direction: str, average: Decimal | None) -> bool:
        if average is None:
            return False
        return (direction == "bullish" and average > 0) or (
            direction == "bearish" and average < 0
        )

    @staticmethod
    def _return_bps(current: Decimal, future: Decimal) -> Decimal:
        return (future - current) / current * Decimal("10000")

    @staticmethod
    def _excursions(direction: str, returns: list[Decimal]) -> tuple[Decimal, Decimal]:
        if direction == "bullish":
            return max(Decimal("0"), max(returns)), max(Decimal("0"), -min(returns))
        return max(Decimal("0"), -min(returns)), max(Decimal("0"), max(returns))

    @staticmethod
    def _is_hit(direction: str, value: Decimal) -> bool:
        return (direction == "bullish" and value > 0) or (
            direction == "bearish" and value < 0
        )

    @staticmethod
    def _average(values: list[Decimal]) -> Decimal | None:
        return sum(values, Decimal("0")) / Decimal(len(values)) if values else None

    @staticmethod
    def _median(values: list[Decimal]) -> Decimal | None:
        if not values:
            return None
        ordered = sorted(values)
        midpoint = len(ordered) // 2
        if len(ordered) % 2:
            return ordered[midpoint]
        return (ordered[midpoint - 1] + ordered[midpoint]) / Decimal("2")

    @classmethod
    def _population_stddev(cls, values: list[Decimal]) -> Decimal | None:
        if not values:
            return None
        average = cls._average(values)
        assert average is not None
        variance = sum((value - average) ** 2 for value in values) / Decimal(len(values))
        return variance.sqrt()

    def _parse_record(self, record: dict[str, Any], source_file: Path) -> _StudyRecord | None:
        if record.get("iteration_ok") is False:
            return None
        timestamp = self._parse_timestamp(record.get("timestamp"))
        values = [
            self._decimal(record.get(name))
            for name in (
                "mid_price",
                "signal_spread_bps",
                "signal_depth_imbalance",
                "signal_microprice_edge_bps",
                "signal_rolling_momentum_bps",
            )
        ]
        if timestamp is None or any(value is None for value in values):
            return None
        mid, spread, imbalance, edge, momentum = values
        assert all(value is not None for value in values)
        if mid <= 0 or spread < 0:
            return None
        return _StudyRecord(
            source_file=source_file,
            timestamp=timestamp,
            symbol=str(record.get("symbol") or "unknown"),
            mid_price=mid,
            spread_bps=spread,
            depth_imbalance=imbalance,
            microprice_edge_bps=edge,
            rolling_momentum_bps=momentum,
        )

    @staticmethod
    def _load_jsonl(path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            raise FileNotFoundError(f"paper run file does not exist: {path}")
        records: list[dict[str, Any]] = []
        with path.open("r", encoding="utf-8") as file:
            for line_number, raw_line in enumerate(file, start=1):
                if not raw_line.strip():
                    continue
                try:
                    value = json.loads(raw_line)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"invalid JSON on line {line_number} of {path}") from exc
                if not isinstance(value, dict):
                    raise ValueError(f"record on line {line_number} of {path} must be an object")
                records.append(value)
        return records

    @staticmethod
    def _decimal(value: Any) -> Decimal | None:
        if value is None:
            return None
        try:
            parsed = Decimal(str(value))
        except (InvalidOperation, ValueError, TypeError):
            return None
        return parsed if parsed.is_finite() else None

    @staticmethod
    def _parse_timestamp(value: Any) -> datetime | None:
        if value is None:
            return None
        text = str(value).strip()
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    @classmethod
    def _serialize(cls, value: Any) -> Any:
        if is_dataclass(value):
            return cls._serialize(asdict(value))
        if isinstance(value, Decimal):
            return str(value)
        if isinstance(value, Path):
            return str(value)
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, tuple) or isinstance(value, list):
            return [cls._serialize(item) for item in value]
        if isinstance(value, dict):
            return {str(key): cls._serialize(item) for key, item in value.items()}
        return value
