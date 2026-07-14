"""Run deterministic offline trend stress scenarios for paper trading."""

from __future__ import annotations

from decimal import Decimal

from bot.analytics.trend_stress_scenarios import (
    run_all_scenarios,
    run_fast_sell_off_comparison,
)


def _fmt_decimal(value: Decimal) -> str:
    return format(value, ".4f")


def main() -> int:
    results = run_all_scenarios()
    fast_sell_off_before, fast_sell_off_after = run_fast_sell_off_comparison()
    print("TREND PAPER STRESS SCENARIOS")
    print("(offline deterministic harness; no network or real orders)")
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
    print("FAST_SELL_OFF emergency-exit comparison")
    print(
        "Mode                 Final inventory  Min equity  Max DD    "
        "Latch DD  Overshoot  Risk-exit intents/fills  Open"
    )
    print(
        f"disabled             {_fmt_decimal(fast_sell_off_before.final_base_inventory):>15} "
        f"{_fmt_decimal(fast_sell_off_before.minimum_equity):>11} "
        f"{_fmt_decimal(fast_sell_off_before.maximum_drawdown * Decimal('100')):>7}% "
        f"{_fmt_decimal(fast_sell_off_before.drawdown_at_latch * Decimal('100') if fast_sell_off_before.drawdown_at_latch is not None else None):>7}% "
        f"{_fmt_decimal(fast_sell_off_before.drawdown_overshoot * Decimal('100')):>9}% "
        f"{fast_sell_off_before.risk_exit_intents}/{fast_sell_off_before.risk_exit_fills:>3} "
        f"{fast_sell_off_before.open_orders_after_shutdown:>4}"
    )
    print(
        f"enabled              {_fmt_decimal(fast_sell_off_after.final_base_inventory):>15} "
        f"{_fmt_decimal(fast_sell_off_after.minimum_equity):>11} "
        f"{_fmt_decimal(fast_sell_off_after.maximum_drawdown * Decimal('100')):>7}% "
        f"{_fmt_decimal(fast_sell_off_after.drawdown_at_latch * Decimal('100') if fast_sell_off_after.drawdown_at_latch is not None else None):>7}% "
        f"{_fmt_decimal(fast_sell_off_after.drawdown_overshoot * Decimal('100')):>9}% "
        f"{fast_sell_off_after.risk_exit_intents}/{fast_sell_off_after.risk_exit_fills:>3} "
        f"{fast_sell_off_after.open_orders_after_shutdown:>4}"
    )

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

    overall_pass = all(
        result.invariant_passed
        for result in (*results, fast_sell_off_before, fast_sell_off_after)
    )
    print()
    print(f"Overall result: {'PASS' if overall_pass else 'FAIL'}")
    return 0 if overall_pass else 1


if __name__ == "__main__":
    raise SystemExit(main())
