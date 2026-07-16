from pathlib import Path

from bot.execution.dreamdex_dry_run_orchestrator import DreamDexDryRunDependencies, DreamDexDryRunState, run_dreamdex_dry_run
from bot.execution.dreamdex_execution_journal import DreamDexExecutionJournalPolicy, initialize_journal
from scripts.run_dreamdex_execution_dry_run import build_synthetic_evidence, build_synthetic_policy, build_synthetic_dependencies


def test_happy_path_is_deterministic_and_offline(tmp_path):
    def run_once():
        journal = initialize_journal(tmp_path / ("journal-" + str(run_once.count) + ".sqlite"), DreamDexExecutionJournalPolicy(maximum_active_intents=10, maximum_active_reservations=10))
        try:
            return run_dreamdex_dry_run(policy=build_synthetic_policy(), evidence=build_synthetic_evidence(), dependencies=build_synthetic_dependencies(journal, "happy-path"))
        finally:
            journal.close()
    run_once.count = 0
    first = run_once(); run_once.count = 1; second = run_once()
    assert first.synthetic_dry_run_passed is True
    assert first.final_state is DreamDexDryRunState.RECONCILIATION_COMPLETE
    assert first.signer_invocation_count == 2
    assert first.submission_call_count == 2
    assert first.automatic_retry_count == 0
    assert first.replacement_count == 0
    assert first.network_execution_performed is False
    assert first.production_secret_used is False
    assert first.production_dry_run_approved is False
    assert first.ready_for_real_submission is False
    assert first.confirmed_order_identity_status == "confirmed"
    assert first.final_open_order_status == "absent"
    assert first.dry_run_fingerprint == second.dry_run_fingerprint


def test_gate_failure_calls_no_dependencies(tmp_path):
    calls = []
    def callback(*args):
        calls.append(args)
        raise AssertionError("dependency should not be called")
    deps = DreamDexDryRunDependencies(callback, callback, callback, callback, callback, callback, callback, callback, callback, callback)
    journal = initialize_journal(tmp_path / "blocked.sqlite", DreamDexExecutionJournalPolicy(maximum_active_intents=10, maximum_active_reservations=10))
    try:
        result = run_dreamdex_dry_run(policy=build_synthetic_policy(), evidence=build_synthetic_evidence("stale-market-data"), dependencies=deps)
    finally:
        journal.close()
    assert result.final_state is DreamDexDryRunState.GATE_REJECTED
    assert calls == []
    assert result.signer_invocation_count == 0
    assert result.submission_call_count == 0


def test_stage_sequence_includes_signing_and_rejects_hash_mismatch(tmp_path):
    journal = initialize_journal(tmp_path / "hash.sqlite", DreamDexExecutionJournalPolicy(maximum_active_intents=10, maximum_active_reservations=10))
    try:
        result = run_dreamdex_dry_run(policy=build_synthetic_policy(), evidence=build_synthetic_evidence(), dependencies=build_synthetic_dependencies(journal, "rpc-hash-mismatch"), scenario_name="rpc-hash-mismatch")
    finally:
        journal.close()
    assert result.synthetic_dry_run_passed is False
    assert result.final_state is DreamDexDryRunState.FAILED
    assert any(stage.stage == "place_order.signing_started" for stage in result.stage_results)
    assert not any(stage.stage == "place_order.confirmation_pending" for stage in result.stage_results)
    assert result.submission_call_count == 1
