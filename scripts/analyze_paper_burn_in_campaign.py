"""Print an offline aggregate audit for explicitly supplied burn-in files."""
from __future__ import annotations

import argparse
from decimal import Decimal
import sys

from bot.analytics.paper_burn_in_campaign_analyzer import (
    PaperBurnInCampaignSummary,
    analyze_paper_burn_in_campaign,
)


def _v(value):
    if isinstance(value, Decimal):
        return format(value, "f")
    return value if value is not None else "unavailable"


def print_analysis(summary: PaperBurnInCampaignSummary) -> None:
    market = summary.market
    execution = summary.execution
    risk = summary.risk
    fair = summary.fair_play
    print("PAPER BURN-IN CAMPAIGN ANALYSIS:")
    print(f"  input files: {', '.join(summary.input_files)}")
    print("  network access used: NO")
    print(f"  total input runs: {len(summary.runs)}")
    print(f"  qualifying runs: {summary.qualifying_run_count}")
    print(f"  failed runs: {summary.failed_run_count}")
    print(f"  insufficient-evidence runs: {summary.insufficient_evidence_run_count}")
    print(f"  symbol: {summary.symbol or 'unavailable'}")
    print(f"  total duration: {_v(market.total_duration_seconds)}s")
    print(f"  total records: {market.total_records}")
    print(f"  total accepted snapshots: {market.total_accepted_snapshots}")
    print(f"  total rejected snapshots: {market.total_rejected_snapshots}")
    print(f"  accepted snapshot ratio: {_v(market.accepted_ratio)}")
    print(f"  healthy market runs: {market.healthy_runs}")
    print(f"  degraded market runs: {market.degraded_runs}")
    print(f"  invalid market runs: {market.invalid_runs}")
    print(f"  stale/crossed/malformed: {market.stale_count}/{market.crossed_count}/{market.malformed_count}")
    print(f"  maximum consecutive failures: {market.maximum_consecutive_failures}")
    print(f"  maximum timestamp gap: {_v(market.maximum_timestamp_gap_seconds)}s")
    print(f"  median of run median gaps: {_v(market.median_run_median_gap_seconds)}s")
    print(f"  maximum mid-price step: {_v(market.maximum_mid_price_step)}")
    print(f"  spread min/median/max: {_v(market.minimum_spread)}/{_v(market.median_spread)}/{_v(market.maximum_spread)}")
    print(f"  distinct mid/bid/ask levels: {market.distinct_mid_price_count}/{market.distinct_bid_level_count}/{market.distinct_ask_level_count}")
    print(f"  total strategy intents: {execution.total_strategy_intents}")
    print(f"  total risk-approved intents: {execution.total_risk_approved_intents}")
    print(f"  total risk-rejected intents: {execution.total_risk_rejected_intents}")
    print(f"  total fair-play rejections: {fair.fair_play_rejection_count}")
    print(f"  fair-play rejection ratio: {_v(fair.reason_ratio_to_intents)}")
    dominant = max(fair.reason_counts, key=fair.reason_counts.get) if fair.reason_counts else "none"
    print(f"  dominant fair-play reason: {dominant}")
    print(f"  maximum consecutive fair-play rejections: {fair.maximum_consecutive_rejections}")
    print(f"  fair-play rejection clustering: {'YES' if fair.rejection_clustering else 'NO'}")
    print(f"  fair-play affected runs: {fair.affected_runs}")
    print(f"  total paper orders: {execution.total_paper_orders}")
    print(f"  total cancels: {execution.total_cancels}")
    print(f"  total replacements: {execution.total_replacements}")
    print(f"  total fills: {execution.total_fills}")
    print(f"  fills per hour: {_v(execution.fills_per_hour)}")
    print(f"  runs with fills: {execution.runs_with_fills}")
    print(f"  distinct fill timestamps: {execution.distinct_fill_timestamps}")
    print(f"  distinct executed price levels: {execution.distinct_executed_price_levels}")
    print(f"  inventory transitions: {execution.inventory_transitions}")
    print(f"  maximum absolute inventory: {_v(execution.maximum_abs_inventory)}")
    print(f"  intent-to-fill ratio: {_v(execution.intent_to_fill_ratio)}")
    print(f"  order-to-fill ratio: {_v(execution.order_to_fill_ratio)}")
    print(f"  strategy activity: {execution.strategy_activity}")
    print(f"  total net PnL: {_v(summary.total_net_pnl)}")
    print(f"  total fees: {_v(summary.total_fees)}")
    print(f"  mean fees per run: {_v(summary.mean_fees)}")
    print(f"  mean run PnL: {_v(summary.mean_run_pnl)}")
    print(f"  median run PnL: {_v(summary.median_run_pnl)}")
    print(f"  maximum observed drawdown: {_v(risk.maximum_observed_drawdown)}")
    print(f"  maximum projected shocked drawdown: {_v(risk.maximum_projected_shocked_drawdown)}")
    print(f"  maximum reserved exposure: {_v(risk.maximum_reserved_exposure)}")
    print(f"  maximum absolute inventory: {_v(risk.maximum_abs_inventory)}")
    print(f"  preemptive halts: {risk.preemptive_halt_count}")
    print(f"  hard kills: {risk.hard_kill_count}")
    print(f"  fair-play halts: {risk.fair_play_halt_count}")
    print("  open orders after all shutdowns: " + ", ".join("0" if value in (0, "0") else str(value) for value in risk.final_open_orders_per_run) if risk.final_open_orders_per_run else "  open orders after all shutdowns: unavailable")
    print("  configured adverse move assumption: 12%")
    print("  guarantee beyond configured gap: NO")
    print(f"  integrity: {summary.integrity_status}")
    print(f"  market quality: {summary.market_quality_status}")
    print(f"  risk audit: {summary.risk_status}")
    print(f"  fair-play audit: {summary.fair_play_status}")
    print(f"  fair-play enforcement: {fair.enforcement_status}")
    print(f"  strategy fair-play compatibility: {fair.compatibility_status}")
    print(f"  enforcement-failed runs: {fair.enforcement_fail_runs}")
    print(f"  compatibility-failed runs: {fair.compatibility_fail_runs}")
    print(f"  privacy audit: {summary.privacy_status}")
    print(f"  live order calls: {summary.live_order_calls}")
    print(f"  authenticated calls: {summary.authenticated_calls}")
    print(f"  RPC calls: {summary.rpc_calls}")
    print(f"  mutation RPC calls: {summary.mutation_rpc_calls}")
    print(f"  journal writes: {summary.journal_writes}")
    print(f"  signer calls: {summary.signer_calls}")
    print(f"  submission calls: {summary.submission_calls}")
    print("  authoritative trading status available: NO")
    print("  usable for production readiness: NO")
    print(f"  Real submission enabled: {'YES' if summary.real_submission_enabled else 'NO'}")
    print(f"  result: {summary.result}")
    print(f"  blockers: {', '.join(summary.blockers) or 'none'}")
    print(f"  warnings: {', '.join(summary.warnings) or 'none'}")
    print("  run results:")
    for run in summary.runs:
        print(f"    {run.input_file}: {run.result}; qualifying={'YES' if run.qualifying else 'NO'}; run_fp={run.fingerprint or 'unavailable'}; config_fp={run.configuration_fingerprint or 'unavailable'}; fills={run.analysis.execution.paper_fills}; pnl={_v(run.absolute_pnl)}; fees={_v(run.fees)}; ending_inventory={_v(run.ending_inventory)}; market={run.analysis.market_quality.status}; risk={run.analysis.risk.status}; fair_enforcement={run.fair_play_enforcement}; fair_compatibility={run.fair_play_compatibility}")
    if fair.reason_counts:
        print("  fair-play reasons:")
        for reason, count in sorted(fair.reason_counts.items()):
            print(f"    {reason}: {count}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Offline paper burn-in campaign analyzer", allow_abbrev=False)
    parser.add_argument("--input", action="append", required=True)
    args = parser.parse_args(argv)
    try:
        summary = analyze_paper_burn_in_campaign(args.input)
    except (OSError, ValueError) as exc:
        print("PAPER BURN-IN CAMPAIGN ANALYSIS:")
        print("  network access used: NO")
        print("  result: MALFORMED_INPUT")
        print(f"  blocker: {str(exc).split(':', 1)[0]}")
        return 2
    print_analysis(summary)
    return summary.exit_code


if __name__ == "__main__":
    sys.exit(main())
