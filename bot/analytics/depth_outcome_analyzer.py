from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable


REGIMES = (
    "L1_POSITIVE_L5_NEGATIVE",
    "L1_NEGATIVE_L5_NEGATIVE",
    "L1_POSITIVE_L5_POSITIVE",
    "L1_NEGATIVE_L5_POSITIVE",
    "SAME_SIGN_POSITIVE",
    "SAME_SIGN_NEGATIVE",
    "UNKNOWN",
)
L5_MAGNITUDE_BUCKETS = ("mild", "medium", "strong")
INCLUSIVE_SIGN_PAIRS = (
    "L1_POSITIVE_L5_NEGATIVE",
    "L1_NEGATIVE_L5_NEGATIVE",
    "L1_POSITIVE_L5_POSITIVE",
    "L1_NEGATIVE_L5_POSITIVE",
    "L1_OR_L5_ZERO",
    "UNKNOWN",
)


@dataclass(frozen=True)
class DepthOutcomeObservation:
    source_file: Path
    timestamp: datetime
    future_timestamp: datetime
    horizon_records: int
    regime: str
    inclusive_sign_pair: str
    l5_magnitude_bucket: str | None
    current_mid_price: Decimal
    future_mid_price: Decimal
    forward_return_bps: Decimal
    elapsed_seconds: Decimal
    maximum_favorable_excursion_bps: Decimal
    maximum_adverse_excursion_bps: Decimal


@dataclass(frozen=True)
class DepthOutcomeMetrics:
    regime: str
    horizon_records: int
    observation_count: int
    average_forward_return_bps: Decimal | None
    median_forward_return_bps: Decimal | None
    minimum_forward_return_bps: Decimal | None
    maximum_forward_return_bps: Decimal | None
    positive_return_count: int
    negative_return_count: int
    zero_return_count: int
    average_elapsed_seconds: Decimal | None
    maximum_favorable_excursion_bps: Decimal | None
    maximum_adverse_excursion_bps: Decimal | None
    average_favorable_excursion_bps: Decimal | None
    median_favorable_excursion_bps: Decimal | None
    average_adverse_excursion_bps: Decimal | None
    median_adverse_excursion_bps: Decimal | None
    zero_return_rate: Decimal
    nonzero_observation_count: int
    average_nonzero_forward_return_bps: Decimal | None
    median_nonzero_forward_return_bps: Decimal | None


@dataclass(frozen=True)
class DepthOutcomeComparison:
    horizon_records: int
    positive_l1_negative_l5: DepthOutcomeMetrics
    negative_l1_negative_l5: DepthOutcomeMetrics


@dataclass(frozen=True)
class InclusiveDepthOutcomeComparison:
    horizon_records: int
    positive_l1_negative_l5: DepthOutcomeMetrics
    negative_l1_negative_l5: DepthOutcomeMetrics
    average_return_difference_bps: Decimal | None
    median_return_difference_bps: Decimal | None
    zero_return_rate_difference: Decimal | None
    positive_return_count_difference: int
    negative_return_count_difference: int
    zero_return_count_difference: int
    average_favorable_excursion_difference_bps: Decimal | None
    median_favorable_excursion_difference_bps: Decimal | None
    average_adverse_excursion_difference_bps: Decimal | None
    median_adverse_excursion_difference_bps: Decimal | None


@dataclass(frozen=True)
class DepthOutcomeFileInclusiveMetrics:
    source_file: Path
    metrics: tuple[DepthOutcomeMetrics, ...]


@dataclass(frozen=True)
class DepthOutcomeAnalysis:
    files: tuple[Path, ...]
    raw_record_count: int
    valid_record_count: int
    skipped_record_count: int
    horizons: tuple[int, ...]
    regime_counts: dict[str, int]
    inclusive_sign_pair_counts: dict[str, int]
    observations: tuple[DepthOutcomeObservation, ...]
    metrics_by_regime: tuple[DepthOutcomeMetrics, ...]
    l5_magnitude_metrics: tuple[DepthOutcomeMetrics, ...]
    comparisons: tuple[DepthOutcomeComparison, ...]
    inclusive_sign_pair_metrics: tuple[DepthOutcomeMetrics, ...]
    inclusive_comparisons: tuple[InclusiveDepthOutcomeComparison, ...]
    per_file_inclusive_metrics: tuple[DepthOutcomeFileInclusiveMetrics, ...]

    def metrics_for(self, regime: str, horizon_records: int) -> DepthOutcomeMetrics:
        for metrics in self.metrics_by_regime:
            if metrics.regime == regime and metrics.horizon_records == horizon_records:
                return metrics
        raise KeyError(f"no metrics for {regime} at horizon {horizon_records}")

    def l5_metrics_for(self, bucket: str, horizon_records: int) -> DepthOutcomeMetrics:
        for metrics in self.l5_magnitude_metrics:
            if metrics.regime == bucket and metrics.horizon_records == horizon_records:
                return metrics
        raise KeyError(f"no L5 bucket metrics for {bucket} at horizon {horizon_records}")

    def comparison_for(self, horizon_records: int) -> DepthOutcomeComparison:
        for comparison in self.comparisons:
            if comparison.horizon_records == horizon_records:
                return comparison
        raise KeyError(f"no comparison at horizon {horizon_records}")

    def inclusive_metrics_for(self, pair: str, horizon_records: int) -> DepthOutcomeMetrics:
        for metrics in self.inclusive_sign_pair_metrics:
            if metrics.regime == pair and metrics.horizon_records == horizon_records:
                return metrics
        raise KeyError(f"no inclusive metrics for {pair} at horizon {horizon_records}")

    def inclusive_comparison_for(self, horizon_records: int) -> InclusiveDepthOutcomeComparison:
        for comparison in self.inclusive_comparisons:
            if comparison.horizon_records == horizon_records:
                return comparison
        raise KeyError(f"no inclusive comparison at horizon {horizon_records}")

    @property
    def regime_horizon_metrics(self) -> dict[tuple[str, int], DepthOutcomeMetrics]:
        return {
            (metrics.regime, metrics.horizon_records): metrics
            for metrics in self.metrics_by_regime
        }

    @property
    def bucket_horizon_metrics(self) -> dict[tuple[str, int], DepthOutcomeMetrics]:
        return {
            (metrics.regime, metrics.horizon_records): metrics
            for metrics in self.l5_magnitude_metrics
        }


@dataclass(frozen=True)
class _DepthRecord:
    source_file: Path
    timestamp: datetime
    mid_price: Decimal
    l1: Decimal
    l2: Decimal | None
    l3: Decimal | None
    l5: Decimal
    l10: Decimal | None
    ask_concentration: Decimal | None
    bid_concentration: Decimal | None


class DepthOutcomeAnalyzer:
    DEFAULT_HORIZONS = (1, 3, 6, 12)

    def analyze_files(
        self,
        paths: Iterable[str | Path],
        horizons: Iterable[int] | None = None,
    ) -> DepthOutcomeAnalysis:
        files = tuple(Path(path) for path in paths)
        if not files:
            raise ValueError("at least one JSONL file is required")
        validated_horizons = self.validate_horizons(
            self.DEFAULT_HORIZONS if horizons is None else horizons
        )
        raw_count = 0
        valid_count = 0
        skipped_count = 0
        observations: list[DepthOutcomeObservation] = []
        regime_counts = {regime: 0 for regime in REGIMES}
        inclusive_counts = {pair: 0 for pair in INCLUSIVE_SIGN_PAIRS}
        per_file_inclusive_metrics: list[DepthOutcomeFileInclusiveMetrics] = []
        for path in files:
            raw_records = self.load_jsonl(path)
            raw_count += len(raw_records)
            timeline: list[_DepthRecord] = []
            for record in raw_records:
                parsed = self._parse_record(record, path)
                if parsed is None:
                    skipped_count += 1
                else:
                    valid_count += 1
                    timeline.append(parsed)
                    regime_counts[classify_regime(record)] += 1
                    inclusive_counts[classify_inclusive_sign_pair(record)] += 1
            timeline.sort(key=lambda item: item.timestamp)
            file_observations = self._build_observations(timeline, validated_horizons)
            observations.extend(file_observations)
            per_file_inclusive_metrics.append(
                DepthOutcomeFileInclusiveMetrics(
                    source_file=path,
                    metrics=tuple(
                        self._aggregate(
                            tuple(file_observations),
                            pair,
                            horizon,
                            by_inclusive_pair=True,
                        )
                        for pair in INCLUSIVE_SIGN_PAIRS
                        for horizon in validated_horizons
                    ),
                )
            )
        observation_tuple = tuple(observations)
        metrics = tuple(
            self._aggregate(observation_tuple, regime, horizon)
            for regime in REGIMES
            for horizon in validated_horizons
        )
        bucket_metrics = tuple(
            self._aggregate(observation_tuple, bucket, horizon, by_bucket=True)
            for bucket in L5_MAGNITUDE_BUCKETS
            for horizon in validated_horizons
        )
        comparisons = tuple(
            DepthOutcomeComparison(
                horizon_records=horizon,
                positive_l1_negative_l5=self._aggregate(
                    observation_tuple,
                    "L1_POSITIVE_L5_NEGATIVE",
                    horizon,
                ),
                negative_l1_negative_l5=self._aggregate(
                    observation_tuple,
                    "L1_NEGATIVE_L5_NEGATIVE",
                    horizon,
                ),
            )
            for horizon in validated_horizons
        )
        inclusive_metrics = tuple(
            self._aggregate(
                observation_tuple,
                pair,
                horizon,
                by_inclusive_pair=True,
            )
            for pair in INCLUSIVE_SIGN_PAIRS
            for horizon in validated_horizons
        )
        inclusive_comparisons = tuple(
            self._inclusive_comparison(observation_tuple, horizon)
            for horizon in validated_horizons
        )
        return DepthOutcomeAnalysis(
            files=files,
            raw_record_count=raw_count,
            valid_record_count=valid_count,
            skipped_record_count=skipped_count,
            horizons=validated_horizons,
            regime_counts=regime_counts,
            inclusive_sign_pair_counts=inclusive_counts,
            observations=observation_tuple,
            metrics_by_regime=metrics,
            l5_magnitude_metrics=bucket_metrics,
            comparisons=comparisons,
            inclusive_sign_pair_metrics=inclusive_metrics,
            inclusive_comparisons=inclusive_comparisons,
            per_file_inclusive_metrics=tuple(per_file_inclusive_metrics),
        )

    def load_jsonl(self, path: str | Path) -> list[dict[str, Any]]:
        input_path = Path(path)
        if not input_path.exists():
            raise FileNotFoundError(f"paper run file does not exist: {input_path}")
        records: list[dict[str, Any]] = []
        with input_path.open("r", encoding="utf-8") as file:
            for line_number, line in enumerate(file, start=1):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"invalid JSON on line {line_number}: {exc.msg}") from exc
                if not isinstance(record, dict):
                    raise ValueError(f"record on line {line_number} must be an object")
                records.append(record)
        return records

    @staticmethod
    def validate_horizons(horizons: Iterable[int]) -> tuple[int, ...]:
        values = tuple(horizons)
        if not values:
            raise ValueError("at least one forward horizon is required")
        validated: list[int] = []
        for value in values:
            if isinstance(value, bool):
                raise ValueError("forward horizons must be positive integers")
            try:
                horizon = int(value)
            except (TypeError, ValueError) as exc:
                raise ValueError("forward horizons must be positive integers") from exc
            if horizon != value or horizon < 1:
                raise ValueError("forward horizons must be positive integers")
            if horizon not in validated:
                validated.append(horizon)
        return tuple(sorted(validated))

    def _parse_record(self, record: dict[str, Any], source_file: Path) -> _DepthRecord | None:
        if record.get("iteration_ok") is False:
            return None
        timestamp = self._parse_timestamp(record.get("timestamp"))
        mid_price = self._decimal(record.get("mid_price"))
        l1 = self._decimal(record.get("depth_imbalance_l1"))
        l5 = self._decimal(record.get("depth_imbalance_l5"))
        if timestamp is None or mid_price is None or mid_price <= 0 or l1 is None or l5 is None:
            return None
        return _DepthRecord(
            source_file=source_file,
            timestamp=timestamp,
            mid_price=mid_price,
            l1=l1,
            l2=self._decimal(record.get("depth_imbalance_l2")),
            l3=self._decimal(record.get("depth_imbalance_l3")),
            l5=l5,
            l10=self._decimal(record.get("depth_imbalance_l10")),
            ask_concentration=self._decimal(record.get("ask_depth_concentration_l2_to_l5")),
            bid_concentration=self._decimal(record.get("bid_depth_concentration_l2_to_l5")),
        )

    def _build_observations(
        self,
        timeline: list[_DepthRecord],
        horizons: tuple[int, ...],
    ) -> list[DepthOutcomeObservation]:
        observations: list[DepthOutcomeObservation] = []
        for index, current in enumerate(timeline):
            regime = classify_regime_from_values(current.l1, current.l5, current.l2, current.l3, current.l10)
            bucket = l5_magnitude_bucket(current.l5)
            for horizon in horizons:
                future_index = index + horizon
                if future_index >= len(timeline):
                    continue
                future = timeline[future_index]
                path_returns = [
                    self._return_bps(current.mid_price, item.mid_price)
                    for item in timeline[index + 1 : future_index + 1]
                ]
                observations.append(
                    DepthOutcomeObservation(
                        source_file=current.source_file,
                        timestamp=current.timestamp,
                        future_timestamp=future.timestamp,
                        horizon_records=horizon,
                        regime=regime,
                        inclusive_sign_pair=classify_inclusive_sign_pair_from_values(
                            current.l1,
                            current.l5,
                        ),
                        l5_magnitude_bucket=bucket,
                        current_mid_price=current.mid_price,
                        future_mid_price=future.mid_price,
                        forward_return_bps=path_returns[-1],
                        elapsed_seconds=Decimal(str(max(0, (future.timestamp - current.timestamp).total_seconds()))),
                        maximum_favorable_excursion_bps=max(Decimal("0"), max(path_returns)),
                        maximum_adverse_excursion_bps=max(Decimal("0"), -min(path_returns)),
                    )
                )
        return observations

    def _aggregate(
        self,
        observations: tuple[DepthOutcomeObservation, ...],
        key: str,
        horizon: int,
        *,
        by_bucket: bool = False,
        by_inclusive_pair: bool = False,
    ) -> DepthOutcomeMetrics:
        selected = [
            item for item in observations
            if item.horizon_records == horizon
            and (
                (
                    item.inclusive_sign_pair
                    if by_inclusive_pair
                    else item.l5_magnitude_bucket
                    if by_bucket
                    else item.regime
                )
                == key
            )
        ]
        returns = [item.forward_return_bps for item in selected]
        favorable = [item.maximum_favorable_excursion_bps for item in selected]
        adverse = [item.maximum_adverse_excursion_bps for item in selected]
        nonzero_returns = [value for value in returns if value != 0]
        return DepthOutcomeMetrics(
            regime=key,
            horizon_records=horizon,
            observation_count=len(selected),
            average_forward_return_bps=self._average(returns),
            median_forward_return_bps=self._median(returns),
            minimum_forward_return_bps=min(returns) if returns else None,
            maximum_forward_return_bps=max(returns) if returns else None,
            positive_return_count=sum(value > 0 for value in returns),
            negative_return_count=sum(value < 0 for value in returns),
            zero_return_count=sum(value == 0 for value in returns),
            average_elapsed_seconds=self._average([item.elapsed_seconds for item in selected]),
            maximum_favorable_excursion_bps=max(favorable) if favorable else None,
            maximum_adverse_excursion_bps=max(adverse) if adverse else None,
            average_favorable_excursion_bps=self._average(favorable),
            median_favorable_excursion_bps=self._median(favorable),
            average_adverse_excursion_bps=self._average(adverse),
            median_adverse_excursion_bps=self._median(adverse),
            zero_return_rate=(
                Decimal(sum(value == 0 for value in returns)) / Decimal(len(returns))
                if returns else Decimal("0")
            ),
            nonzero_observation_count=len(nonzero_returns),
            average_nonzero_forward_return_bps=self._average(nonzero_returns),
            median_nonzero_forward_return_bps=self._median(nonzero_returns),
        )

    def _inclusive_comparison(
        self,
        observations: tuple[DepthOutcomeObservation, ...],
        horizon: int,
    ) -> InclusiveDepthOutcomeComparison:
        positive = self._aggregate(
            observations, "L1_POSITIVE_L5_NEGATIVE", horizon, by_inclusive_pair=True
        )
        negative = self._aggregate(
            observations, "L1_NEGATIVE_L5_NEGATIVE", horizon, by_inclusive_pair=True
        )
        return InclusiveDepthOutcomeComparison(
            horizon_records=horizon,
            positive_l1_negative_l5=positive,
            negative_l1_negative_l5=negative,
            average_return_difference_bps=self._difference(
                positive.average_forward_return_bps,
                negative.average_forward_return_bps,
            ),
            median_return_difference_bps=self._difference(
                positive.median_forward_return_bps,
                negative.median_forward_return_bps,
            ),
            zero_return_rate_difference=positive.zero_return_rate - negative.zero_return_rate,
            positive_return_count_difference=(
                positive.positive_return_count - negative.positive_return_count
            ),
            negative_return_count_difference=(
                positive.negative_return_count - negative.negative_return_count
            ),
            zero_return_count_difference=(
                positive.zero_return_count - negative.zero_return_count
            ),
            average_favorable_excursion_difference_bps=self._difference(
                positive.average_favorable_excursion_bps,
                negative.average_favorable_excursion_bps,
            ),
            median_favorable_excursion_difference_bps=self._difference(
                positive.median_favorable_excursion_bps,
                negative.median_favorable_excursion_bps,
            ),
            average_adverse_excursion_difference_bps=self._difference(
                positive.average_adverse_excursion_bps,
                negative.average_adverse_excursion_bps,
            ),
            median_adverse_excursion_difference_bps=self._difference(
                positive.median_adverse_excursion_bps,
                negative.median_adverse_excursion_bps,
            ),
        )

    @staticmethod
    def _difference(left: Decimal | None, right: Decimal | None) -> Decimal | None:
        if left is None or right is None:
            return None
        return left - right

    @staticmethod
    def _return_bps(current: Decimal, future: Decimal) -> Decimal:
        return (future - current) / current * Decimal("10000")

    @staticmethod
    def _average(values: list[Decimal]) -> Decimal | None:
        return sum(values, Decimal("0")) / Decimal(len(values)) if values else None

    @staticmethod
    def _median(values: list[Decimal]) -> Decimal | None:
        if not values:
            return None
        ordered = sorted(values)
        middle = len(ordered) // 2
        if len(ordered) % 2:
            return ordered[middle]
        return (ordered[middle - 1] + ordered[middle]) / Decimal("2")

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
        if isinstance(value, datetime):
            parsed = value
        else:
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


# Compatibility aliases for callers that use the longer structure-oriented name.
DepthStructureOutcomeAnalyzer = DepthOutcomeAnalyzer


def classify_regime(
    record: dict[str, Any] | None = None,
    *,
    l1: Decimal | None = None,
    l5: Decimal | None = None,
    l2: Decimal | None = None,
    l3: Decimal | None = None,
    l10: Decimal | None = None,
) -> str:
    if record is not None:
        l1 = _module_decimal(record.get("depth_imbalance_l1"))
        l5 = _module_decimal(record.get("depth_imbalance_l5"))
        l2 = _module_decimal(record.get("depth_imbalance_l2"))
        l3 = _module_decimal(record.get("depth_imbalance_l3"))
        l10 = _module_decimal(record.get("depth_imbalance_l10"))
    return classify_regime_from_values(
        l1,
        l5,
        l2,
        l3,
        l10,
    )


classify_depth_regime = classify_regime


def classify_inclusive_sign_pair(record: dict[str, Any]) -> str:
    return classify_inclusive_sign_pair_from_values(
        _module_decimal(record.get("depth_imbalance_l1")),
        _module_decimal(record.get("depth_imbalance_l5")),
    )


def classify_inclusive_sign_pair_from_values(
    l1: Decimal | None,
    l5: Decimal | None,
) -> str:
    if l1 is None or l5 is None:
        return "UNKNOWN"
    if l1 == 0 or l5 == 0:
        return "L1_OR_L5_ZERO"
    if l1 > 0 and l5 < 0:
        return "L1_POSITIVE_L5_NEGATIVE"
    if l1 < 0 and l5 < 0:
        return "L1_NEGATIVE_L5_NEGATIVE"
    if l1 > 0 and l5 > 0:
        return "L1_POSITIVE_L5_POSITIVE"
    if l1 < 0 and l5 > 0:
        return "L1_NEGATIVE_L5_POSITIVE"
    return "UNKNOWN"


def classify_regime_from_values(
    l1: Decimal | None,
    l5: Decimal | None,
    l2: Decimal | None = None,
    l3: Decimal | None = None,
    l10: Decimal | None = None,
) -> str:
    if l1 is None or l5 is None or l1 == 0 or l5 == 0:
        return "UNKNOWN"
    intermediate = (l1, l2, l3, l5, l10)
    if all(value is not None and value > 0 for value in intermediate):
        return "SAME_SIGN_POSITIVE"
    if all(value is not None and value < 0 for value in intermediate):
        return "SAME_SIGN_NEGATIVE"
    if l1 > 0 and l5 < 0:
        return "L1_POSITIVE_L5_NEGATIVE"
    if l1 < 0 and l5 < 0:
        return "L1_NEGATIVE_L5_NEGATIVE"
    if l1 > 0 and l5 > 0:
        return "L1_POSITIVE_L5_POSITIVE"
    if l1 < 0 and l5 > 0:
        return "L1_NEGATIVE_L5_POSITIVE"
    return "UNKNOWN"


def l5_magnitude_bucket(value: Decimal | None) -> str | None:
    if value is None:
        return None
    magnitude = abs(value)
    if magnitude < Decimal("0.20"):
        return "mild"
    if magnitude < Decimal("0.40"):
        return "medium"
    return "strong"


def _module_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return parsed if parsed.is_finite() else None
