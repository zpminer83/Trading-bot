"""Run the public paper burn-in integrity audit without network access."""
from __future__ import annotations

import argparse
from decimal import Decimal
import sys

from bot.analytics.paper_burn_in_analyzer import PaperBurnInAnalysisSummary, analyze_paper_burn_in


def _value(value):
    if isinstance(value, Decimal):
        return format(value, "f")
    return value


def print_analysis(summary: PaperBurnInAnalysisSummary) -> None:
    integrity = summary.integrity
    market = summary.market_quality
    execution = summary.execution
    risk = summary.risk
    fair = summary.fair_play
    print("PAPER BURN-IN OFFLINE ANALYSIS:")
    print(f"  input file: {summary.input_file}")
    print("  network access used: NO")
    print(f"  integrity: {integrity.status}")
    print(f"  run fingerprint: {integrity.run_fingerprint or 'unavailable'}")
    print(f"  symbol: {integrity.symbol or 'unavailable'}")
    print(f"  duration: {_value(market.duration_seconds)}s")
    print(f"  records: {integrity.record_count}")
    print(f"  sequence valid: {'YES' if integrity.sequence_valid else 'NO'}")
    print(f"  timestamps valid: {'YES' if integrity.timestamps_valid else 'NO'}")
    print(f"  summary counters match: {'YES' if summary.summary_counters_match else 'NO' if summary.summary_counters_match is False else 'UNAVAILABLE'}")
    print(f"  portfolio reconstruction: {summary.portfolio_reconstruction}")
    print(f"  ending equity match: {'YES' if summary.ending_equity_match else 'NO' if summary.ending_equity_match is False else 'UNAVAILABLE'}")
    print(f"  open orders after shutdown: {execution.final_open_orders if execution.final_open_orders is not None else 'unavailable'}")
    print(f"  accepted snapshot ratio: {_value(market.accepted_ratio)}")
    print("  sampling telemetry:")
    for label, value in (
        ("sampling attempts", summary.sampling_attempts),
        ("accepted snapshots", summary.accepted_snapshots),
        ("rejected snapshots", summary.rejected_snapshots),
        ("duplicate rejects", summary.duplicate_rejects),
        ("stale rejects", summary.stale_rejects),
        ("crossed rejects", summary.crossed_rejects),
        ("malformed rejects", summary.malformed_rejects),
        ("transport rejects", summary.transport_rejects),
        ("schema rejects", summary.schema_rejects),
        ("other explicit rejects", summary.other_explicit_rejects),
        ("sampling delay events", summary.sampling_delay_events),
        ("markets endpoint requests", summary.markets_endpoint_requests),
        ("orderbook endpoint requests", summary.orderbook_endpoint_requests),
        ("public requests", summary.total_public_http_requests),
    ):
        print(f"    {label}: {value}")
    print("    reject reasons:")
    for reason, count in sorted(market.reject_reason_counts.items()):
        print(f"      {reason}: {count}")
    print(f"  maximum timestamp gap: {_value(market.largest_timestamp_gap_seconds)}s")
    print(f"  market quality: {market.status}")
    print(f"  strategy activity: {market.book_activity if execution.strategy_intents == 0 else ('ADEQUATE_ACTIVITY' if execution.paper_fills >= 10 and execution.inventory_transitions >= 2 and execution.distinct_executed_prices >= 2 else 'SPARSE_ACTIVITY')}")
    print(f"  strategy fills: {execution.paper_fills}")
    print(f"  inventory transitions: {execution.inventory_transitions}")
    print(f"  portfolio transitions without fill evidence: {summary.portfolio_transitions_without_fill_evidence}")
    print(f"  starting cash/inventory: {_value(summary.starting_cash)} / {_value(summary.starting_inventory)}")
    print(f"  ending cash/inventory: {_value(summary.ending_cash)} / {_value(summary.ending_inventory)}")
    print(f"  risk audit: {risk.status}")
    print(f"  maximum drawdown: {_value(summary.maximum_drawdown)}")
    print(f"  maximum projected shocked drawdown: {_value(summary.maximum_projected_shocked_drawdown)}")
    print(f"  preemptive halt: {'YES' if risk.preemptive_halt else 'NO'}")
    print(f"  hard kill: {'YES' if risk.hard_kill else 'NO'}")
    print(f"  fair-play audit: {fair.status}")
    print(f"  fair-play rejections: {fair.fair_play_rejections}")
    print(f"  fair-play halt: {'YES' if fair.fair_play_halt else 'NO'}")
    print(f"  privacy scan: {summary.privacy_status}")
    print(f"  live order calls: {summary.live_order_calls}")
    print(f"  authenticated calls: {summary.authenticated_calls}")
    print(f"  RPC calls: {summary.rpc_calls}")
    print(f"  mutation RPC calls: {summary.mutation_rpc_calls}")
    print(f"  journal writes: {summary.journal_writes}")
    print(f"  signer calls: {summary.signer_calls}")
    print(f"  submission calls: {summary.submission_calls}")
    print("  authoritative trading status available: NO")
    print("  usable for production readiness: NO")
    print("  Real submission enabled: NO")
    print(f"  result: {summary.result}")
    print(f"  blockers: {', '.join(summary.blockers) or 'none'}")
    print(f"  warnings: {', '.join(summary.warnings) or 'none'}")
    if summary.privacy_findings:
        print("  privacy findings:")
        for category, line_number in summary.privacy_findings:
            print(f"    {category} at line {line_number}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Offline paper burn-in JSONL auditor",
        allow_abbrev=False,
    )
    parser.add_argument("--input", required=True)
    args = parser.parse_args(argv)
    try:
        summary = analyze_paper_burn_in(args.input)
    except (OSError, ValueError) as exc:
        print(f"PAPER BURN-IN OFFLINE ANALYSIS: {type(exc).__name__}")
        print("  network access used: NO")
        print("  result: MALFORMED_INPUT")
        print(f"  blocker: {str(exc).split(':', 1)[0]}")
        return 2
    print_analysis(summary)
    return summary.exit_code


if __name__ == "__main__":
    sys.exit(main())
