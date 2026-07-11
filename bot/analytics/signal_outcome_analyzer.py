from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable


KNOWN_SIGNAL_STATES = (
    "bullish",
    "bearish",
    "neutral",
    "warming_up",
    "unavailable",
)
DIRECTIONAL_SIGNAL_STATES = ("bullish", "bearish")
CONFIDENCE_BUCKETS = ("low", "medium", "high")


@dataclass(frozen=True)
class SignalOutcomeObservation:
    source_file: Path
    symbol: str
    timestamp: datetime
    future_timestamp: datetime
    horizon_records: int
    state: str
    reason: str | None
    current_mid_price: Decimal
    future_mid_price: Decimal
    forward_return_bps: Decimal
    elapsed_seconds: Decimal
    maximum_favorable_excursion_bps: Decimal | None
    maximum_adverse_excursion_bps: Decimal | None
    confidence: Decimal | None
    confidence_bucket: str | None
    depth_imbalance: Decimal | None
    microprice_edge_bps: Decimal | None
    rolling_momentum_bps: Decimal | None


@dataclass(frozen=True)
class SignalStateHorizonStats:
    state: str
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
    directional_hit_count: int | None
    directional_miss_count: int | None
    directional_hit_rate: Decimal | None
    average_favorable_excursion_bps: Decimal | None
    median_favorable_excursion_bps: Decimal | None
    average_adverse_excursion_bps: Decimal | None
    median_adverse_excursion_bps: Decimal | None


@dataclass(frozen=True)
class SignalConfidenceBucketStats:
    state: str
    horizon_records: int
    confidence_bucket: str
    observation_count: int
    average_forward_return_bps: Decimal | None
    directional_hit_count: int
    directional_miss_count: int
    directional_hit_rate: Decimal | None


@dataclass(frozen=True)
class SignalOutcomeAnalysis:
    files: tuple[Path, ...]
    raw_record_count: int
    valid_record_count: int
    skipped_record_count: int
    horizons: tuple[int, ...]
    observations: tuple[SignalOutcomeObservation, ...]
    state_horizon_stats: tuple[SignalStateHorizonStats, ...]
    confidence_bucket_stats: tuple[SignalConfidenceBucketStats, ...]

    def stats_for(self, state: str, horizon_records: int) -> SignalStateHorizonStats:
        normalized_state = str(state).lower()
        for stats in self.state_horizon_stats:
            if (
                stats.state == normalized_state
                and stats.horizon_records == horizon_records
            ):
                return stats
        raise KeyError(f"no statistics for {state} at horizon {horizon_records}")


@dataclass(frozen=True)
class _PriceRecord:
    source_file: Path
    symbol: str
    timestamp: datetime
    mid_price: Decimal
    state: str | None
    reason: str | None
    confidence: Decimal | None
    depth_imbalance: Decimal | None
    microprice_edge_bps: Decimal | None
    rolling_momentum_bps: Decimal | None


class SignalOutcomeAnalyzer:
    DEFAULT_HORIZONS = (1, 3, 6, 12)

    def analyze_files(
        self,
        paths: Iterable[str | Path],
        horizons: Iterable[int] | None = None,
    ) -> SignalOutcomeAnalysis:
        resolved_paths = tuple(Path(path) for path in paths)
        if not resolved_paths:
            raise ValueError("at least one JSONL file is required")
        validated_horizons = self.validate_horizons(
            horizons if horizons is not None else self.DEFAULT_HORIZONS
        )

        raw_record_count = 0
        valid_record_count = 0
        skipped_record_count = 0
        observations: list[SignalOutcomeObservation] = []
        for path in resolved_paths:
            raw_records = self.load_jsonl(path)
            raw_record_count += len(raw_records)
            timeline: list[_PriceRecord] = []
            for record in raw_records:
                parsed = self._parse_price_record(record, path)
                if parsed is not None:
                    timeline.append(parsed)
                if parsed is not None and parsed.state in KNOWN_SIGNAL_STATES:
                    valid_record_count += 1
                else:
                    skipped_record_count += 1
            observations.extend(self._build_observations(timeline, validated_horizons))

        observation_tuple = tuple(observations)
        return SignalOutcomeAnalysis(
            files=resolved_paths,
            raw_record_count=raw_record_count,
            valid_record_count=valid_record_count,
            skipped_record_count=skipped_record_count,
            horizons=validated_horizons,
            observations=observation_tuple,
            state_horizon_stats=self._aggregate_state_horizons(
                observation_tuple,
                validated_horizons,
            ),
            confidence_bucket_stats=self._aggregate_confidence_buckets(
                observation_tuple,
                validated_horizons,
            ),
        )

    def load_jsonl(self, path: str | Path) -> list[dict[str, Any]]:
        input_path = Path(path)
        if not input_path.exists():
            raise FileNotFoundError(f"paper run file does not exist: {input_path}")
        if not input_path.is_file():
            raise ValueError(f"paper run path is not a file: {input_path}")

        records: list[dict[str, Any]] = []
        with input_path.open("r", encoding="utf-8") as file:
            for line_number, raw_line in enumerate(file, start=1):
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(
                        f"invalid JSON on line {line_number} of {input_path}: {exc.msg}"
                    ) from exc
                if not isinstance(record, dict):
                    raise ValueError(
                        f"record on line {line_number} of {input_path} must be an object"
                    )
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

    def _parse_price_record(
        self,
        record: dict[str, Any],
        source_file: Path,
    ) -> _PriceRecord | None:
        if record.get("iteration_ok") is False:
            return None
        timestamp = self._parse_timestamp(record.get("timestamp"))
        mid_price = self._optional_decimal(record.get("mid_price"))
        if timestamp is None or mid_price is None or mid_price <= 0:
            return None
        state_text = str(record.get("signal_state") or "").strip().lower()
        state = state_text if state_text in KNOWN_SIGNAL_STATES else None
        confidence = self._optional_decimal(record.get("signal_confidence"))
        if confidence is not None and not Decimal("0") <= confidence <= Decimal("1"):
            confidence = None
        return _PriceRecord(
            source_file=source_file,
            symbol=str(record.get("symbol") or "unknown"),
            timestamp=timestamp,
            mid_price=mid_price,
            state=state,
            reason=(
                str(record.get("signal_reason"))
                if record.get("signal_reason") is not None
                else None
            ),
            confidence=confidence,
            depth_imbalance=self._optional_decimal(
                record.get("signal_depth_imbalance")
            ),
            microprice_edge_bps=self._optional_decimal(
                record.get("signal_microprice_edge_bps")
            ),
            rolling_momentum_bps=self._optional_decimal(
                record.get("signal_rolling_momentum_bps")
            ),
        )

    def _build_observations(
        self,
        timeline: list[_PriceRecord],
        horizons: tuple[int, ...],
    ) -> list[SignalOutcomeObservation]:
        observations: list[SignalOutcomeObservation] = []
        for index, current in enumerate(timeline):
            if current.state not in KNOWN_SIGNAL_STATES:
                continue
            for horizon in horizons:
                future_index = index + horizon
                if future_index >= len(timeline):
                    continue
                future = timeline[future_index]
                forward_return = self._return_bps(
                    current.mid_price,
                    future.mid_price,
                )
                path_returns = [
                    self._return_bps(current.mid_price, item.mid_price)
                    for item in timeline[index + 1 : future_index + 1]
                ]
                favorable, adverse = self._excursions(current.state, path_returns)
                observations.append(
                    SignalOutcomeObservation(
                        source_file=current.source_file,
                        symbol=current.symbol,
                        timestamp=current.timestamp,
                        future_timestamp=future.timestamp,
                        horizon_records=horizon,
                        state=current.state,
                        reason=current.reason,
                        current_mid_price=current.mid_price,
                        future_mid_price=future.mid_price,
                        forward_return_bps=forward_return,
                        elapsed_seconds=Decimal(
                            str(
                                (future.timestamp - current.timestamp).total_seconds()
                            )
                        ),
                        maximum_favorable_excursion_bps=favorable,
                        maximum_adverse_excursion_bps=adverse,
                        confidence=current.confidence,
                        confidence_bucket=self._confidence_bucket(current.confidence),
                        depth_imbalance=current.depth_imbalance,
                        microprice_edge_bps=current.microprice_edge_bps,
                        rolling_momentum_bps=current.rolling_momentum_bps,
                    )
                )
        return observations

    def _aggregate_state_horizons(
        self,
        observations: tuple[SignalOutcomeObservation, ...],
        horizons: tuple[int, ...],
    ) -> tuple[SignalStateHorizonStats, ...]:
        aggregated: list[SignalStateHorizonStats] = []
        for horizon in horizons:
            for state in KNOWN_SIGNAL_STATES:
                selected = [
                    item
                    for item in observations
                    if item.horizon_records == horizon and item.state == state
                ]
                returns = [item.forward_return_bps for item in selected]
                elapsed = [item.elapsed_seconds for item in selected]
                favorable = [
                    item.maximum_favorable_excursion_bps
                    for item in selected
                    if item.maximum_favorable_excursion_bps is not None
                ]
                adverse = [
                    item.maximum_adverse_excursion_bps
                    for item in selected
                    if item.maximum_adverse_excursion_bps is not None
                ]
                hits: int | None = None
                misses: int | None = None
                hit_rate: Decimal | None = None
                if state in DIRECTIONAL_SIGNAL_STATES:
                    hits = sum(self._is_hit(state, value) for value in returns)
                    misses = len(returns) - hits
                    hit_rate = (
                        Decimal(hits) / Decimal(len(returns)) if returns else None
                    )
                aggregated.append(
                    SignalStateHorizonStats(
                        state=state,
                        horizon_records=horizon,
                        observation_count=len(selected),
                        average_forward_return_bps=self._average(returns),
                        median_forward_return_bps=self._median(returns),
                        minimum_forward_return_bps=min(returns) if returns else None,
                        maximum_forward_return_bps=max(returns) if returns else None,
                        positive_return_count=sum(value > 0 for value in returns),
                        negative_return_count=sum(value < 0 for value in returns),
                        zero_return_count=sum(value == 0 for value in returns),
                        average_elapsed_seconds=self._average(elapsed),
                        directional_hit_count=hits,
                        directional_miss_count=misses,
                        directional_hit_rate=hit_rate,
                        average_favorable_excursion_bps=self._average(favorable),
                        median_favorable_excursion_bps=self._median(favorable),
                        average_adverse_excursion_bps=self._average(adverse),
                        median_adverse_excursion_bps=self._median(adverse),
                    )
                )
        return tuple(aggregated)

    def _aggregate_confidence_buckets(
        self,
        observations: tuple[SignalOutcomeObservation, ...],
        horizons: tuple[int, ...],
    ) -> tuple[SignalConfidenceBucketStats, ...]:
        aggregated: list[SignalConfidenceBucketStats] = []
        for horizon in horizons:
            for state in DIRECTIONAL_SIGNAL_STATES:
                for bucket in CONFIDENCE_BUCKETS:
                    selected = [
                        item
                        for item in observations
                        if item.horizon_records == horizon
                        and item.state == state
                        and item.confidence_bucket == bucket
                    ]
                    returns = [item.forward_return_bps for item in selected]
                    hits = sum(self._is_hit(state, value) for value in returns)
                    misses = len(returns) - hits
                    aggregated.append(
                        SignalConfidenceBucketStats(
                            state=state,
                            horizon_records=horizon,
                            confidence_bucket=bucket,
                            observation_count=len(selected),
                            average_forward_return_bps=self._average(returns),
                            directional_hit_count=hits,
                            directional_miss_count=misses,
                            directional_hit_rate=(
                                Decimal(hits) / Decimal(len(returns))
                                if returns
                                else None
                            ),
                        )
                    )
        return tuple(aggregated)

    @staticmethod
    def _return_bps(current: Decimal, future: Decimal) -> Decimal:
        return (future - current) / current * Decimal("10000")

    @staticmethod
    def _excursions(
        state: str,
        path_returns: list[Decimal],
    ) -> tuple[Decimal | None, Decimal | None]:
        if not path_returns or state not in DIRECTIONAL_SIGNAL_STATES:
            return None, None
        if state == "bullish":
            return (
                max(Decimal("0"), max(path_returns)),
                min(Decimal("0"), min(path_returns)),
            )
        return (
            max(Decimal("0"), -min(path_returns)),
            max(Decimal("0"), max(path_returns)),
        )

    @staticmethod
    def _is_hit(state: str, forward_return: Decimal) -> bool:
        return (state == "bullish" and forward_return > 0) or (
            state == "bearish" and forward_return < 0
        )

    @staticmethod
    def _confidence_bucket(confidence: Decimal | None) -> str | None:
        if confidence is None:
            return None
        if confidence < Decimal("0.50"):
            return "low"
        if confidence < Decimal("0.75"):
            return "medium"
        return "high"

    @staticmethod
    def _average(values: list[Decimal]) -> Decimal | None:
        if not values:
            return None
        return sum(values, Decimal("0")) / Decimal(len(values))

    @staticmethod
    def _median(values: list[Decimal]) -> Decimal | None:
        if not values:
            return None
        ordered = sorted(values)
        midpoint = len(ordered) // 2
        if len(ordered) % 2:
            return ordered[midpoint]
        return (ordered[midpoint - 1] + ordered[midpoint]) / Decimal("2")

    @staticmethod
    def _optional_decimal(value: Any) -> Decimal | None:
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
