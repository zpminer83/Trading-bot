"""Print a bounded offline fair-play incident reconstruction."""
from __future__ import annotations

import argparse
from decimal import Decimal

from bot.analytics.paper_burn_in_fair_play_incident_analyzer import (
    analyze_paper_burn_in_fair_play_incident,
)


def _value(value):
    if isinstance(value, Decimal):
        return str(value)
    return value


def _mask_fingerprint(value: str | None) -> str:
    """Apply the repository's short hash masking policy for CLI output."""
    if not value:
        return "unavailable"
    return value if value.startswith("<") else (value[:6] + "..." + value[-4:] if len(value) > 10 else "***")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Offline paper burn-in fair-play incident analysis",
        allow_abbrev=False,
    )
    parser.add_argument("--input", required=True)
    args = parser.parse_args(argv)
    try:
        result = analyze_paper_burn_in_fair_play_incident(args.input)
    except (OSError, ValueError) as exc:
        print(f"PAPER BURN-IN FAIR-PLAY INCIDENT ANALYSIS: {type(exc).__name__}")
        print(f"  result: INSUFFICIENT_RECORDED_EVIDENCE")
        print(f"  blocker: {str(exc)}")
        return 2

    print("PAPER BURN-IN FAIR-PLAY INCIDENT ANALYSIS:")
    for label, value in (
        ("input file", result.input_file),
        ("network access used", "YES" if result.network_access_used else "NO"),
        ("integrity", result.integrity),
        ("symbol", result.symbol or "unavailable"),
        ("run fingerprint", _mask_fingerprint(result.run_fingerprint)),
        ("configuration fingerprint", _mask_fingerprint(result.configuration_fingerprint)),
        ("first rejection sequence", result.first_rejection_sequence),
        ("last rejection sequence", result.last_rejection_sequence),
        ("halt sequence", result.halt_sequence),
        ("rejection count", result.rejection_count),
        ("maximum consecutive rejections", result.maximum_consecutive_rejections),
        ("normalized reasons", ", ".join(f"{k}={v}" for k, v in result.normalized_reasons) or "none"),
        ("raw reason codes", ", ".join(f"{k}={v}" for k, v in result.reason_code_counts) or "none"),
        ("dominant reason", result.dominant_reason or "none"),
        ("dominant reason count", result.dominant_reason_count),
        ("rejection reason distribution", ", ".join(f"{k}={v}" for k, v in result.rejection_reason_counts) or "none"),
        ("halt trigger distribution", ", ".join(f"{k}={v}" for k, v in result.halt_trigger_counts) or "none"),
        ("dominant halt trigger", result.dominant_halt_trigger or "none"),
        ("halt trigger", result.halt_trigger or "none"),
        ("halt trigger code", result.halt_trigger_code or "none"),
        ("halt threshold", _value(result.halt_threshold) or "unavailable"),
        ("observed trigger value", _value(result.observed_trigger_value) or "unavailable"),
        ("rejection streak at halt", result.halt_rejection_streak),
        ("paper orders open before halt", result.paper_orders_open_before_halt),
        ("paper orders cancelled by halt", result.paper_orders_cancelled_by_halt),
        ("rejected intents creating orders", result.rejected_intents_creating_orders),
        ("rejected intents creating fills", result.rejected_intents_creating_fills),
        ("normal intents after halt", result.normal_intents_after_halt),
        ("records after halt", result.records_after_halt),
        ("fills before halt", result.fills_before_halt),
        ("ending inventory", _value(result.ending_inventory) or "unavailable"),
        ("ending open orders", result.ending_open_orders),
        ("fair-play enforcement", result.enforcement),
        ("strategy fair-play compatibility", result.strategy_compatibility),
        ("evidence sufficiency", result.evidence_sufficiency),
        ("privacy scan", result.privacy_status),
        ("result", result.result),
        ("blockers", ", ".join(result.blockers) or "none"),
        ("warnings", ", ".join(result.warnings) or "none"),
    ):
        print(f"  {label}: {value}")
    if result.missing_fields:
        print(f"  missing fields: {', '.join(result.missing_fields)}")
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
