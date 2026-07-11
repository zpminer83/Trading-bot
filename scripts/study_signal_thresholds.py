import argparse
from decimal import Decimal
from pathlib import Path

from bot.analytics.signal_threshold_study import (
    DIRECTIONS,
    DirectionalThresholdMetrics,
    SignalThresholdStudy,
    SignalThresholdStudyConfig,
)


def parse_int_tuple(value: str, *, expected_length: int | None = None) -> tuple[int, ...]:
    try:
        values = tuple(int(item.strip()) for item in value.split(",") if item.strip())
    except ValueError as exc:
        raise argparse.ArgumentTypeError("value must contain comma-separated integers") from exc
    if not values or any(item < 1 for item in values):
        raise argparse.ArgumentTypeError("values must be positive integers")
    if expected_length is not None and len(values) != expected_length:
        raise argparse.ArgumentTypeError(f"exactly {expected_length} values are required")
    return values


def parse_horizons(value: str) -> tuple[int, ...]:
    return parse_int_tuple(value)


def parse_split(value: str) -> tuple[int, int, int]:
    values = parse_int_tuple(value, expected_length=3)
    if sum(values) != 100:
        raise argparse.ArgumentTypeError("split percentages must total 100")
    return values  # type: ignore[return-value]


def fmt_decimal(value: Decimal | None, suffix: str = "") -> str:
    if value is None:
        return "n/a"
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return f"{text or '0'}{suffix}"


def fmt_rate(value: Decimal | None) -> str:
    return fmt_decimal(value * Decimal("100"), "%") if value is not None else "n/a"


def print_metrics(label: str, metrics: DirectionalThresholdMetrics) -> None:
    print(f"    {label} {metrics.direction}:")
    print(f"      Observations       : {metrics.observation_count}")
    print(f"      Coverage           : {fmt_rate(metrics.coverage_ratio)}")
    print(
        "      Average / median   : "
        f"{fmt_decimal(metrics.average_forward_return_bps, ' bps')} / "
        f"{fmt_decimal(metrics.median_forward_return_bps, ' bps')}"
    )
    print(f"      Hit rate           : {fmt_rate(metrics.directional_hit_rate)}")
    print(
        "      Favorable / adverse: "
        f"{fmt_decimal(metrics.average_favorable_excursion_bps, ' bps')} / "
        f"{fmt_decimal(metrics.average_adverse_excursion_bps, ' bps')}"
    )
    print(
        "      Excursion ratio    : "
        f"{fmt_decimal(metrics.favorable_adverse_excursion_ratio)}"
    )
    print(
        "      Return stddev      : "
        f"{fmt_decimal(metrics.standard_deviation_forward_return_bps, ' bps')}"
    )
    print(
        "      File consistency   : "
        f"{fmt_rate(metrics.file_consistency_ratio)} "
        f"across {metrics.contributing_file_count} contributing file(s)"
    )


def print_result(result) -> None:
    print("SIGNAL THRESHOLD STUDY")
    print()
    print("Files:")
    for path in result.files:
        print(f"  {path}")
    print(f"Valid records                  : {result.valid_record_count}")
    print(f"Skipped records                : {result.skipped_record_count}")
    print(
        "Chronological split sizes     : "
        f"training={result.split_sizes.training}, "
        f"validation={result.split_sizes.validation}, test={result.split_sizes.test}"
    )
    print(f"Candidate combinations evaluated: {result.candidate_combination_count}")
    print(f"Eligible candidates            : {result.eligible_candidate_count}")

    if not result.selected_candidates:
        print()
        print("No candidates met the training and validation sample requirements.")
    for index, selected in enumerate(result.selected_candidates, start=1):
        candidate = selected.candidate
        print()
        print(f"Selected candidate {index}")
        print(f"  Interpretation: {candidate.interpretation}")
        print(
            "  Thresholds    : "
            f"imbalance={candidate.imbalance_threshold}, "
            f"edge={candidate.microprice_edge_threshold_bps} bps, "
            f"momentum={candidate.momentum_threshold_bps} bps, "
            f"max spread={candidate.maximum_spread_bps} bps"
        )
        print(f"  Horizon      : {candidate.horizon_records} record(s)")
        for direction in DIRECTIONS:
            print_metrics(
                "Training",
                next(item for item in selected.training_metrics if item.direction == direction),
            )
            print_metrics(
                "Validation",
                next(item for item in selected.validation_metrics if item.direction == direction),
            )
            print_metrics(
                "Held-out test",
                next(item for item in selected.test_metrics if item.direction == direction),
            )
        print(f"  Validation status: {selected.validation_status}")
        if selected.validation_reasons:
            print("  Reasons:")
            for reason in selected.validation_reasons:
                print(f"    - {reason}")

    print()
    print("Warnings:")
    print("  - Threshold searches can overfit historical data.")
    print("  - Held-out results remain exploratory with small samples.")
    print("  - Overlapping forward horizons are not independent observations.")
    print("  - Paper-market results do not establish live profitability.")
    print("  - No candidate should affect trading until repeated independent runs agree.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run an offline leakage-resistant order-book threshold study."
    )
    parser.add_argument("files", nargs="+", type=Path)
    parser.add_argument("--horizons", type=parse_horizons, default=(1, 3, 6, 12))
    parser.add_argument("--minimum-training-samples", type=int, default=30)
    parser.add_argument("--minimum-validation-samples", type=int, default=10)
    parser.add_argument("--split", type=parse_split, default=(60, 20, 20))
    parser.add_argument("--top", type=int, default=5)
    parser.add_argument("--json-output", type=Path)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        config = SignalThresholdStudyConfig(
            horizons=args.horizons,
            split_percentages=args.split,
            minimum_training_samples=args.minimum_training_samples,
            minimum_validation_samples=args.minimum_validation_samples,
            top_per_interpretation=args.top,
        )
        study = SignalThresholdStudy(config)
        result = study.analyze_files(args.files)
        if args.json_output is not None:
            study.export_json(result, args.json_output)
    except (FileNotFoundError, ValueError) as exc:
        parser.error(str(exc))
    print_result(result)
    if args.json_output is not None:
        print(f"JSON output: {args.json_output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
