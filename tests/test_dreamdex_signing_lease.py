from dataclasses import FrozenInstanceError, replace
from decimal import Decimal
from pathlib import Path
import threading

import pytest

from bot.execution.dreamdex_direct_order_encoding import DreamDexDirectOrderSpecification
from bot.execution.dreamdex_execution_journal import DreamDexExecutionJournalPolicy, initialize_journal, open_journal
from bot.execution.dreamdex_execution_primitives import build_execution_capability_matrix
from bot.execution.dreamdex_readonly_rpc import FixtureDreamDexReadOnlyRpcTransport
from bot.execution.dreamdex_signing_lease import (
    DreamDexLiveNonceEvidence,
    DreamDexLiveNonceRevalidationPolicy,
    DreamDexSigningLease,
    acquire_signing_lease,
    build_signing_lease_preview,
    serialize_signing_lease_diagnostics,
)
from bot.execution.dreamdex_transaction_envelope import DreamDexTransactionEnvelopeEvidence, build_unsigned_transaction_envelope
from bot.execution.dreamdex_transaction_signer import DreamDexTransactionSigningPolicy, build_signing_request
from bot.execution.dreamdex_unsigned_transaction import CHAIN_ID, build_unsigned_place_order_request

OWNER = "0x2222222222222222222222222222222222222222"
POOL = "0x035de7403eac6872787779cca7ccf1b4cdb61379"
WRITE_POLICY = DreamDexExecutionJournalPolicy(maximum_active_intents=20, maximum_active_reservations=20)


def _envelope():
    spec = DreamDexDirectOrderSpecification(
        symbol="SOMI:USDso", side="buy", order_type="limit", price=Decimal("10"), quantity=Decimal("1"),
        time_in_force="gtc", post_only=False, reduce_only=False, deadline=999999999999999999,
        owner_subject=OWNER, signer_subject=OWNER, target_contract=POOL,
        tick_size=Decimal("0.0001"), quantity_step=Decimal("0.01"), minimum_quantity=Decimal("1"), minimum_notional=Decimal("1"),
    )
    request = build_unsigned_place_order_request(spec, chain_id=CHAIN_ID, from_address=OWNER, to_address=POOL, source_confirmed_pool_address=POOL, declared_signer_address=OWNER, value_wei=0, input_asset_kind="erc20")
    evidence = DreamDexTransactionEnvelopeEvidence(source_type="external_manual", source_status="available", chain_id_status="externally_supplied", nonce_status="externally_supplied", gas_limit_status="externally_supplied", transaction_type_status="test_confirmed", fee_status="externally_supplied", priority_fee_status="externally_supplied", max_fee_status="externally_supplied")
    return build_unsigned_transaction_envelope(request, nonce=1, gas_limit=700000, transaction_type="eip1559", max_fee_per_gas_wei=20, max_priority_fee_per_gas_wei=2, evidence=evidence)


def _signing_policy():
    return DreamDexTransactionSigningPolicy(required_signer_address=OWNER, maximum_gas_limit=1_000_000, maximum_total_fee_wei=50_000_000, production_status="test_fixture", unresolved_reasons=())


def _prepared(path: Path):
    journal = initialize_journal(path, WRITE_POLICY)
    envelope = _envelope()
    signing_policy = _signing_policy()
    request = build_signing_request(envelope, signing_policy, signer_address=OWNER)
    created = journal.create_or_get_execution_intent(operation=envelope.operation, chain_id=envelope.chain_id, signer_address=OWNER, target_address=POOL, request_fingerprint=envelope.request_fingerprint, original_envelope_fingerprint=envelope.envelope_fingerprint, finalized_envelope_fingerprint=envelope.envelope_fingerprint, preflight_fingerprint="d" * 64, signing_request_fingerprint=request.signing_request_fingerprint, order_identity_status="unavailable")
    assert created.intent
    journal.transition_execution_intent(created.intent.intent_id, "preflight_validated")
    reserved = journal.reserve_nonce(intent_id=created.intent.intent_id, nonce=envelope.nonce, finalized_envelope_fingerprint=envelope.envelope_fingerprint)
    assert reserved.reservation_id
    journal.transition_execution_intent(created.intent.intent_id, "signing_review_ready")
    return journal, journal.get_execution_intent(created.intent.intent_id), journal.get_nonce_reservation(reserved.reservation_id), envelope, request, signing_policy


def _policy(**overrides):
    values = dict(required_chain_id=CHAIN_ID, required_signer_address=OWNER, maximum_observation_age_ms=10_000)
    values.update(overrides)
    return DreamDexLiveNonceRevalidationPolicy(**values)


def test_models_are_frozen_non_authoritative_and_validate_limits():
    evidence = DreamDexLiveNonceEvidence("1", CHAIN_ID, True, OWNER, 1, True, 10, -3, True, "source_confirmed", True)
    assert evidence.observation_age_ms == 0 and evidence.authoritative is False
    with pytest.raises(FrozenInstanceError):
        evidence.pending_nonce = 2
    assert _policy(maximum_observation_age_ms=0).maximum_observation_age_ms == 0
    with pytest.raises(ValueError):
        _policy(maximum_active_signing_leases_per_signer=0)
    with pytest.raises(ValueError):
        DreamDexLiveNonceEvidence("1", CHAIN_ID, True, OWNER, 1, True, 10, 0, True, "bad", False)


def test_happy_path_uses_exact_chain_then_pending_and_persists_lease(tmp_path):
    journal, intent, reservation, envelope, request, signing_policy = _prepared(tmp_path / "j.sqlite")
    rpc = FixtureDreamDexReadOnlyRpcTransport({"eth_chainId": "0x13a7", "eth_getTransactionCount": "0x1"})
    result = acquire_signing_lease(journal=journal, intent=intent, reservation=reservation, finalized_envelope=envelope, signing_request=request, signing_policy=signing_policy, policy=_policy(), rpc=rpc)
    assert result.status == "acquired"
    assert result.lease_created and result.lease
    assert result.ready_for_signer_invocation is False
    assert result.signer_invocation_allowed is False
    assert result.transaction_submission_allowed is False
    assert result.evidence and result.evidence.pending_nonce == 1
    assert [name for name, _ in rpc.calls] == ["eth_chainId", "eth_getTransactionCount"]
    assert journal.get_execution_intent(intent.intent_id).state == "signing_lease_acquired"
    assert journal.get_events(intent.intent_id)[-1].event_id == result.lease.lease_id
    retry = acquire_signing_lease(journal=journal, intent=journal.get_execution_intent(intent.intent_id), reservation=journal.get_nonce_reservation(reservation.reservation_id), finalized_envelope=envelope, signing_request=request, signing_policy=signing_policy, policy=_policy(), rpc=FixtureDreamDexReadOnlyRpcTransport({"eth_chainId": "0x13a7", "eth_getTransactionCount": "0x1"}))
    assert retry.status == "conflict" and retry.existing_lease_detected and retry.conflict_detected
    journal.close()


def test_chain_mismatch_stops_before_pending_and_does_not_mutate(tmp_path):
    journal, intent, reservation, envelope, request, signing_policy = _prepared(tmp_path / "j.sqlite")
    rpc = FixtureDreamDexReadOnlyRpcTransport({"eth_chainId": "0x1", "eth_getTransactionCount": "0x1"})
    result = acquire_signing_lease(journal=journal, intent=intent, reservation=reservation, finalized_envelope=envelope, signing_request=request, signing_policy=signing_policy, policy=_policy(), rpc=rpc)
    assert result.status == "blocked" and "live_nonce_chain_mismatch" in result.blockers
    assert [name for name, _ in rpc.calls] == ["eth_chainId"]
    assert journal.get_execution_intent(intent.intent_id).state == "signing_review_ready"
    journal.close()


def test_nonce_mismatch_is_fail_closed_without_lease(tmp_path):
    journal, intent, reservation, envelope, request, signing_policy = _prepared(tmp_path / "j.sqlite")
    rpc = FixtureDreamDexReadOnlyRpcTransport({"eth_chainId": "0x13a7", "eth_getTransactionCount": "0x2"})
    result = acquire_signing_lease(journal=journal, intent=intent, reservation=reservation, finalized_envelope=envelope, signing_request=request, signing_policy=signing_policy, policy=_policy(), rpc=rpc)
    assert result.status == "blocked" and result.nonce_match is False
    assert "live_nonce_mismatch" in result.blockers
    assert journal.get_execution_intent(intent.intent_id).state == "signing_review_ready"
    journal.close()


def test_stale_observation_blocks_lease(tmp_path, monkeypatch):
    journal, intent, reservation, envelope, request, signing_policy = _prepared(tmp_path / "j.sqlite")
    import bot.execution.dreamdex_signing_lease as module
    ticks = iter([1000, 2000, 2000])
    monkeypatch.setattr(module, "_now_ms", lambda: next(ticks))
    rpc = FixtureDreamDexReadOnlyRpcTransport({"eth_chainId": "0x13a7", "eth_getTransactionCount": "0x1"})
    result = acquire_signing_lease(journal=journal, intent=intent, reservation=reservation, finalized_envelope=envelope, signing_request=request, signing_policy=signing_policy, policy=_policy(maximum_observation_age_ms=1), rpc=rpc)
    assert result.status == "blocked" and "live_nonce_observation_stale" in result.blockers
    journal.close()


def test_pre_network_validation_rejects_tampering_without_rpc(tmp_path):
    journal, intent, reservation, envelope, request, signing_policy = _prepared(tmp_path / "j.sqlite")
    rpc = FixtureDreamDexReadOnlyRpcTransport({"eth_chainId": "0x13a7", "eth_getTransactionCount": "0x1"})
    tampered = replace(envelope, nonce=2)
    result = acquire_signing_lease(journal=journal, intent=intent, reservation=reservation, finalized_envelope=tampered, signing_request=request, signing_policy=signing_policy, policy=_policy(), rpc=rpc)
    assert result.status == "blocked" and not rpc.calls
    journal.close()


def test_signing_request_not_ready_blocks_before_rpc(tmp_path):
    journal, intent, reservation, envelope, request, signing_policy = _prepared(tmp_path / "j.sqlite")
    not_ready = replace(request, ready_for_signer_invocation=False)
    rpc = FixtureDreamDexReadOnlyRpcTransport({"eth_chainId": "0x13a7", "eth_getTransactionCount": "0x1"})
    result = acquire_signing_lease(journal=journal, intent=intent, reservation=reservation, finalized_envelope=envelope, signing_request=not_ready, signing_policy=signing_policy, policy=_policy(), rpc=rpc)
    assert result.status == "blocked" and "signing_lease_request_not_ready" in result.blockers
    assert not rpc.calls
    journal.close()


def test_second_parallel_lease_attempt_conflicts(tmp_path):
    journal, intent, reservation, envelope, request, signing_policy = _prepared(tmp_path / "j.sqlite")
    rpc_values = {"eth_chainId": "0x13a7", "eth_getTransactionCount": "0x1"}
    results = []
    barrier = threading.Barrier(2)

    def worker():
        barrier.wait()
        rpc = FixtureDreamDexReadOnlyRpcTransport(rpc_values)
        local = open_journal(tmp_path / "j.sqlite", WRITE_POLICY)
        results.append(acquire_signing_lease(journal=local, intent=local.get_execution_intent(intent.intent_id), reservation=local.get_nonce_reservation(reservation.reservation_id), finalized_envelope=envelope, signing_request=request, signing_policy=signing_policy, policy=_policy(), rpc=rpc).status)
        local.close()

    t1 = threading.Thread(target=worker); t2 = threading.Thread(target=worker); t1.start(); t2.start(); t1.join(); t2.join()
    assert sorted(results) == ["acquired", "conflict"]
    journal.close()


def test_recovery_state_and_unresolved_policy_block_before_rpc(tmp_path):
    journal, intent, reservation, envelope, request, signing_policy = _prepared(tmp_path / "j.sqlite")
    journal._require_conn().execute("UPDATE execution_intents SET state='recovery_required' WHERE intent_id=?", (intent.intent_id,))
    rpc = FixtureDreamDexReadOnlyRpcTransport({"eth_chainId": "0x13a7", "eth_getTransactionCount": "0x1"})
    result = acquire_signing_lease(journal=journal, intent=journal.get_execution_intent(intent.intent_id), reservation=reservation, finalized_envelope=envelope, signing_request=request, signing_policy=signing_policy, policy=_policy(unresolved_reasons=("manual_review",)), rpc=FixtureDreamDexReadOnlyRpcTransport({"eth_chainId": "0x13a7", "eth_getTransactionCount": "0x1"}))
    assert result.status == "blocked" and not rpc.calls
    journal.close()


def test_preview_and_diagnostics_are_redacted_and_capabilities_expose_boundary(tmp_path):
    journal, intent, reservation, envelope, request, signing_policy = _prepared(tmp_path / "j.sqlite")
    result = acquire_signing_lease(journal=journal, intent=intent, reservation=reservation, finalized_envelope=envelope, signing_request=request, signing_policy=signing_policy, policy=_policy(), rpc=FixtureDreamDexReadOnlyRpcTransport({"eth_chainId": "0x13a7", "eth_getTransactionCount": "0x1"}))
    preview = build_signing_lease_preview(result)
    text = str(serialize_signing_lease_diagnostics(result, preview=preview))
    assert OWNER not in text and request.signing_request_fingerprint not in text and envelope.calldata_sha256 not in text
    matrix = build_execution_capability_matrix()
    assert matrix.by_name("signing_lease_model").status == "available_offline"
    assert matrix.by_name("revalidate_pending_nonce_live").status == "partial"
    assert matrix.by_name("externally_lock_nonce").status == "unavailable"
    journal.close()
