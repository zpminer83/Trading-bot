import argparse
from decimal import Decimal
from pathlib import Path

from bot.analytics.depth_structure_analyzer import DepthStructureAnalyzer


def fmt(value: Decimal | None) -> str:
    return "n/a" if value is None else format(value, "f")


def main() -> int:
    parser = argparse.ArgumentParser(description="Analyze recorded multi-depth order-book telemetry.")
    parser.add_argument("files", nargs="+", type=Path)
    args = parser.parse_args()
    summary = DepthStructureAnalyzer().analyze_files(args.files)
    print("ORDER-BOOK DEPTH STRUCTURE")
    print(f"Files: {len(summary.files)}")
    print(f"Records: {summary.record_count}")
    print(f"Records with depth telemetry: {summary.depth_record_count}")
    for depth, distribution in summary.distributions.items():
        print(
            f"{depth.upper()} imbalance: count={distribution.count} "
            f"min={fmt(distribution.minimum)} avg={fmt(distribution.average)} "
            f"median={fmt(distribution.median)} max={fmt(distribution.maximum)}"
        )
        print(f"  Signs: {summary.sign_counts[depth]}")
    print(f"Positive L1 -> negative L5: {summary.l1_positive_l5_negative_count}")
    print(f"Negative L1 -> positive L5: {summary.l1_negative_l5_positive_count}")
    print(f"Sign-consistency failures: {summary.sign_consistency_failure_count}")
    print(f"Average bid L2-L5 concentration: {fmt(summary.average_bid_depth_concentration_l2_to_l5)}")
    print(f"Median bid L2-L5 concentration: {fmt(summary.median_bid_depth_concentration_l2_to_l5)}")
    print(f"Average ask L2-L5 concentration: {fmt(summary.average_ask_depth_concentration_l2_to_l5)}")
    print(f"Median ask L2-L5 concentration: {fmt(summary.median_ask_depth_concentration_l2_to_l5)}")
    print(f"Ask depth grows faster L1->L5: {fmt(summary.ask_depth_grows_faster_percentage)}%")
    print()
    print("Warning: displayed order-book depth may reflect cancellations or spoof-like behavior.")
    print("It is not proof of future trading direction.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
