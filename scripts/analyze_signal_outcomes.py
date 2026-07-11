import argparse
from decimal import Decimal
from pathlib import Path

from bot.analytics.signal_outcome_analyzer import (
    DIRECTIONAL_SIGNAL_STATES,
    SignalOutcomeAnalysis,
    SignalOutcomeAnalyzer,
    SignalStateHorizonStats,
)


def parse_horizons(value: str) -> tuple[int, ...]:
    try:
        raw_values = tuple(int(item.strip()) for item in value.split(",") if item.strip())
    except ValueError as exc:
        raise argparse.ArgumentTypeError("horizons must be comma-separated integers") from exc
    try:
        return SignalOutcomeAnalyzer.validate_horizons(raw_values)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def fmt_decimal(value: Decimal | None, suffix: str = "") -> str:
    if value is None:
        return "n/a"
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return f"{text or '0'}{suffix}"


def fmt_rate(value: Decimal | None) -> str:
    if value is None:
        return "n/a"
    return fmt_decimal(value * Decimal("100"), "%")


def print_state(stats: SignalStateHorizonStats) -> None:
    print(f"  {stats.state.upper()}")
    print(f"    Observations             : {stats.observation_count}")
    print(
        "    Average return          : "
        f"{fmt_decimal(stats.average_forward_return_bps, ' bps')}"
    )
    print(
        "    Median return           : "
        f"{fmt_decimal(stats.median_forward_return_bps, ' bps')}"
    )
    if stats.state in DIRECTIONAL_SIGNAL_STATES:
        print(f"    Directional hits        : {stats.directional_hit_count}")
        print(f"    Directional misses      : {stats.directional_miss_count}")
        print(f"    Hit rate                : {fmt_rate(stats.directional_hit_rate)}")
        print(
            "    Average favorable excursion: "
            f"{fmt_decimal(stats.average_favorable_excursion_bps, ' bps')}"
        )
        print(
            "    Average adverse excursion  : "
            f"{fmt_decimal(stats.average_adverse_excursion_bps, ' bps')}"
        )
    else:
        print(
            "    Positive / negative / zero: "
            f"{stats.positive_return_count} / {stats.negative_return_count} / "
            f"{stats.zero_return_count}"
        )
    print(
        "    Average elapsed time    : "
        f"{fmt_decimal(stats.average_elapsed_seconds, 's')}"
    )


def print_analysis(analysis: SignalOutcomeAnalysis) -> None:
    print("SIGNAL FORWARD-OUTCOME ANALYSIS")
    print()
    print("Files:")
    for path in analysis.files:
        print(f"  {path}")
    print(f"Valid records  : {analysis.valid_record_count}")
    print(f"Skipped records: {analysis.skipped_record_count}")
    print("Horizons       : " + ", ".join(str(value) for value in analysis.horizons))
    print("Note: record horizons are not guaranteed wall-clock durations.")

    for horizon in analysis.horizons:
        print()
        print(f"Horizon: {horizon} record(s)")
        for state in ("bullish", "bearish", "neutral"):
            print_state(analysis.stats_for(state, horizon))

    print()
    print("Confidence-bucket diagnostics:")
    print("  Local diagnostic buckets: low < 0.50, medium < 0.75, high >= 0.75.")
    print("  These are not calibrated statistical categories.")
    for stats in analysis.confidence_bucket_stats:
        if stats.observation_count == 0:
            continue
        print(
            f"  horizon={stats.horizon_records} state={stats.state} "
            f"bucket={stats.confidence_bucket} observations={stats.observation_count} "
            f"average_return={fmt_decimal(stats.average_forward_return_bps, ' bps')} "
            f"hit_rate={fmt_rate(stats.directional_hit_rate)}"
        )

    print()
    print("Warnings:")
    print("  - Signal confidence is uncalibrated.")
    print("  - Results from small samples are not statistically reliable.")
    print("  - Paper results do not establish live profitability.")
    print("  - Multiple overlapping horizons are not independent observations.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Analyze forward outcomes of recorded order-book signals offline."
    )
    parser.add_argument("files", nargs="+", type=Path)
    parser.add_argument(
        "--horizons",
        type=parse_horizons,
        default=SignalOutcomeAnalyzer.DEFAULT_HORIZONS,
        help="Comma-separated forward record horizons (default: 1,3,6,12).",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        analysis = SignalOutcomeAnalyzer().analyze_files(
            args.files,
            horizons=args.horizons,
        )
    except (FileNotFoundError, ValueError) as exc:
        parser.error(str(exc))
    print_analysis(analysis)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
