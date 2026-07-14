from decimal import Decimal

from bot.analytics.execution_fault_scenarios import run_all_fault_scenarios, run_fault_scenario


def test_all_fault_scenarios_are_offline_and_pass():
    results = run_all_fault_scenarios()
    assert len(results) == 8
    assert all(result.passed for result in results), results


def test_restart_and_unknown_submission_invariants():
    restart = run_fault_scenario("RESTART_WITH_OPEN_ORDER")
    assert restart.passed
    timeout = run_fault_scenario("SUBMIT_TIMEOUT_UNKNOWN")
    assert timeout.passed
    assert timeout.unknown_submission_count >= 1


def test_partial_and_duplicate_fills_change_state_only_once():
    partial = run_fault_scenario("PARTIAL_FILL")
    assert partial.passed
    assert partial.partial_fill_count == 2
    assert partial.final_inventory == Decimal("0.6")
    assert partial.competition_volume == Decimal("6.0")
    duplicate = run_fault_scenario("DUPLICATE_FILL_EVENT")
    assert duplicate.passed
    assert duplicate.duplicate_fill_count == 1
    assert duplicate.competition_volume == Decimal("10")


def test_network_loss_reports_unresolved_and_mismatch_blocks_until_authoritative_state():
    network = run_fault_scenario("NETWORK_LOSS_WITH_OPEN_ORDER")
    assert network.passed
    assert network.unresolved_orders == 1
    mismatch = run_fault_scenario("BALANCE_MISMATCH")
    assert mismatch.passed
    assert mismatch.final_inventory == Decimal("1")

