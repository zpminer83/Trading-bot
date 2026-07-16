from dataclasses import replace
from decimal import Decimal
import json
import pickle

import pytest
from eth_account import Account
from eth_utils import to_checksum_address

from bot.execution.dreamdex_direct_order_encoding import DreamDexDirectOrderSpecification
from bot.execution.dreamdex_execution_journal import DreamDexExecutionJournalPolicy, JournalState, initialize_journal
from bot.execution.dreamdex_readonly_rpc import FixtureDreamDexReadOnlyRpcTransport
from bot.execution.dreamdex_signed_transaction import (
    DreamDexEphemeralSignedTransaction,
    DreamDexSignedTransactionDecodeError,
    DreamDexTransactionSigningMaterial,
    UnavailableDreamDexBoundTransactionSigner,
    build_transaction_signing_material,
    decode_signed_transaction,
    run_transaction_signing_session,
    serialize_signed_transaction_diagnostics,
    verify_signed_transaction,
)
from bot.execution.dreamdex_signing_lease import DreamDexLiveNonceRevalidationPolicy, acquire_signing_lease
from bot.execution.dreamdex_transaction_envelope import DreamDexTransactionEnvelopeEvidence, build_unsigned_transaction_envelope
from bot.execution.dreamdex_transaction_signer import DreamDexTransactionSigningPolicy, build_signing_request
from bot.execution.dreamdex_unsigned_transaction import CHAIN_ID, build_unsigned_place_order_request

OWNER = "0x19e7e376e7c213b7e7e7e46cc70a5dd086daff2a"  # address derived from TEST_KEY
POOL = "0x035de7403eac6872787779cca7ccf1b4cdb61379"
TEST_KEY = "0x" + "11" * 32  # test-only fixture key; never production code
WRITE_POLICY = DreamDexExecutionJournalPolicy(maximum_active_intents=20, maximum_active_reservations=20)


def _envelope(*, transaction_type="legacy", nonce=1, value=0, data=None):
    spec = DreamDexDirectOrderSpecification(
        symbol="SOMI:USDso", side="buy", order_type="limit", price=Decimal("10"), quantity=Decimal("1"),
        time_in_force="gtc", post_only=False, reduce_only=False, deadline=999999999999999999,
        owner_subject=OWNER, signer_subject=OWNER, target_contract=POOL,
        tick_size=Decimal("0.0001"), quantity_step=Decimal("0.01"), minimum_quantity=Decimal("1"), minimum_notional=Decimal("1"),
    )
    request = build_unsigned_place_order_request(spec, chain_id=CHAIN_ID, from_address=OWNER, to_address=POOL, source_confirmed_pool_address=POOL, declared_signer_address=OWNER, value_wei=value, input_asset_kind="erc20")
    if data is not None:
        request = replace(request, calldata=data)
    evidence = DreamDexTransactionEnvelopeEvidence(source_type="external_manual", source_status="available", chain_id_status="externally_supplied", nonce_status="externally_supplied", gas_limit_status="externally_supplied", transaction_type_status="test_confirmed", fee_status="externally_supplied", priority_fee_status="externally_supplied", max_fee_status="externally_supplied")
    if transaction_type == "legacy":
        return build_unsigned_transaction_envelope(request, nonce=nonce, gas_limit=700000, transaction_type="legacy", gas_price_wei=20, evidence=evidence)
    return build_unsigned_transaction_envelope(request, nonce=nonce, gas_limit=700000, transaction_type="eip1559", max_fee_per_gas_wei=20, max_priority_fee_per_gas_wei=2, evidence=evidence)


def _policy():
    return DreamDexTransactionSigningPolicy(required_signer_address=OWNER, maximum_gas_limit=1_000_000, maximum_total_fee_wei=50_000_000, production_status="test_fixture", unresolved_reasons=())


def _prepared(path, *, transaction_type="legacy"):
    journal = initialize_journal(path, WRITE_POLICY)
    envelope = _envelope(transaction_type=transaction_type)
    policy = _policy()
    request = build_signing_request(envelope, policy, signer_address=OWNER)
    created = journal.create_or_get_execution_intent(operation=envelope.operation, chain_id=envelope.chain_id, signer_address=OWNER, target_address=POOL, request_fingerprint=envelope.request_fingerprint, original_envelope_fingerprint=envelope.envelope_fingerprint, finalized_envelope_fingerprint=envelope.envelope_fingerprint, preflight_fingerprint="d" * 64, signing_request_fingerprint=request.signing_request_fingerprint, order_identity_status="unavailable")
    journal.transition_execution_intent(created.intent.intent_id, "preflight_validated")
    reserved = journal.reserve_nonce(intent_id=created.intent.intent_id, nonce=envelope.nonce, finalized_envelope_fingerprint=envelope.envelope_fingerprint)
    journal.transition_execution_intent(created.intent.intent_id, "signing_review_ready")
    intent = journal.get_execution_intent(created.intent.intent_id)
    reservation = journal.get_nonce_reservation(reserved.reservation_id)
    lease = acquire_signing_lease(journal=journal, intent=intent, reservation=reservation, finalized_envelope=envelope, signing_request=request, signing_policy=policy, policy=DreamDexLiveNonceRevalidationPolicy(required_chain_id=CHAIN_ID, required_signer_address=OWNER, maximum_observation_age_ms=10_000), rpc=FixtureDreamDexReadOnlyRpcTransport({"eth_chainId": "0x13a7", "eth_getTransactionCount": "0x1"}))
    assert lease.lease
    material = build_transaction_signing_material(journal=journal, intent=journal.get_execution_intent(intent.intent_id), reservation=journal.get_nonce_reservation(reservation.reservation_id), finalized_envelope=envelope, signing_request=request, lease=lease.lease, signing_policy=policy)
    return journal, material, envelope, request, policy


class _TestOnlySigner:
    def __init__(self, *, transaction_type="legacy", mutate=None):
        self.transaction_type = transaction_type
        self.mutate = mutate or {}
        self.invocations = 0
        self.account = Account.from_key(TEST_KEY)

    def get_address(self):
        return self.account.address

    def describe_capabilities(self):
        from bot.execution.dreamdex_transaction_signer import DreamDexTransactionSignerCapabilities
        return DreamDexTransactionSignerCapabilities(signer_type="test_fixture", address_discovery="test_fixture", transaction_signing="test_fixture", supported_chain_ids=(CHAIN_ID,), supported_transaction_types=("legacy", "eip1559"), production_status="test_fixture", authoritative=False, unresolved_reasons=())

    def sign_finalized_transaction(self, material):
        self.invocations += 1
        envelope = material.finalized_envelope
        tx = {"nonce": self.mutate.get("nonce", envelope.nonce), "to": to_checksum_address(self.mutate.get("to", envelope.to_address)), "value": self.mutate.get("value", envelope.value_wei), "gas": self.mutate.get("gas", envelope.gas_limit), "data": self.mutate.get("data", envelope.calldata), "chainId": self.mutate.get("chain_id", envelope.chain_id)}
        if envelope.transaction_type == "legacy":
            tx["gasPrice"] = self.mutate.get("gas_price", envelope.gas_price_wei)
        else:
            tx["type"] = 2
            tx["maxFeePerGas"] = self.mutate.get("max_fee", envelope.max_fee_per_gas_wei)
            tx["maxPriorityFeePerGas"] = self.mutate.get("max_priority", envelope.max_priority_fee_per_gas_wei)
        signed = Account.sign_transaction(tx, TEST_KEY)
        return DreamDexEphemeralSignedTransaction(signed.raw_transaction, self.mutate.get("reported", self.account.address), self.mutate.get("request_fp", material.signing_request_fingerprint), self.mutate.get("lease_fp", material.lease_fingerprint))


def test_valid_legacy_and_eip1559_decode_and_session(tmp_path):
    for transaction_type in ("legacy", "eip1559"):
        journal, material, _, _, _ = _prepared(tmp_path / f"{transaction_type}.sqlite", transaction_type=transaction_type)
        signer = _TestOnlySigner(transaction_type=transaction_type)
        result = run_transaction_signing_session(journal=journal, material=material, signer=signer)
        assert result.status == "signed"
        assert result.verification and result.verification.verified
        assert result.verification.ready_for_submission is False
        assert result.artifact and result.artifact.ready_for_submission is False
        assert signer.invocations == 1
        assert journal.get_execution_intent(material.intent_id).state == JournalState.SIGNED.value
        events = journal.get_events(material.intent_id)
        assert [event.event_type for event in events][-2:] == ["signing_started", "signed_transaction_verified"]
        assert all(event.details_status != "raw_signed_transaction" for event in events)
        journal.close()


def test_tampering_is_rejected_and_enters_recovery_without_retry(tmp_path):
    journal, material, _, _, _ = _prepared(tmp_path / "tamper.sqlite")
    signer = _TestOnlySigner(mutate={"nonce": 2})
    result = run_transaction_signing_session(journal=journal, material=material, signer=signer)
    assert result.status == "recovery_required"
    assert "signed_transaction_nonce_mismatch" in result.blockers
    assert journal.get_execution_intent(material.intent_id).state == JournalState.RECOVERY_REQUIRED.value
    retry = run_transaction_signing_session(journal=journal, material=material, signer=signer)
    assert retry.status == "blocked"
    assert signer.invocations == 1
    journal.close()


def test_ephemeral_payload_is_redacted_and_not_serializable():
    payload = DreamDexEphemeralSignedTransaction(b"\x01\x02", OWNER, "request", "lease")
    assert b"\x01\x02" not in repr(payload).encode()
    assert "payload=redacted" in str(payload)
    with pytest.raises(TypeError):
        pickle.dumps(payload)
    with pytest.raises(TypeError):
        json.dumps(payload)
    with pytest.raises(TypeError):
        payload.__copy__()
    with pytest.raises(TypeError):
        serialize_signed_transaction_diagnostics(payload)


def test_unavailable_production_signer_never_invokes_or_mutates(tmp_path):
    journal, material, _, _, _ = _prepared(tmp_path / "unavailable.sqlite")
    result = run_transaction_signing_session(journal=journal, material=material, signer=UnavailableDreamDexBoundTransactionSigner())
    assert result.status == "recovery_required"
    assert journal.get_execution_intent(material.intent_id).state == JournalState.RECOVERY_REQUIRED.value
    journal.close()


def test_restart_in_signing_started_blocks_automatic_retry(tmp_path):
    journal, material, _, _, _ = _prepared(tmp_path / "restart.sqlite")
    begun = journal.begin_transaction_signing(intent_id=material.intent_id, lease_id=material.lease_id, chain_id=material.finalized_envelope.chain_id, signer_address=material.finalized_envelope.from_address, finalized_envelope_fingerprint=material.finalized_envelope.envelope_fingerprint, signing_request_fingerprint=material.signing_request_fingerprint)
    assert begun.intent and begun.intent.state == JournalState.SIGNING_STARTED.value
    retry = run_transaction_signing_session(journal=journal, material=material, signer=_TestOnlySigner())
    assert retry.status == "blocked"
    assert retry.signer_invocation_performed is False
    assert journal.get_execution_intent(material.intent_id).state == JournalState.SIGNING_STARTED.value
    journal.close()


def test_decode_malformed_and_unsupported_are_safe():
    with pytest.raises(DreamDexSignedTransactionDecodeError):
        decode_signed_transaction(b"\x01\x02")
    with pytest.raises(DreamDexSignedTransactionDecodeError):
        decode_signed_transaction(b"\x01\xc0")
