import argparse
from decimal import Decimal
from pathlib import Path

from bot.analytics.depth_outcome_analyzer import (
    L5_MAGNITUDE_BUCKETS,
    REGIMES,
    DepthOutcomeAnalysis,
    DepthOutcomeMetrics,
    DepthOutcomeAnalyzer,
)


def parse_horizons(value: str) -> tuple[int, ...]:
    try:
        return DepthOutcomeAnalyzer.validate_horizons(
            int(item.strip()) for item in value.split(",") if item.strip()
        )
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def fmt(value: Decimal | None) -> str:
    return "n/a" if value is None else format(value, "f")


def print_metrics(metrics: DepthOutcomeMetrics) -> None:
    print(
        f"      n={metrics.observation_count} "
        f"avg={fmt(metrics.average_forward_return_bps)} "
        f"median={fmt(metrics.median_forward_return_bps)} "
        f"min={fmt(metrics.minimum_forward_return_bps)} "
        f"max={fmt(metrics.maximum_forward_return_bps)}"
    )
    print(
        f"      positive/negative/zero="
        f"{metrics.positive_return_count}/{metrics.negative_return_count}/{metrics.zero_return_count} "
        f"avg elapsed={fmt(metrics.average_elapsed_seconds)}s"
    )
    print(
        f"      favorable max/avg/median="
        f"{fmt(metrics.maximum_favorable_excursion_bps)}/"
        f"{fmt(metrics.average_favorable_excursion_bps)}/"
        f"{fmt(metrics.median_favorable_excursion_bps)}; "
        f"adverse max/avg/median="
        f"{fmt(metrics.maximum_adverse_excursion_bps)}/"
        f"{fmt(metrics.average_adverse_excursion_bps)}/"
        f"{fmt(metrics.median_adverse_excursion_bps)}"
    )


def print_analysis(analysis: DepthOutcomeAnalysis) -> None:
    print("DEPTH STRUCTURE FORWARD-OUTCOME ANALYSIS")
    print()
    print("Files:")
    for path in analysis.files:
        print(f"  {path}")
    print(f"Valid records  : {analysis.valid_record_count}")
    print(f"Skipped records: {analysis.skipped_record_count}")
    print(f"Horizons       : {', '.join(str(item) for item in analysis.horizons)}")

    print()
    print("Regime counts:")
    for regime in REGIMES:
        print(f"  {regime}: {analysis.regime_counts[regime]}")

    print()
    print("Metrics by regime and horizon:")
    for regime in REGIMES:
        print(f"  {regime}:")
        for horizon in analysis.horizons:
            print(f"    horizon {horizon}:")
            print_metrics(analysis.metrics_for(regime, horizon))

    print()
    print("Comparison: L1 positive / L5 negative vs L1 negative / L5 negative:")
    for comparison in analysis.comparisons:
        print(f"  horizon {comparison.horizon_records}:")
        print("    L1_POSITIVE_L5_NEGATIVE:")
        print_metrics(comparison.positive_l1_negative_l5)
        print("    L1_NEGATIVE_L5_NEGATIVE:")
        print_metrics(comparison.negative_l1_negative_l5)

    print()
    print("L5 magnitude bucket results:")
    for bucket in L5_MAGNITUDE_BUCKETS:
        print(f"  {bucket}:")
        for horizon in analysis.horizons:
            print(f"    horizon {horizon}:")
            print_metrics(analysis.l5_metrics_for(bucket, horizon))

    print()
    print("Warnings:")
    print("  - Displayed depth can be cancelled or otherwise ephemeral.")
    print("  - Overlapping horizons are not independent observations.")
    print("  - Paper data does not establish live profitability.")
    print("  - Structural association does not prove causality.")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Analyze forward outcomes for recorded depth-structure regimes."
    )
    parser.add_argument("files", nargs="+", type=Path)
    parser.add_argument("--horizons", type=parse_horizons, default=(1, 3, 6, 12))
    args = parser.parse_args()
    try:
        analysis = DepthOutcomeAnalyzer().analyze_files(args.files, args.horizons)
    except (FileNotFoundError, ValueError) as exc:
        parser.error(str(exc))
    print_analysis(analysis)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
