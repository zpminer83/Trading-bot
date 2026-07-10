import argparse
from decimal import Decimal
from pathlib import Path

from bot.analytics.paper_run_analyzer import (
    PaperRunAnalyzer,
    PaperRunSummary,
)


def fmt_decimal(value: Decimal | None) -> str:
    if value is None:
        return "n/a"

    text = format(value, "f")

    if "." in text:
        text = text.rstrip("0").rstrip(".")

    return text or "0"


def fmt_seconds(value: Decimal | None) -> str:
    if value is None:
        return "n/a"

    return f"{fmt_decimal(value)}s"


def fmt_percentage(value: Decimal) -> str:
    percentage = (value * Decimal("100")).quantize(Decimal("0.01"))
    return f"{fmt_decimal(percentage)}%"


def format_duration(seconds: int) -> str:
    hours, remainder = divmod(seconds, 3600)
    minutes, remaining_seconds = divmod(remainder, 60)

    if hours > 0:
        return f"{hours}h {minutes}m {remaining_seconds}s"

    if minutes > 0:
        return f"{minutes}m {remaining_seconds}s"

    return f"{remaining_seconds}s"


def print_summary(
    path: Path,
    summary: PaperRunSummary,
) -> None:
    print("=" * 72)
    print("PAPER RUN ANALYSIS")
    print("=" * 72)
    print(f"File              : {path}")
    print(f"Records           : {summary.records_count}")
    print(f"First timestamp   : {summary.first_timestamp.isoformat()}")
    print(f"Last timestamp    : {summary.last_timestamp.isoformat()}")
    print(f"Duration          : {format_duration(summary.duration_seconds)}")

    print()
    print("Run reliability:")
    print(f"  Successful iterations: {summary.successful_iterations}")
    print(f"  Failed iterations    : {summary.failed_iterations}")
    print(f"  Success rate         : {fmt_percentage(summary.success_rate)}")
    print(
        "  Max consecutive failures: "
        f"{summary.max_consecutive_failures}"
    )

    if summary.error_type_counts:
        print("  Error types:")

        for error_type, count in sorted(summary.error_type_counts.items()):
            print(f"    {error_type}: {count}")

    print()
    print("Market safety:")
    print(f"  Safe records    : {summary.safe_market_count}")
    print(f"  Unsafe records  : {summary.unsafe_market_count}")
    print(f"  Unknown records : {summary.unknown_market_count}")
    print(f"  Min mid price   : {fmt_decimal(summary.min_mid_price)}")
    print(f"  Max mid price   : {fmt_decimal(summary.max_mid_price)}")

    print()
    print("Market freshness:")
    print(f"  Fresh records   : {summary.fresh_market_count}")
    print(f"  Stale records   : {summary.stale_market_count}")
    print(f"  Unknown records : {summary.unknown_freshness_count}")
    print(
        "  Max exchange age: "
        f"{fmt_seconds(summary.max_exchange_age_seconds)}"
    )
    print(
        "  Max unchanged time: "
        f"{fmt_seconds(summary.max_unchanged_seconds)}"
    )

    if summary.freshness_reason_counts:
        print("  Reasons:")

        for reason, count in sorted(summary.freshness_reason_counts.items()):
            print(f"    {reason}: {count}")

    print()
    print("Trading activity:")
    print(f"  Fills           : {summary.fills_count}")
    print(f"  Orders submitted: {summary.submitted_orders_count}")
    print(f"  Open orders     : {summary.final_open_orders}")
    print(f"  Total volume    : {fmt_decimal(summary.final_total_volume)}")

    print()
    print("Final portfolio:")
    print(f"  Cash            : {fmt_decimal(summary.final_cash_balance)}")
    print(f"  Position        : {fmt_decimal(summary.final_base_position)}")
    print(f"  Equity          : {fmt_decimal(summary.final_equity)}")
    print(f"  Realized PnL    : {fmt_decimal(summary.final_realized_pnl)}")
    print(f"  Unrealized PnL  : {fmt_decimal(summary.final_unrealized_pnl)}")
    print(f"  Max drawdown    : {fmt_decimal(summary.max_drawdown)}")

    print()
    print("Competition:")
    print(f"  Weekly volume   : {fmt_decimal(summary.final_weekly_volume)}")
    print(f"  Estimated score : {fmt_decimal(summary.final_estimated_score)}")
    print(f"  Raffle tickets  : {summary.final_raffle_tickets}")
    print("=" * 72)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Analyze a DreamDEX paper-run JSONL file.",
    )

    parser.add_argument(
        "path",
        type=Path,
        help="Path to a JSONL file produced by PaperRunRecorder.",
    )

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    analyzer = PaperRunAnalyzer()

    try:
        summary = analyzer.analyze_file(args.path)
    except (FileNotFoundError, ValueError) as exc:
        parser.error(str(exc))

    print_summary(
        path=args.path,
        summary=summary,
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
