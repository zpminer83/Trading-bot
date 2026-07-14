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


def fmt_optional_percentage(value: Decimal | None) -> str:
    if value is None:
        return "n/a"

    return fmt_percentage(value)


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
    print("Portfolio risk:")
    print(f"  Allowed records          : {summary.risk_allowed_count}")
    print(f"  Blocked records          : {summary.risk_blocked_count}")
    print(f"  Unknown records          : {summary.unknown_risk_count}")
    print(f"  Kill switch triggered    : {summary.kill_switch_triggered}")
    print(
        "  Maximum recorded drawdown: "
        f"{fmt_optional_percentage(summary.maximum_recorded_drawdown)}"
    )

    if summary.risk_reason_counts:
        print("  Reasons:")

        for reason, count in sorted(summary.risk_reason_counts.items()):
            print(f"    {reason}: {count}")

    print()
    print("Paper risk exit:")
    print(f"  Risk-exit intents: {summary.risk_exit_intent_count}")
    print(f"  Risk-exit fills  : {summary.risk_exit_fill_count}")
    if summary.risk_exit_reason_counts:
        print("  Reasons:")
        for reason, count in sorted(summary.risk_exit_reason_counts.items()):
            print(f"    {reason}: {count}")

    print()
    print("Passive fill evidence:")
    print(
        "  Evaluated open orders: "
        f"{summary.evaluated_open_orders_count}"
    )
    print(f"  Orders at touch       : {summary.orders_at_touch_count}")
    print(f"  Crossed orders        : {summary.crossed_order_count}")
    print(
        "  Quantity decreases    : "
        f"{summary.level_quantity_decreased_count}"
    )
    print(
        "  Level disappearances  : "
        f"{summary.level_disappeared_count}"
    )
    print(
        "  Maximum order age     : "
        f"{fmt_seconds(summary.maximum_open_order_age_seconds)}"
    )
    print(f"  Confirmed fills       : {summary.fills_count}")
    print(
        "  Note: quantity changes may be caused by trades or cancellations; "
        "they are ambiguous evidence and are not counted as fills."
    )

    print()
    print("Competition fair play:")
    print(f"  Confirmed fill events : {summary.confirmed_fill_event_count}")
    print(f"  Buy fills             : {summary.buy_fill_count}")
    print(f"  Sell fills            : {summary.sell_fill_count}")
    print(
        "  Short-window round trips: "
        f"{summary.short_window_round_trip_count}"
    )
    print(f"  Near-flat cycles      : {summary.near_flat_cycle_count}")
    print(f"  Allowed records       : {summary.fair_play_allowed_count}")
    print(f"  Blocked records       : {summary.fair_play_blocked_count}")
    print(f"  Unknown records       : {summary.unknown_fair_play_count}")
    print(f"  Guard latched         : {summary.fair_play_latched}")
    print(
        "  Blocked intents       : "
        f"{summary.fair_play_blocked_intents_count}"
    )
    print(
        "  Minimum opposite-fill delay: "
        f"{fmt_seconds(summary.minimum_opposite_fill_delay_seconds)}"
    )
    print(
        "  Maximum opposite-fill delay: "
        f"{fmt_seconds(summary.maximum_opposite_fill_delay_seconds)}"
    )

    if summary.fair_play_reason_counts:
        print("  Reason counts:")

        for reason, count in sorted(summary.fair_play_reason_counts.items()):
            print(f"    {reason}: {count}")

    print(
        "  Note: Passing local controls does not guarantee competition "
        "eligibility. The organizer may apply additional undisclosed filters."
    )

    print()
    print("Trade intent audit:")
    print(f"  Generated intents        : {summary.generated_intent_count}")
    print(f"  Submitted intents        : {summary.submitted_intent_count}")
    print(
        "  Fair-play rejected intents: "
        f"{summary.fair_play_rejected_intent_count}"
    )
    print(
        "  Execution rejected intents: "
        f"{summary.execution_rejected_intent_count}"
    )
    print(
        "  Unknown-purpose intents  : "
        f"{summary.unknown_purpose_intent_count}"
    )
    print(
        "  Unknown-purpose fills    : "
        f"{summary.unknown_purpose_fill_count}"
    )
    if summary.generated_intent_purpose_counts:
        print("  Generated purposes:")
        for purpose, count in sorted(summary.generated_intent_purpose_counts.items()):
            print(f"    {purpose}: {count}")
    if summary.confirmed_fill_purpose_counts:
        print("  Confirmed-fill purposes:")
        for purpose, count in sorted(summary.confirmed_fill_purpose_counts.items()):
            print(f"    {purpose}: {count}")

    print()
    print("Order-book signal:")
    print(f"  Bullish records     : {summary.bullish_signal_count}")
    print(f"  Bearish records     : {summary.bearish_signal_count}")
    print(f"  Neutral records     : {summary.neutral_signal_count}")
    print(f"  Warming-up records  : {summary.warming_up_signal_count}")
    print(f"  Unavailable records : {summary.unavailable_signal_count}")
    print(f"  Unknown records     : {summary.unknown_signal_count}")
    print(
        "  Maximum confidence  : "
        f"{fmt_decimal(summary.maximum_signal_confidence)}"
    )
    print(
        "  Average confidence  : "
        f"{fmt_decimal(summary.average_signal_confidence)}"
    )
    print(
        "  Imbalance range     : "
        f"{fmt_decimal(summary.minimum_depth_imbalance)} to "
        f"{fmt_decimal(summary.maximum_depth_imbalance)}"
    )
    print(
        "  Momentum range      : "
        f"{fmt_decimal(summary.minimum_rolling_momentum_bps)} to "
        f"{fmt_decimal(summary.maximum_rolling_momentum_bps)} bps"
    )
    print(
        "  Average spread bps  : "
        f"{fmt_decimal(summary.average_spread_bps)}"
    )
    if summary.signal_reason_counts:
        print("  Reason counts:")
        for reason, count in sorted(summary.signal_reason_counts.items()):
            print(f"    {reason}: {count}")
    print(
        "  Signal confidence is an uncalibrated diagnostic score and is not "
        "an estimated probability of profit."
    )

    print()
    print("Order-book depth structure:")
    print(f"  Positive / negative L1 imbalance: {summary.positive_imbalance_l1_count} / {summary.negative_imbalance_l1_count}")
    print(f"  Positive / negative L5 imbalance: {summary.positive_imbalance_l5_count} / {summary.negative_imbalance_l5_count}")
    print(f"  Positive L1 with negative L5    : {summary.l1_positive_l5_negative_count}")
    print(f"  Negative L1 with positive L5    : {summary.l1_negative_l5_positive_count}")
    print(f"  L1 / microprice sign inconsistencies: {summary.sign_consistency_failure_count}")
    print(f"  Average L1 imbalance             : {fmt_decimal(summary.average_imbalance_l1)}")
    print(f"  Average L5 imbalance             : {fmt_decimal(summary.average_imbalance_l5)}")
    print(f"  Average bid L2-L5 concentration  : {fmt_decimal(summary.average_bid_depth_concentration_l2_to_l5)}")
    print(f"  Average ask L2-L5 concentration  : {fmt_decimal(summary.average_ask_depth_concentration_l2_to_l5)}")
    print(
        "  Note: displayed depth can reflect cancellations or spoof-like "
        "behavior and is not proof of future trading direction."
    )

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
