"""Run deterministic offline trend stress scenarios for paper trading."""

from __future__ import annotations

import argparse
from decimal import Decimal

from bot.analytics.trend_stress_scenarios import (
    run_all_scenarios,
    run_fast_sell_off_comparison,
)


def _fmt_decimal(value: Decimal | None) -> str:
    return "-" if value is None else format(value, ".4f")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run deterministic offline trend stress scenarios"
    )
    parser.add_argument(
        "--fail-on-legacy-breach",
        action="store_true",
        help="return nonzero when the audit-only legacy profile breaches the hard limit",
    )
    args = parser.parse_args(argv)
    results = run_all_scenarios()
    fast_legacy, fast_gap_aware, fast_gap_aware_exit = run_fast_sell_off_comparison()
    print("TREND PAPER STRESS SCENARIOS")
    print("(offline deterministic harness; no network or real orders)")
    print("Scenario execution: COMPLETE")
    print()
    print(
        "Scenario                       Return    Gen  Sub  Rej  Fills  "
        "Buy/Sell  MaxInv  MaxDD   Risk  Fair  Open  Result"
    )
    print("-" * 116)
    for result in results:
        fair = "LATCH" if result.fair_play_latched else "OK"
        risk = "LATCH" if result.portfolio_risk_latched else "OK"
        status = "PASS" if result.invariant_passed else "FAIL"
        print(
            f"{result.scenario:<29} "
            f"{_fmt_decimal(result.market_return * Decimal('100')):>7}% "
            f"{result.generated_orders:>5} {result.submitted_orders:>4} "
            f"{result.rejected_orders:>4} {result.confirmed_fills:>6} "
            f"{result.buy_fills:>3}/{result.sell_fills:<3} "
            f"{_fmt_decimal(result.maximum_base_inventory):>7} "
            f"{_fmt_decimal(result.maximum_drawdown * Decimal('100')):>6}% "
            f"{risk:>5} {fair:>5} {result.open_orders_after_shutdown:>5} "
            f"{status:>6}"
        )

    print()
    print("FAST_SELL_OFF gap-risk comparison")
    print(
        "Mode                 Final inventory  Min equity  Max DD    "
        "Latch DD  Overshoot  Risk-exit intents/fills  Open"
    )
    print(
        f"legacy audit-only    {_fmt_decimal(fast_legacy.final_base_inventory):>15} "
        f"{_fmt_decimal(fast_legacy.minimum_equity):>11} "
        f"{_fmt_decimal(fast_legacy.maximum_drawdown * Decimal('100')):>7}% "
        f"{_fmt_decimal(fast_legacy.drawdown_at_latch * Decimal('100') if fast_legacy.drawdown_at_latch is not None else None):>7}% "
        f"{_fmt_decimal(fast_legacy.drawdown_overshoot * Decimal('100')):>9}% "
        f"{fast_legacy.risk_exit_intents}/{fast_legacy.risk_exit_fills:>3} "
        f"{fast_legacy.open_orders_after_shutdown:>4}"
    )
    print(
        f"gap-aware           {_fmt_decimal(fast_gap_aware.final_base_inventory):>15} "
        f"{_fmt_decimal(fast_gap_aware.minimum_equity):>11} "
        f"{_fmt_decimal(fast_gap_aware.maximum_drawdown * Decimal('100')):>7}% "
        f"{_fmt_decimal(fast_gap_aware.drawdown_at_latch * Decimal('100') if fast_gap_aware.drawdown_at_latch is not None else None):>7}% "
        f"{_fmt_decimal(fast_gap_aware.drawdown_overshoot * Decimal('100')):>9}% "
        f"{fast_gap_aware.risk_exit_intents}/{fast_gap_aware.risk_exit_fills:>3} "
        f"{fast_gap_aware.open_orders_after_shutdown:>4}"
    )
    print(
        f"gap-aware+exit      {_fmt_decimal(fast_gap_aware_exit.final_base_inventory):>15} "
        f"{_fmt_decimal(fast_gap_aware_exit.minimum_equity):>11} "
        f"{_fmt_decimal(fast_gap_aware_exit.maximum_drawdown * Decimal('100')):>7}% "
        f"{_fmt_decimal(fast_gap_aware_exit.drawdown_at_latch * Decimal('100') if fast_gap_aware_exit.drawdown_at_latch is not None else None):>7}% "
        f"{_fmt_decimal(fast_gap_aware_exit.drawdown_overshoot * Decimal('100')):>9}% "
        f"{fast_gap_aware_exit.risk_exit_intents}/{fast_gap_aware_exit.risk_exit_fills:>3} "
        f"{fast_gap_aware_exit.open_orders_after_shutdown:>4}"
    )

    print()
    print("GAP-AWARE DRAWDOWN BUDGET:")
    print(
        "  Policy: adverse long/short=12.00%/12.00%, reserve=1.00%, "
        "exit slippage=2.00%, exit fee=0.20%, max position=15.00% of peak"
    )
    print(
        "  Formula: usable=max(remaining hard headroom - reserve - fees - exit slippage, 0); "
        "safe notional=usable/adverse move"
    )
    for label, result in (
        ("legacy", fast_legacy),
        ("gap-aware", fast_gap_aware),
        ("gap-aware+exit", fast_gap_aware_exit),
    ):
        print(
            f"  {label}: peak={_fmt_decimal(result.peak_equity_before_largest_adverse_step)} "
            f"equity_before={_fmt_decimal(result.equity_before_largest_adverse_step)} "
            f"dd_before={_fmt_decimal((result.drawdown_before_largest_adverse_step or Decimal('0')) * Decimal('100'))}% "
            f"step={_fmt_decimal(result.largest_adverse_step_from_price)}->"
            f"{_fmt_decimal(result.largest_adverse_step_to_price)} "
            f"return={_fmt_decimal((result.largest_adverse_step_return or Decimal('0')) * Decimal('100'))}%"
        )
        print(
            f"    inventory={_fmt_decimal(result.inventory_before_largest_adverse_step)} "
            f"marked_exposure={_fmt_decimal(result.marked_exposure_before_largest_adverse_step)} "
            f"reserved_buy={_fmt_decimal(result.reserved_buy_exposure_before_largest_adverse_step)} "
            f"safe_max={_fmt_decimal(result.maximum_gap_safe_position_notional)} "
            f"fee+slippage={_fmt_decimal(result.fee_slippage_contribution)} "
            f"projected_equity={_fmt_decimal(result.projected_equity_after_largest_adverse_step)} "
            f"projected_dd={_fmt_decimal((result.projected_drawdown_after_largest_adverse_step or Decimal('0')) * Decimal('100'))}%"
        )

    print()
    print("RISK DRAWDOWN CONTROL:")
    for result in (*results, fast_legacy, fast_gap_aware, fast_gap_aware_exit):
        print(
            f"  {result.scenario} [{('risk-exit' if result.risk_exit_enabled else 'normal')}]: "
            f"starting={_fmt_decimal(result.initial_equity)} "
            f"peak={_fmt_decimal(result.peak_equity)} "
            f"current={_fmt_decimal(result.final_equity)} "
            f"drawdown={_fmt_decimal(result.maximum_drawdown * Decimal('100'))}% "
            f"preemptive={_fmt_decimal(result.configured_preemptive_drawdown * Decimal('100'))}% "
            f"hard={_fmt_decimal(result.configured_drawdown_threshold * Decimal('100'))}% "
            f"entry_halt={'YES' if result.entry_halt_latched else 'NO'} "
            f"kill_switch={'YES' if result.portfolio_risk_latched else 'NO'} "
            f"normal_after_latch={result.normal_intents_after_latch} "
            f"risk_exits={result.risk_exit_intents}/{result.risk_exit_fills} "
            f"open={result.open_orders_after_shutdown} "
            f"compliance={result.risk_compliance_status}"
        )
        if result.invariant_failures:
            print("    blockers: " + "; ".join(result.invariant_failures))

    print()
    for result in results:
        print(
            f"{result.scenario}: equity "
            f"{_fmt_decimal(result.initial_equity)} -> "
            f"{_fmt_decimal(result.final_equity)} "
            f"(min {_fmt_decimal(result.minimum_equity)}), "
            f"inventory max/final "
            f"{_fmt_decimal(result.maximum_base_inventory)}/"
            f"{_fmt_decimal(result.final_base_inventory)}, "
            f"exposure max {_fmt_decimal(result.maximum_notional_exposure)}, "
            f"confirmed volume {_fmt_decimal(result.confirmed_volume)}, "
            f"fair-play allowed/rejected "
            f"{result.fair_play_allowed_count}/{result.fair_play_rejected_count}"
        )
        if result.invariant_failures:
            print("  Invariant failures: " + "; ".join(result.invariant_failures))

    required_profiles_pass = all(
        result.invariant_passed
        for result in (*results, fast_gap_aware, fast_gap_aware_exit)
    )
    legacy_gap_breach = any(
        result.hard_limit_gap_breach
        for result in (fast_legacy, fast_gap_aware, fast_gap_aware_exit)
    )
    print()
    print("REQUIRED PROFILE RESULT:")
    print("  Required profiles compliance: " + ("PASS" if required_profiles_pass else "FAIL"))
    print("  Overall required result: " + ("PASS" if required_profiles_pass else "FAIL"))
    print()
    print("LEGACY AUDIT RESULT:")
    print("  Legacy audit execution: COMPLETE")
    print(f"  Legacy audit gap breach present: {'YES' if legacy_gap_breach else 'NO'}")
    print(
        "  Legacy audit compliance: intentionally noncompliant"
        if legacy_gap_breach
        else "  Legacy audit compliance: no breach observed"
    )
    strict_failure = args.fail_on_legacy_breach and legacy_gap_breach
    if args.fail_on_legacy_breach:
        print(
            "  Legacy audit strict mode: "
            + ("FAIL" if strict_failure else "PASS")
        )
    print()
    command_pass = required_profiles_pass and not strict_failure
    print("Overall command result: " + ("PASS" if command_pass else "FAIL"))
    return 1 if not required_profiles_pass or strict_failure else 0


if __name__ == "__main__":
    raise SystemExit(main())
