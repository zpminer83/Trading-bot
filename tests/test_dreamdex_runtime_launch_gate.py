from decimal import Decimal
from dataclasses import FrozenInstanceError

import pytest

from bot.execution.dreamdex_runtime_launch_gate import (
    DreamDexRuntimeLaunchEvidence,
    DreamDexRuntimeLaunchPolicy,
    build_runtime_launch_checklist,
    evaluate_runtime_launch_gate,
)


POOL = "0x035de7403eac6872787779cca7ccf1b4cdb61379"
OWNER = "0x1111111111111111111111111111111111111111"


def _policy():
    return DreamDexRuntimeLaunchPolicy(required_chain_id=5031, required_market_symbol="SOMI:USDso", required_market_address=POOL, required_signer_address=OWNER, maximum_market_data_age_ms=1000, maximum_account_data_age_ms=1000, maximum_orderbook_spread_bps=Decimal("50"), maximum_orderbook_cross_depth=0, minimum_orderbook_depth=1, maximum_order_notional=Decimal("10"), maximum_position_notional=Decimal("100"), maximum_open_orders=2, maximum_daily_loss=Decimal("10"), maximum_drawdown_fraction=Decimal("0.10"), maximum_transaction_fee_wei=1000, maximum_active_intents=2, maximum_active_nonce_reservations=2, maximum_active_signing_leases=2)


def _evidence(**changes):
    values = dict(market_identity_status="confirmed", market_rules_status="confirmed", market_trading_status="enabled", market_data_status="fresh", market_data_age_ms=10, orderbook_status="available", spread_bps=Decimal("5"), orderbook_depth_status="sufficient", account_identity_status="confirmed", account_data_status="fresh", account_data_age_ms=10, balance_status="available", open_order_status="available", open_order_count=0, position_status="within_limits", position_notional=Decimal("0"), daily_pnl_status="within_limit", daily_pnl=Decimal("0"), drawdown_status="within_limit", drawdown_fraction=Decimal("0"), fair_play_status="approved", risk_status="approved", journal_status="clean", journal_recovery_required=False, active_intent_count=0, active_nonce_reservation_count=0, active_signing_lease_count=0, preflight_capability_status="available_offline", signer_capability_status="test_only", submission_capability_status="test_only", confirmation_capability_status="available_offline", source_authority_status="source_confirmed")
    values.update(changes)
    return DreamDexRuntimeLaunchEvidence(**values)


def test_policy_is_frozen_and_requires_explicit_limits():
    policy = _policy()
    assert policy.complete is True
    with pytest.raises(FrozenInstanceError):
        policy.maximum_open_orders = 9
    assert DreamDexRuntimeLaunchPolicy().complete is False


def test_policy_failure_blocks_all_permissions():
    decision = evaluate_runtime_launch_gate(DreamDexRuntimeLaunchPolicy(), _evidence(), synthetic_dependencies_supplied=True)
    assert decision.decision_status == "blocked"
    assert decision.allowed_to_build_intent is False
    assert decision.allowed_to_run_synthetic_dry_run is False
    assert "runtime_launch_policy_incomplete" in decision.blockers


def test_market_and_risk_failures_short_circuit_permissions():
    stale = evaluate_runtime_launch_gate(_policy(), _evidence(market_data_status="stale", market_data_age_ms=5000), synthetic_dependencies_supplied=True)
    assert stale.market_gate_passed is False and stale.allowed_to_build_intent is False
    risk = evaluate_runtime_launch_gate(_policy(), _evidence(drawdown_status="latched", drawdown_fraction=Decimal("0.20")), synthetic_dependencies_supplied=True)
    assert risk.risk_gate_passed is False and risk.allowed_to_run_synthetic_dry_run is False


def test_preemptive_halt_and_latched_kill_switch_block_runtime_start():
    preemptive = evaluate_runtime_launch_gate(
        _policy(),
        _evidence(
            drawdown_fraction=Decimal("0.08"),
            preemptive_drawdown_fraction=Decimal("0.08"),
        ),
        synthetic_dependencies_supplied=True,
    )
    latched = evaluate_runtime_launch_gate(
        _policy(),
        _evidence(kill_switch_latched=True),
        synthetic_dependencies_supplied=True,
    )
    assert preemptive.risk_gate_passed is False
    assert latched.risk_gate_passed is False
    assert preemptive.allowed_to_build_intent is False
    assert latched.allowed_to_run_synthetic_dry_run is False


def test_valid_evidence_allows_only_explicit_synthetic_mode():
    blocked = evaluate_runtime_launch_gate(_policy(), _evidence(), synthetic_dependencies_supplied=False)
    allowed = evaluate_runtime_launch_gate(_policy(), _evidence(), synthetic_dependencies_supplied=True)
    assert blocked.allowed_to_build_intent is True and blocked.allowed_to_run_synthetic_dry_run is False
    assert allowed.allowed_to_run_synthetic_dry_run is True
    assert allowed.allowed_to_invoke_production_signer is False
    assert allowed.allowed_to_submit_real_transaction is False


def test_checklist_keeps_production_capabilities_unavailable():
    decision = evaluate_runtime_launch_gate(_policy(), _evidence(), synthetic_dependencies_supplied=True)
    checklist = {item.area: item for item in build_runtime_launch_checklist(decision)}
    assert {"market", "account", "risk", "fair_play", "journal", "transaction_preflight", "signer", "submission", "confirmation", "reconciliation", "operations", "synthetic_dry_run"} <= set(checklist)
    assert checklist["synthetic_dry_run"].status == "pass"
    assert checklist["signer"].status == "unavailable"
    assert checklist["submission"].status == "unavailable"
    assert checklist["operations"].status == "not_applicable"


def test_production_signer_gate_requires_explicit_keystore_and_unlock_evidence():
    evidence = _evidence(
        encrypted_keystore_status="valid",
        keystore_metadata_status="valid",
        keystore_public_address_match="confirmed",
        secure_secret_provider_status="available",
        keystore_unlock_status="verified",
        production_signer_implementation_status="available",
        production_signer_configured=True,
        production_signer_invocation_status="allowed",
        active_signing_lease_count=1,
    )
    decision = evaluate_runtime_launch_gate(_policy(), evidence, synthetic_dependencies_supplied=False)
    assert decision.production_signer_gate_passed is True
    assert decision.allowed_to_invoke_production_signer is False
    assert decision.allowed_to_submit_real_transaction is False
