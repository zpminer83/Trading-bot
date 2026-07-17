"""Run the deterministic offline DreamDEX place→cancel integration fixture."""
from __future__ import annotations

import argparse
from decimal import Decimal
from pathlib import Path
import tempfile
from typing import Any

from bot.execution.dreamdex_dry_run_orchestrator import (
    DreamDexDryRunDependencies,
    DreamDexEndToEndDryRunResult,
    run_dreamdex_dry_run,
)
from bot.execution.dreamdex_execution_journal import DreamDexExecutionJournalPolicy, initialize_journal
from bot.execution.dreamdex_execution_primitives import deterministic_fingerprint, mask_hex_hash
from bot.execution.dreamdex_runtime_launch_gate import DreamDexRuntimeLaunchEvidence, DreamDexRuntimeLaunchPolicy

CHAIN = 5031
MARKET = "SOMI:USDso"
POOL = "0x035de7403eac6872787779cca7ccf1b4cdb61379"
OWNER = "0x1111111111111111111111111111111111111111"


def _hash(operation: str, nonce: int) -> str:
    return "0x" + deterministic_fingerprint({"operation": operation, "nonce": nonce}, domain="synthetic_transaction")[:64]


def build_synthetic_policy() -> DreamDexRuntimeLaunchPolicy:
    return DreamDexRuntimeLaunchPolicy(required_chain_id=CHAIN, required_market_symbol=MARKET, required_market_address=POOL, required_signer_address=OWNER, maximum_market_data_age_ms=1000, maximum_account_data_age_ms=1000, maximum_orderbook_spread_bps=Decimal("50"), maximum_orderbook_cross_depth=0, minimum_orderbook_depth=1, maximum_order_notional=Decimal("100"), maximum_position_notional=Decimal("1000"), maximum_open_orders=10, maximum_daily_loss=Decimal("100"), maximum_drawdown_fraction=Decimal("0.10"), maximum_transaction_fee_wei=10_000_000, maximum_active_intents=10, maximum_active_nonce_reservations=10, maximum_active_signing_leases=10)


def build_synthetic_evidence(scenario: str = "happy-path") -> DreamDexRuntimeLaunchEvidence:
    values: dict[str, Any] = dict(market_identity_status="confirmed", market_rules_status="confirmed", market_trading_status="enabled", market_data_status="fresh", market_data_age_ms=100, orderbook_status="available", spread_bps=Decimal("5"), orderbook_depth_status="sufficient", account_identity_status="confirmed", account_data_status="fresh", account_data_age_ms=100, balance_status="available", open_order_status="available", open_order_count=0, position_status="within_limits", position_notional=Decimal("0"), daily_pnl_status="within_limit", daily_pnl=Decimal("0"), drawdown_status="within_limit", drawdown_fraction=Decimal("0"), preemptive_drawdown_fraction=Decimal("0.08"), entry_halt_latched=False, kill_switch_latched=False, emergency_exit_requested=False, fair_play_status="approved", risk_status="approved", gap_risk_status="available", gap_risk_budget_approved=True, journal_status="clean", journal_recovery_required=False, active_intent_count=0, active_nonce_reservation_count=0, active_signing_lease_count=0, preflight_capability_status="available_offline", signer_capability_status="test_only", submission_capability_status="test_only", confirmation_capability_status="available_offline", reconciliation_status="complete", source_authority_status="source_confirmed")
    if scenario == "stale-market-data": values.update(market_data_status="stale", market_data_age_ms=5000)
    elif scenario == "crossed-orderbook": values.update(orderbook_status="crossed")
    elif scenario == "spread-too-wide": values.update(spread_bps=Decimal("500"))
    elif scenario == "missing-market-rules": values.update(market_rules_status="unavailable")
    elif scenario == "account-identity-unresolved": values.update(account_identity_status="unresolved")
    elif scenario == "insufficient-balance": values.update(balance_status="insufficient")
    elif scenario == "position-limit-exceeded": values.update(position_notional=Decimal("5000"))
    elif scenario == "daily-loss-exceeded": values.update(daily_pnl_status="exceeded")
    elif scenario == "drawdown-kill-switch": values.update(drawdown_status="latched", drawdown_fraction=Decimal("0.20"))
    elif scenario == "fair-play-cooldown": values.update(fair_play_status="cooldown")
    elif scenario == "journal-recovery-required": values.update(journal_status="recovery", journal_recovery_required=True)
    elif scenario == "active-intent-limit-exceeded": values.update(active_intent_count=20)
    return DreamDexRuntimeLaunchEvidence(**values)


def build_synthetic_dependencies(journal: Any, scenario: str) -> DreamDexDryRunDependencies:
    def fail(name: str) -> None:
        if scenario == name:
            raise RuntimeError(name)

    def unsigned(operation: str, nonce: int) -> dict[str, Any]:
        return {"operation": operation, "nonce": nonce, "market": MARKET, "quantity": "1"}

    def envelope(operation: str, value: Any) -> dict[str, Any]:
        return {"operation": operation, "request": value, "chain_id": CHAIN, "target": POOL}

    def preflight(operation: str, value: Any) -> dict[str, Any]:
        if scenario in {"preflight-chain-mismatch", "target-code-missing", "fee-cap-exceeded"}: raise RuntimeError(scenario)
        return {"status": "resolved", "operation": operation, "nonce": value["request"]["nonce"], "gas_limit": 700000, "fee_wei": 1000}

    def intent(operation: str, value: Any) -> dict[str, Any]:
        created = journal.create_or_get_execution_intent(operation=operation, chain_id=CHAIN, signer_address=OWNER, target_address=POOL, request_fingerprint=deterministic_fingerprint({"operation": operation, "value": value}, domain="synthetic_request"), created_source="test_fixture")
        if not created.intent:
            raise RuntimeError("journal_intent_failed")
        return {"intent_id": created.intent.intent_id, "operation": operation}

    def reserve(operation: str, value: Any) -> dict[str, Any]:
        if scenario == "nonce-mismatch": raise RuntimeError("nonce_mismatch")
        return {"reservation_id": deterministic_fingerprint({"operation": operation}, domain="synthetic_reservation"), "nonce": 100 if operation == "place_order" else 101}

    def lease(operation: str, value: Any) -> dict[str, Any]:
        if scenario == "signing-lease-conflict": raise RuntimeError("signing_lease_conflict")
        return {"lease_id": deterministic_fingerprint({"operation": operation}, domain="synthetic_lease")}

    def sign(operation: str, value: Any) -> dict[str, Any]:
        if scenario == "signer-output-mismatch": raise RuntimeError("signer_output_mismatch")
        return {"transaction_hash": _hash(operation, 100 if operation == "place_order" else 101), "verified": True}

    def submit(operation: str, value: Any) -> dict[str, Any]:
        if scenario == "submission-timeout": raise RuntimeError("submission_timeout")
        if scenario == "rpc-hash-mismatch": return {"transaction_hash": _hash(operation, 999), "accepted": True}
        return {"transaction_hash": value["transaction_hash"], "accepted": True}

    def confirm(operation: str, value: Any) -> dict[str, Any]:
        if scenario == "receipt-reverted": return {"status": "confirmed_reverted"}
        if scenario == "expected-event-missing": return {"status": "confirmed_missing_event"}
        if scenario == "reorg-detected": return {"status": "reorg_detected"}
        if scenario == "cancel-order-id-mismatch" and operation == "cancel_order": return {"status": "confirmed_success", "order_id": 99}
        return {"status": "confirmed_success", "order_id": 42}

    def reconcile(operation: str, value: Any) -> dict[str, Any]:
        if scenario == "final-account-state-incomplete": return {"status": "incomplete"}
        return {"status": "complete", "open_order": operation == "place_order"}

    return DreamDexDryRunDependencies(unsigned, envelope, preflight, intent, reserve, lease, sign, submit, confirm, reconcile)


def _print_result(result: DreamDexEndToEndDryRunResult) -> None:
    print("DREAMDEX SYNTHETIC EXECUTION DRY-RUN")
    print(f"Scenario: {result.scenario_name}")
    print(f"Final state: {result.final_state.value}")
    print(f"Place intent: {mask_hex_hash(result.place_intent_id)}")
    print(f"Place transaction: {mask_hex_hash(result.place_transaction_hash)}")
    print(f"Place order ID confirmed: {'YES' if result.confirmed_order_identity_status == 'confirmed' else 'NO'}")
    print(f"Cancel intent: {mask_hex_hash(result.cancel_intent_id)}")
    print(f"Cancel transaction: {mask_hex_hash(result.cancel_transaction_hash)}")
    print(f"Cancel confirmation: {result.cancel_confirmation_status}")
    print(f"Final open-order state: {result.final_open_order_status}")
    print(f"Journal integrity: {result.journal_integrity_status}")
    print(f"Reconciliation: {result.reconciliation_status}")
    print(f"Signer invocations: {result.signer_invocation_count}")
    print(f"Submission calls: {result.submission_call_count}")
    print(f"Receipt observations: {result.receipt_observation_count}")
    print("Automatic retries: 0")
    print("Replacements: 0")
    print("Live network execution: NO")
    print("Production secret used: NO")
    print(f"Synthetic dry-run passed: {'YES' if result.synthetic_dry_run_passed else 'NO'}")
    print("Production dry-run approved: NO")
    print(f"Ready for production signer integration: {'YES' if result.ready_for_production_signer_integration else 'NO'}")
    print("Ready for real submission: NO")
    print(f"Dry-run fingerprint: {mask_hex_hash(result.dry_run_fingerprint)}")
    print(f"Dry-run blockers: {', '.join(result.blockers) or 'none'}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Offline deterministic DreamDEX place/cancel dry-run")
    parser.add_argument("--scenario", default="happy-path", choices=("happy-path", "stale-market-data", "crossed-orderbook", "spread-too-wide", "missing-market-rules", "account-identity-unresolved", "insufficient-balance", "position-limit-exceeded", "daily-loss-exceeded", "drawdown-kill-switch", "fair-play-cooldown", "journal-recovery-required", "active-intent-limit-exceeded", "preflight-chain-mismatch", "target-code-missing", "fee-cap-exceeded", "nonce-mismatch", "signing-lease-conflict", "signer-output-mismatch", "submission-timeout", "rpc-hash-mismatch", "receipt-reverted", "expected-event-missing", "cancel-order-id-mismatch", "reorg-detected", "final-account-state-incomplete"))
    args = parser.parse_args(argv)
    with tempfile.TemporaryDirectory(prefix="dreamdex-dry-run-") as directory:
        journal = initialize_journal(Path(directory) / "execution.sqlite", DreamDexExecutionJournalPolicy(maximum_active_intents=10, maximum_active_reservations=10))
        try:
            result = run_dreamdex_dry_run(policy=build_synthetic_policy(), evidence=build_synthetic_evidence(args.scenario), dependencies=build_synthetic_dependencies(journal, args.scenario), scenario_name=args.scenario)
            result = DreamDexEndToEndDryRunResult(result.schema_version, result.scenario_name, result.launch_decision, result.final_state, result.stage_results, result.place_intent_id, result.place_transaction_hash, result.confirmed_order_identity_status, result.cancel_intent_id, result.cancel_transaction_hash, result.cancel_confirmation_status, result.final_open_order_status, journal.build_execution_journal_snapshot().integrity_status, result.reconciliation_status, result.signer_invocation_count, result.submission_call_count, result.receipt_observation_count, result.automatic_retry_count, result.replacement_count, result.network_execution_performed, result.production_secret_used, result.dry_run_fingerprint, result.synthetic_dry_run_passed, result.production_dry_run_approved, result.ready_for_production_signer_integration, result.ready_for_real_submission, result.blockers, result.validation_errors)
            _print_result(result)
            return 0 if result.synthetic_dry_run_passed else 1
        finally:
            journal.close()


if __name__ == "__main__":
    raise SystemExit(main())
