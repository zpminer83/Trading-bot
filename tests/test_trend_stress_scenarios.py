from decimal import Decimal
from contextlib import redirect_stdout
from dataclasses import replace
from io import StringIO
import socket

from bot.analytics.trend_stress_scenarios import (
    SCENARIO_NAMES,
    build_all_scenarios,
    build_scenario,
    run_all_scenarios,
    run_fast_sell_off_comparison,
    run_scenario,
)
import scripts.run_trend_paper_scenarios as trend_runner


def test_all_scenarios_are_deterministic_and_offline(monkeypatch):
    monkeypatch.setattr(
        socket,
        "create_connection",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("trend stress scenarios must not use the network")
        ),
    )
    first = run_all_scenarios()
    second = run_all_scenarios()

    assert first == second
    assert tuple(result.scenario for result in first) == SCENARIO_NAMES
    assert all(
        result.invariant_passed
        for result in first
        if result.scenario != "FAST_SELL_OFF"
    )
    fast = next(result for result in first if result.scenario == "FAST_SELL_OFF")
    assert fast.hard_limit_gap_breach is False
    assert fast.maximum_drawdown <= Decimal("0.10")
    assert all(result.open_orders_after_shutdown == 0 for result in first)


def test_scenario_shapes_have_valid_spreads_and_expected_directions():
    scenarios = build_all_scenarios()
    by_name = {scenario.name: scenario for scenario in scenarios}

    assert by_name["STEADY_UPTREND"].mid_prices[-1] > by_name["STEADY_UPTREND"].mid_prices[0]
    assert by_name["STEADY_DOWNTREND"].mid_prices[-1] < by_name["STEADY_DOWNTREND"].mid_prices[0]
    assert by_name["FAST_SELL_OFF"].mid_prices[-1] < by_name["FAST_SELL_OFF"].mid_prices[0]
    assert by_name["V_SHAPE_RECOVERY"].mid_prices[-1] == by_name["V_SHAPE_RECOVERY"].mid_prices[0]
    assert by_name["HIGH_VOLATILITY_SIDEWAYS"].mid_prices[-1] == by_name["HIGH_VOLATILITY_SIDEWAYS"].mid_prices[0]
    assert all(scenario.spread > 0 for scenario in scenarios)


def test_fast_sell_off_exercises_existing_portfolio_risk_guard():
    result = run_scenario("FAST_SELL_OFF")

    assert result.portfolio_risk_latched is False
    assert result.maximum_drawdown <= Decimal("0.10")
    assert result.hard_limit_gap_breach is False
    assert result.invariant_passed is True
    assert result.gap_risk_blocked_count > 0
    assert result.inventory_limit_ok is True
    assert result.open_orders_after_shutdown == 0


def test_fast_sell_off_risk_exit_reduces_inventory_without_entry_after_latch():
    legacy, gap_aware, enabled = run_fast_sell_off_comparison()

    assert legacy.risk_exit_enabled is False
    assert legacy.risk_exit_intents == 0
    assert legacy.maximum_drawdown > Decimal("0.10")
    assert legacy.hard_limit_gap_breach is True
    assert legacy.invariant_passed is False
    assert gap_aware.maximum_drawdown <= Decimal("0.10")
    assert gap_aware.invariant_passed is True
    assert enabled.risk_exit_enabled is True
    assert enabled.risk_exit_intents in {0, 1}
    assert enabled.risk_exit_fills in {0, 1}
    assert enabled.final_base_inventory >= Decimal("0")
    assert enabled.drawdown_overshoot >= Decimal("0")
    assert enabled.open_orders_after_shutdown == 0
    assert enabled.maximum_drawdown <= Decimal("0.10")
    assert enabled.invariant_passed is True


def test_confirmed_volume_is_only_from_engine_confirmed_fills():
    result = run_scenario(build_scenario("V_SHAPE_RECOVERY"))

    assert result.confirmed_fills == result.buy_fills + result.sell_fills
    assert result.confirmed_volume > 0
    assert result.rejected_orders >= result.fair_play_rejected_count


def test_unknown_scenario_is_rejected():
    try:
        build_scenario("NOT_A_SCENARIO")
    except ValueError as exc:
        assert "unknown trend stress scenario" in str(exc)
    else:
        raise AssertionError("unknown scenario should fail fast")


def test_runner_default_uses_required_profiles_only_and_keeps_legacy_audit():
    output = StringIO()
    with redirect_stdout(output):
        exit_code = trend_runner.main([])

    text = output.getvalue()
    assert exit_code == 0
    assert "Legacy audit execution: COMPLETE" in text
    assert "Legacy audit gap breach present: YES" in text
    assert "Legacy audit compliance: intentionally noncompliant" in text
    assert "Required profiles compliance: PASS" in text
    assert "Overall required result: PASS" in text


def test_runner_strict_legacy_flag_returns_nonzero():
    output = StringIO()
    with redirect_stdout(output):
        exit_code = trend_runner.main(["--fail-on-legacy-breach"])

    assert exit_code != 0
    assert "Legacy audit strict mode: FAIL" in output.getvalue()


def test_runner_required_profile_breach_returns_nonzero(monkeypatch):
    original_results = trend_runner.run_all_scenarios()
    original_comparison = trend_runner.run_fast_sell_off_comparison()
    broken_results = (
        replace(original_results[0], invariant_passed=False),
        *original_results[1:],
    )
    monkeypatch.setattr(trend_runner, "run_all_scenarios", lambda: broken_results)
    monkeypatch.setattr(
        trend_runner,
        "run_fast_sell_off_comparison",
        lambda: original_comparison,
    )

    output = StringIO()
    with redirect_stdout(output):
        exit_code = trend_runner.main([])

    assert exit_code != 0
    assert "Required profiles compliance: FAIL" in output.getvalue()
