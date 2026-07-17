from decimal import Decimal

import pytest

from bot.execution.dreamdex_zero_mutation_rehearsal import (
    DreamDexZeroMutationRehearsalEvidence,
    DreamDexZeroMutationRehearsalPolicy,
    build_rehearsal_candidate,
    collect_live_read_only_rehearsal_evidence,
    run_zero_mutation_rehearsal,
)


def _policy(**kwargs):
    return DreamDexZeroMutationRehearsalPolicy(required_market_symbol="SOMI:USDso", **kwargs)


def _evidence(**kwargs):
    values = dict(market_status="available", account_status="available", rpc_status="available", chain_id=5031,
                  target_code_status="available", pending_nonce_status="available", native_balance_status="available",
                  gas_estimate_status="available", fee_status="available", market_rules_status="available",
                  runtime_gate_status="available", risk_status="available", fair_play_status="available",
                  market_age_ms=1, account_age_ms=1, source_authority="authoritative", network_read_call_count=7,
                  market_identity_status="confirmed", account_identity_status="confirmed", trading_enabled=True,
                  contract_code_present=True, pending_nonce=7, gas_estimate=21000, estimated_fee_wei=1000, native_balance_wei=1000000,
                  gap_risk_status="available", gap_risk_budget_approved=True)
    values.update(drawdown_fraction=Decimal("0"), preemptive_drawdown=Decimal("0.08"), hard_drawdown_limit=Decimal("0.10"))
    values.update(kwargs)
    return DreamDexZeroMutationRehearsalEvidence(**values)


def _candidate(policy):
    return build_rehearsal_candidate(market_symbol="SOMI:USDso", side="BUY", price="1.0000", quantity="1.00",
                                     market_rules={"tick_size": "0.0001", "quantity_step": "0.01", "minimum_quantity": "1", "minimum_notional": "1"},
                                     best_ask="1.0010", policy=policy)


def test_default_policy_is_fail_closed_and_side_effect_flags_false():
    p = _policy()
    assert p.allow_submission is False and p.allow_signing is False and p.allow_approval_prompt is False
    assert p.authoritative is False


def test_policy_rejects_enabling_side_effects():
    with pytest.raises(ValueError):
        _policy(allow_submission=True)


def test_complete_fixture_is_ready_only_for_human_review():
    p = _policy()
    result = run_zero_mutation_rehearsal(policy=p, evidence=_evidence(), candidate=_candidate(p))
    assert result.ready_for_human_review is True
    assert result.ready_for_signing is False and result.ready_for_submission is False
    assert result.mutation_call_count == 0 and result.signer_invocation_count == 0 and result.submission_attempt_count == 0
    assert result.production_journal_write_performed is False


@pytest.mark.parametrize("field", ["market_status", "account_status", "rpc_status", "target_code_status", "pending_nonce_status", "fee_status", "risk_status", "fair_play_status"])
def test_missing_evidence_blocks(field):
    p = _policy()
    result = run_zero_mutation_rehearsal(policy=p, evidence=_evidence(**{field: "unavailable"}), candidate=_candidate(p))
    assert result.readiness_status == "blocked"
    assert result.blockers


def test_stale_evidence_and_wrong_chain_block():
    p = _policy(maximum_market_age_ms=5)
    result = run_zero_mutation_rehearsal(policy=p, evidence=_evidence(market_age_ms=6, chain_id=1), candidate=_candidate(p))
    assert "rpc_chain_mismatch" in result.blockers
    assert "market_evidence_unavailable_or_stale" in result.blockers


def test_candidate_requires_known_rules_and_non_crossing_price():
    p = _policy()
    assert _candidate(p) is not None
    assert build_rehearsal_candidate(market_symbol="SOMI:USDso", side="BUY", price="1.0010", quantity="1.00",
                                     market_rules={"tick_size": "0.0001", "quantity_step": "0.01", "minimum_quantity": "1", "minimum_notional": "1"},
                                     best_ask="1.0010", policy=p) is None
    assert build_rehearsal_candidate(market_symbol="SOMI:USDso", side="BUY", price="1.0000", quantity="1.00",
                                     market_rules={}, best_ask="1.0010", policy=p) is None


def test_read_only_collector_requires_explicit_invocation():
    calls = []
    def collector():
        calls.append(1)
        return _evidence()
    p = _policy()
    result = run_zero_mutation_rehearsal(policy=p, evidence=DreamDexZeroMutationRehearsalEvidence(), candidate=None, collector=collector)
    assert calls == [] and "read_only_collector_unavailable" not in result.blockers
    result = run_zero_mutation_rehearsal(policy=p, evidence=DreamDexZeroMutationRehearsalEvidence(), candidate=_candidate(p), execute_read_only=True, collector=collector)
    assert calls == [1]


def test_safe_diagnostics_do_not_expose_raw_address_or_calldata():
    p = _policy(required_market_address="0x1111111111111111111111111111111111111111", expected_signer_address="0x2222222222222222222222222222222222222222")
    result = run_zero_mutation_rehearsal(policy=p, evidence=_evidence(), candidate=_candidate(p))
    text = repr(p) + repr(result) + str(result.safe_dict())
    assert "0x1111111111111111111111111111111111111111" not in text
    assert "calldata" not in text.lower() and "private" not in text.lower()


def test_evidence_collector_accepts_typed_mapping():
    evidence = collect_live_read_only_rehearsal_evidence(lambda: {"market_status": "available", "network_read_call_count": 1})
    assert evidence.market_status == "available" and evidence.network_read_call_count == 1
