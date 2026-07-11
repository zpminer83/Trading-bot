from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable


DEPTHS = ("l1", "l2", "l3", "l5", "l10")


@dataclass(frozen=True)
class DepthImbalanceDistribution:
    count: int
    minimum: Decimal | None
    average: Decimal | None
    median: Decimal | None
    maximum: Decimal | None


@dataclass(frozen=True)
class DepthStructureFileSummary:
    source_file: Path
    record_count: int
    depth_record_count: int
    distributions: dict[str, DepthImbalanceDistribution]
    sign_counts: dict[str, dict[str, int]]
    l1_positive_l5_negative_count: int
    l1_negative_l5_positive_count: int
    sign_consistency_failure_count: int
    average_bid_depth_concentration_l2_to_l5: Decimal | None
    median_bid_depth_concentration_l2_to_l5: Decimal | None
    average_ask_depth_concentration_l2_to_l5: Decimal | None
    median_ask_depth_concentration_l2_to_l5: Decimal | None
    ask_depth_grows_faster_percentage: Decimal | None


@dataclass(frozen=True)
class DepthStructureSummary:
    files: tuple[Path, ...]
    record_count: int
    depth_record_count: int
    distributions: dict[str, DepthImbalanceDistribution]
    sign_counts: dict[str, dict[str, int]]
    l1_positive_l5_negative_count: int
    l1_negative_l5_positive_count: int
    sign_consistency_failure_count: int
    average_bid_depth_concentration_l2_to_l5: Decimal | None
    median_bid_depth_concentration_l2_to_l5: Decimal | None
    average_ask_depth_concentration_l2_to_l5: Decimal | None
    median_ask_depth_concentration_l2_to_l5: Decimal | None
    ask_depth_grows_faster_percentage: Decimal | None
    per_file: tuple[DepthStructureFileSummary, ...]


class DepthStructureAnalyzer:
    """Analyze recorded depth telemetry without touching trading state."""

    def analyze_file(self, path: str | Path) -> DepthStructureFileSummary:
        input_path = Path(path)
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
                    raise ValueError(f"record on line {line_number} must be a JSON object")
                records.append(record)
        if not records:
            raise ValueError("depth structure file contains no records")
        return self._summarize_records(Path(path), records)

    def analyze_files(self, paths: Iterable[str | Path]) -> DepthStructureSummary:
        file_paths = tuple(Path(path) for path in paths)
        if not file_paths:
            raise ValueError("at least one JSONL file is required")
        per_file = tuple(self.analyze_file(path) for path in file_paths)
        records: list[dict[str, Any]] = []
        for path in file_paths:
            with path.open("r", encoding="utf-8") as file:
                records.extend(json.loads(line) for line in file if line.strip())
        aggregate = self._summarize_records(Path("<all files>"), records)
        return DepthStructureSummary(
            files=file_paths,
            record_count=aggregate.record_count,
            depth_record_count=aggregate.depth_record_count,
            distributions=aggregate.distributions,
            sign_counts=aggregate.sign_counts,
            l1_positive_l5_negative_count=aggregate.l1_positive_l5_negative_count,
            l1_negative_l5_positive_count=aggregate.l1_negative_l5_positive_count,
            sign_consistency_failure_count=aggregate.sign_consistency_failure_count,
            average_bid_depth_concentration_l2_to_l5=aggregate.average_bid_depth_concentration_l2_to_l5,
            median_bid_depth_concentration_l2_to_l5=aggregate.median_bid_depth_concentration_l2_to_l5,
            average_ask_depth_concentration_l2_to_l5=aggregate.average_ask_depth_concentration_l2_to_l5,
            median_ask_depth_concentration_l2_to_l5=aggregate.median_ask_depth_concentration_l2_to_l5,
            ask_depth_grows_faster_percentage=aggregate.ask_depth_grows_faster_percentage,
            per_file=per_file,
        )

    def _summarize_records(
        self,
        source_file: Path,
        records: list[dict[str, Any]],
    ) -> DepthStructureFileSummary:
        values: dict[str, list[Decimal]] = {depth: [] for depth in DEPTHS}
        sign_counts = {
            depth: {"positive": 0, "negative": 0, "zero": 0, "unknown": 0}
            for depth in DEPTHS
        }
        l1_positive_l5_negative = 0
        l1_negative_l5_positive = 0
        sign_failures = 0
        bid_concentrations: list[Decimal] = []
        ask_concentrations: list[Decimal] = []
        ask_growth_comparisons = 0
        ask_growth_faster = 0
        depth_record_count = 0
        for record in records:
            parsed = {depth: self._decimal(record.get(f"depth_imbalance_{depth}")) for depth in DEPTHS}
            has_depth = any(value is not None for value in parsed.values())
            if has_depth:
                depth_record_count += 1
            for depth, value in parsed.items():
                if value is None:
                    sign_counts[depth]["unknown"] += 1
                else:
                    values[depth].append(value)
                    sign_counts[depth][self._sign_name(value)] += 1
            l1 = parsed["l1"]
            l5 = parsed["l5"]
            if l1 is not None and l5 is not None:
                l1_positive_l5_negative += l1 > 0 and l5 < 0
                l1_negative_l5_positive += l1 < 0 and l5 > 0
            if record.get("l1_edge_sign_consistent") is False:
                sign_failures += 1
            bid_concentration = self._decimal(record.get("bid_depth_concentration_l2_to_l5"))
            ask_concentration = self._decimal(record.get("ask_depth_concentration_l2_to_l5"))
            if bid_concentration is not None:
                bid_concentrations.append(bid_concentration)
            if ask_concentration is not None:
                ask_concentrations.append(ask_concentration)
            bid_l1 = self._decimal(record.get("depth_bid_l1"))
            bid_l5 = self._decimal(record.get("depth_bid_l5"))
            ask_l1 = self._decimal(record.get("depth_ask_l1"))
            ask_l5 = self._decimal(record.get("depth_ask_l5"))
            if None not in (bid_l1, bid_l5, ask_l1, ask_l5):
                ask_growth = ask_l5 - ask_l1
                bid_growth = bid_l5 - bid_l1
                ask_growth_comparisons += 1
                ask_growth_faster += ask_growth > bid_growth
        return DepthStructureFileSummary(
            source_file=source_file,
            record_count=len(records),
            depth_record_count=depth_record_count,
            distributions={depth: self._distribution(values[depth]) for depth in DEPTHS},
            sign_counts=sign_counts,
            l1_positive_l5_negative_count=l1_positive_l5_negative,
            l1_negative_l5_positive_count=l1_negative_l5_positive,
            sign_consistency_failure_count=sign_failures,
            average_bid_depth_concentration_l2_to_l5=self._average(bid_concentrations),
            median_bid_depth_concentration_l2_to_l5=self._median(bid_concentrations),
            average_ask_depth_concentration_l2_to_l5=self._average(ask_concentrations),
            median_ask_depth_concentration_l2_to_l5=self._median(ask_concentrations),
            ask_depth_grows_faster_percentage=(
                Decimal(ask_growth_faster) / Decimal(ask_growth_comparisons) * Decimal("100")
                if ask_growth_comparisons else None
            ),
        )

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
    def _sign_name(value: Decimal) -> str:
        if value == 0:
            return "zero"
        return "positive" if value > 0 else "negative"

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

    @classmethod
    def _distribution(cls, values: list[Decimal]) -> DepthImbalanceDistribution:
        return DepthImbalanceDistribution(
            count=len(values),
            minimum=min(values) if values else None,
            average=cls._average(values),
            median=cls._median(values),
            maximum=max(values) if values else None,
        )
