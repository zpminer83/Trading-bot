from dataclasses import FrozenInstanceError

import pytest

from bot.execution.dreamdex_execution_approval import (
    InteractiveDreamDexExecutionApprovalProvider,
    UnavailableDreamDexExecutionApprovalProvider,
    build_execution_approval_preview,
    DreamDexExecutionApprovalRegistry,
    consume_execution_approval,
    create_execution_approval_challenge,
    evaluate_execution_approval,
    revalidate_execution_approval,
)
from bot.execution.dreamdex_production_readiness import (
    DreamDexProductionReadinessEvidence,
    DreamDexProductionReadinessPolicy,
    evaluate_production_readiness,
)

MARKET = "0x" + "1" * 40
SIGNER = "0x" + "2" * 40


def _preview(**changes):
    values = dict(operation="place_order", chain_id=5031, market_symbol="SOMI:USDso", market_address=MARKET, signer_address=SIGNER,
                  transaction_type="eip1559", side="BUY", order_type="LIMIT", normalized_price="1.25", normalized_quantity="2", order_notional="2.50",
                  value_wei=0, nonce=7, gas_limit=21000, max_fee_per_gas_wei=3, max_priority_fee_per_gas_wei=1, maximum_total_fee_wei=63000,
                  calldata="0x12345678", unsigned_request_fingerprint="a" * 64, envelope_fingerprint="b" * 64, preflight_fingerprint="c" * 64,
                  intent_fingerprint="d" * 64, reservation_fingerprint="e" * 64, lease_fingerprint="f" * 64, journal_snapshot_fingerprint="g" * 64,
                  market_evidence_fingerprint="h" * 64, account_evidence_fingerprint="i" * 64, risk_decision_fingerprint="j" * 64, fair_play_decision_fingerprint="k" * 64)
    values.update(changes)
    return build_execution_approval_preview(**values)


class _Provider:
    provider_type = "test"
    def __init__(self, response): self.response = response
    def request_approval(self, preview, challenge_display_value, expiry_status): return self.response


def test_readiness_default_is_frozen_and_fail_closed():
    policy = DreamDexProductionReadinessPolicy()
    decision = evaluate_production_readiness(policy, DreamDexProductionReadinessEvidence())
    assert not decision.configuration_ready
    assert not decision.allowed_to_invoke_production_signer
    assert not decision.allowed_to_submit_real_transaction
    with pytest.raises(FrozenInstanceError): policy.required_chain_id = 1


def test_preview_masks_addresses_and_binding_changes_with_nonce_and_calldata():
    first, second, third = _preview(), _preview(nonce=8), _preview(calldata="0x12345679")
    assert first.approval_binding_fingerprint != second.approval_binding_fingerprint != third.approval_binding_fingerprint
    safe = first.safe_dict()
    assert MARKET not in repr(first) and MARKET not in str(safe)
    assert "calldata" not in safe
    assert safe["normalized_price"] == "1.25"


def test_exact_challenge_is_one_attempt_and_not_serialized():
    preview = _preview()
    challenge = create_execution_approval_challenge(preview, issued_monotonic_ms=100, maximum_age_ms=50, challenge_generator=lambda: "APPROVE-TEST-0001")
    evidence = evaluate_execution_approval(challenge=challenge, preview=preview, provider=_Provider("APPROVE-TEST-0001"), now_monotonic_ms=120)
    assert evidence.approved and evidence.attempt_count == 1
    assert "challenge_display_value" not in challenge.safe_dict()
    assert "APPROVE-TEST-0001" not in repr(evidence)


def test_whitespace_case_expiry_and_unavailable_provider_fail_closed():
    preview = _preview()
    challenge = create_execution_approval_challenge(preview, issued_monotonic_ms=100, maximum_age_ms=10, challenge_generator=lambda: "APPROVE-TEST-0001")
    assert not evaluate_execution_approval(challenge=challenge, preview=preview, provider=_Provider(" APPROVE-TEST-0001"), now_monotonic_ms=105).approved
    assert not evaluate_execution_approval(challenge=challenge, preview=preview, provider=_Provider("approve-test-0001"), now_monotonic_ms=105).approved
    assert not evaluate_execution_approval(challenge=challenge, preview=preview, provider=UnavailableDreamDexExecutionApprovalProvider(), now_monotonic_ms=105).approved
    assert evaluate_execution_approval(challenge=challenge, preview=preview, provider=_Provider("APPROVE-TEST-0001"), now_monotonic_ms=111).approval_status == "expired"


def test_revalidation_detects_tampering_without_side_effects():
    preview = _preview()
    challenge = create_execution_approval_challenge(preview, issued_monotonic_ms=100, maximum_age_ms=100, challenge_generator=lambda: "APPROVE-TEST-0001")
    approval = evaluate_execution_approval(challenge=challenge, preview=preview, provider=_Provider("APPROVE-TEST-0001"), now_monotonic_ms=110)
    assert revalidate_execution_approval(approval=approval, original_preview=preview, current_preview=preview, now_monotonic_ms=110).revalidation_status == "confirmed"
    changed = revalidate_execution_approval(approval=approval, original_preview=preview, current_preview=_preview(nonce=8), now_monotonic_ms=110)
    assert changed.revalidation_status == "failed"
    assert "execution_approval_binding_mismatch" in changed.blockers


def test_approval_consumption_is_process_local_and_replay_is_rejected():
    preview = _preview()
    challenge = create_execution_approval_challenge(preview, issued_monotonic_ms=100, maximum_age_ms=100, challenge_generator=lambda: "APPROVE-TEST-0001")
    evidence = evaluate_execution_approval(challenge=challenge, preview=preview, provider=_Provider("APPROVE-TEST-0001"), now_monotonic_ms=110)
    registry = DreamDexExecutionApprovalRegistry()
    consumed = consume_execution_approval(evidence, registry)
    replay = consume_execution_approval(evidence, registry)
    assert consumed.approval_consumed
    assert not replay.approved
    assert "execution_approval_replay_detected" in replay.blockers


def test_interactive_provider_requires_tty_and_does_not_prompt_when_redirected():
    class Stream:
        def isatty(self): return False
    calls = []
    provider = InteractiveDreamDexExecutionApprovalProvider(stdin=Stream(), stdout=Stream(), input_fn=lambda prompt: calls.append(prompt) or "APPROVE-TEST-0001")
    assert provider.request_approval(_preview(), "APPROVE-TEST-0001", "current") is None
    assert calls == []
