from dataclasses import FrozenInstanceError
from decimal import Decimal

import pytest

from bot.execution.dreamdex_direct_order_encoding import ORDER_CANCELLED_TOPIC, ORDER_PLACED_TOPIC, DreamDexDirectOrderSpecification
from bot.execution.dreamdex_transaction_envelope import CHAIN_ID, DreamDexTransactionEnvelopeEvidence, build_unsigned_transaction_envelope
from bot.execution.dreamdex_unsigned_transaction import build_unsigned_place_order_request
from bot.execution.dreamdex_transaction_lifecycle import (
    DreamDexExternalSubmissionEvidence,
    DreamDexTransactionEventEvidence,
    DreamDexTransactionReceiptEvidence,
    DreamDexTransactionReplacementEvidence,
    DreamDexTransactionLifecycleEvidence,
    apply_dropped_state,
    apply_receipt_evidence,
    apply_replacement_evidence,
    build_transaction_lifecycle_preview,
    compute_event_fingerprint,
    compute_lifecycle_fingerprint,
    compute_receipt_fingerprint,
    create_prepared_lifecycle,
    describe_transaction_lifecycle_capabilities,
    import_external_submission,
    serialize_transaction_lifecycle_diagnostics,
    transition_transaction_lifecycle,
    validate_event_evidence,
    validate_external_submission_evidence,
    validate_receipt_evidence,
    validate_state_transition,
)


POOL = "0x1111111111111111111111111111111111111111"
OWNER = "0x2222222222222222222222222222222222222222"
TX = "0x" + "a" * 64
TX2 = "0x" + "b" * 64
BLOCK = "0x" + "c" * 64


def envelope():
    spec = DreamDexDirectOrderSpecification(
        symbol="SOMI:USDso", side="buy", order_type="limit", price=Decimal("10"), quantity=Decimal("1"),
        time_in_force="gtc", post_only=False, reduce_only=False, deadline=999999999999999999,
        owner_subject=OWNER, signer_subject=OWNER, target_contract=POOL,
        tick_size=Decimal("0.0001"), quantity_step=Decimal("0.01"), minimum_quantity=Decimal("1"), minimum_notional=Decimal("1"),
    )
    request = build_unsigned_place_order_request(
        spec, chain_id=CHAIN_ID, from_address=OWNER, to_address=POOL,
        source_confirmed_pool_address=POOL, declared_signer_address=OWNER,
        value_wei=0, input_asset_kind="erc20",
    )
    return build_unsigned_transaction_envelope(
        request, nonce=0, gas_limit=700000, transaction_type="eip1559",
        max_fee_per_gas_wei=20, max_priority_fee_per_gas_wei=2,
        evidence=DreamDexTransactionEnvelopeEvidence(source_type="test_fixture", source_status="observed", nonce_status="test_confirmed", gas_limit_status="test_confirmed", transaction_type_status="test_confirmed", fee_status="test_confirmed"),
    )


def submission(env, tx=TX):
    return DreamDexExternalSubmissionEvidence(
        transaction_hash=tx, submission_channel="test_fixture", source_type="test_fixture", source_status="observed",
        signer_address=OWNER, chain_id=CHAIN_ID, target_address=POOL,
        request_fingerprint=env.request_fingerprint, envelope_fingerprint=env.envelope_fingerprint,
    )


def receipt(status="success", tx=TX, **overrides):
    values = dict(transaction_hash=tx, block_hash=BLOCK, block_number=10, transaction_index=1, status=status, from_address=OWNER, to_address=POOL, gas_used=100000, effective_gas_price=2, logs_count=1, evidence_source="test_fixture")
    values.update(overrides)
    return DreamDexTransactionReceiptEvidence(**values)


def placed_event(tx=TX, **overrides):
    values = dict(event_name="OrderPlaced", event_signature=None, topic0=ORDER_PLACED_TOPIC, transaction_hash=tx, block_number=10, log_index=0, contract_address=POOL, order_id=7, owner_address=OWNER, raw_topics_sha256="d" * 64, raw_data_sha256="e" * 64, raw_topics_count=2, raw_data_length=0, source_status="source_confirmed")
    values.update(overrides)
    return DreamDexTransactionEventEvidence(**values)


def test_models_are_frozen_and_prepared_lifecycle_is_fail_closed():
    record = create_prepared_lifecycle(envelope(), lifecycle_id="l1")
    assert record.current_state == "prepared"
    assert record.authoritative is False
    assert record.reconciliation_status == "incomplete"
    with pytest.raises(FrozenInstanceError):
        record.current_state = "externally_submitted"
    unavailable = create_prepared_lifecycle(None)
    assert unavailable.current_state == "unavailable"
    assert "transaction_envelope_unavailable" in unavailable.blockers


def test_submission_import_requires_matching_external_metadata():
    env = envelope()
    record = create_prepared_lifecycle(env, lifecycle_id="l2")
    imported = import_external_submission(env, submission(env), lifecycle_id="l2")
    assert imported.current_state == "externally_submitted"
    assert imported.transaction_hash == TX
    assert imported.request_fingerprint == env.request_fingerprint
    bad = submission(env)
    bad = DreamDexExternalSubmissionEvidence(**{**bad.__dict__, "target_address": OWNER})
    result = validate_external_submission_evidence(bad, envelope=env)
    assert "submission_target_mismatch" in result.errors
    with pytest.raises(ValueError):
        import_external_submission(env, bad)


def test_state_machine_accepts_only_explicit_external_progression():
    env = envelope()
    prepared = create_prepared_lifecycle(env)
    assert not validate_state_transition("prepared", "externally_signed").allowed
    signed = transition_transaction_lifecycle(prepared, "externally_signed", external_signing_confirmed=True)
    submitted = transition_transaction_lifecycle(signed, "externally_submitted", submission_evidence=submission(env))
    pending = transition_transaction_lifecycle(submitted, "pending_external_confirmation")
    assert pending.current_state == "pending_external_confirmation"
    assert pending.request_fingerprint == env.request_fingerprint
    assert pending.envelope_fingerprint == env.envelope_fingerprint
    assert not validate_state_transition("prepared", "confirmed_success").allowed
    assert not validate_state_transition("confirmed_success", "pending_external_confirmation").allowed


def test_receipt_and_event_confirmation_requires_matching_evidence():
    env = envelope()
    submitted = import_external_submission(env, submission(env))
    valid_receipt = validate_receipt_evidence(receipt(), transaction_hash=TX, envelope=env)
    assert valid_receipt.valid
    valid_event = validate_event_evidence(placed_event(), operation="place_order", transaction_hash=TX, expected_pool=POOL, expected_order_id=7, expected_owner=OWNER)
    assert valid_event.valid
    confirmed = apply_receipt_evidence(submitted, receipt(), (placed_event(),))
    assert confirmed.current_state == "confirmed_success"
    assert confirmed.order_id == 7
    assert confirmed.receipt_evidence is not None
    assert confirmed.event_evidence[0].event_name == "OrderPlaced"
    assert confirmed.reconciliation_status == "incomplete"
    assert "transaction_lifecycle_non_authoritative" in confirmed.blockers
    mismatch = apply_receipt_evidence(submitted, receipt(from_address=POOL), (placed_event(),))
    assert mismatch.current_state == "unknown_external_state"


def test_success_receipt_without_required_event_is_not_success():
    submitted = import_external_submission(envelope(), submission(envelope()))
    missing = apply_receipt_evidence(submitted, receipt(), ())
    assert missing.current_state == "confirmed_missing_required_event"
    assert missing.order_id is None
    assert "transaction_event_evidence_unavailable" in missing.blockers
    with pytest.raises(ValueError):
        transition_transaction_lifecycle(missing, "confirmed_success", receipt_evidence=receipt())
    recovered = apply_receipt_evidence(missing, receipt(), (placed_event(),))
    assert recovered.current_state == "confirmed_success"


def test_reverted_receipt_is_confirmed_reverted_and_success_cannot_regress():
    submitted = import_external_submission(envelope(), submission(envelope()))
    reverted = apply_receipt_evidence(submitted, receipt("reverted"), ())
    assert reverted.current_state == "confirmed_reverted"
    with pytest.raises(ValueError):
        transition_transaction_lifecycle(reverted, "confirmed_success", receipt_evidence=receipt())


def test_cancel_event_and_wrong_event_are_validated_by_exact_topic():
    event = DreamDexTransactionEventEvidence(event_name="OrderCancelled", topic0=ORDER_CANCELLED_TOPIC, transaction_hash=TX, contract_address=POOL, order_id=7, source_status="source_confirmed")
    assert validate_event_evidence(event, operation="cancel_order", transaction_hash=TX, expected_pool=POOL, expected_order_id=7).valid
    wrong = DreamDexTransactionEventEvidence(event_name="OrderCancelled", topic0=ORDER_PLACED_TOPIC, transaction_hash=TX, contract_address=POOL, order_id=7, source_status="source_confirmed")
    assert "event_signature_or_topic_mismatch" in validate_event_evidence(wrong, operation="cancel_order", transaction_hash=TX, expected_pool=POOL).errors
    assert "reduce_event_semantics_unavailable" in validate_event_evidence(event, operation="reduce_order", transaction_hash=TX, expected_pool=POOL).errors


def test_receipt_validation_rejects_hash_addresses_status_and_numeric_faults():
    env = envelope()
    assert "receipt_transaction_hash_mismatch" in validate_receipt_evidence(receipt(tx=TX2), transaction_hash=TX, envelope=env).errors
    assert "receipt_to_mismatch" in validate_receipt_evidence(receipt(to_address=OWNER), transaction_hash=TX, envelope=env).errors
    assert "receipt_status_unavailable" in validate_receipt_evidence(receipt(status="unavailable"), transaction_hash=TX, envelope=env).errors
    with pytest.raises(ValueError):
        DreamDexTransactionReceiptEvidence(transaction_hash=TX, block_number=-1)
    with pytest.raises(ValueError):
        DreamDexTransactionReceiptEvidence(transaction_hash=TX, block_number=True)
    with pytest.raises(ValueError):
        DreamDexTransactionReceiptEvidence(transaction_hash=TX, block_hash="0x" + "0" * 64)


def test_hash_canonicalization_and_zero_or_malformed_rejection():
    assert DreamDexTransactionReceiptEvidence(transaction_hash=TX.upper()).transaction_hash == TX
    for value in (True, "", "0x1234", "0x" + "0" * 64):
        with pytest.raises(ValueError):
            DreamDexTransactionReceiptEvidence(transaction_hash=value)
    replacement = DreamDexTransactionReplacementEvidence(TX, TX2, replacement_reason="fee_bump", source_type="test_fixture")
    assert replacement.original_transaction_hash == TX
    with pytest.raises(ValueError):
        DreamDexTransactionReplacementEvidence(TX, TX)


def test_replacement_and_drop_do_not_transfer_old_receipt_or_order_id():
    submitted = import_external_submission(envelope(), submission(envelope()))
    replacement = DreamDexTransactionReplacementEvidence(TX, TX2, replacement_reason="fee_bump", nonce_match_status="confirmed", from_match_status="confirmed", chain_match_status="confirmed", source_type="test_fixture", source_status="observed")
    replaced = apply_replacement_evidence(submitted, replacement)
    assert replaced.current_state == "replaced_external"
    assert replaced.transaction_hash == TX
    assert replaced.replacement_transaction_hash == TX2
    assert replaced.receipt_evidence is None
    assert replaced.order_id is None
    with pytest.raises(ValueError):
        transition_transaction_lifecycle(replaced, "confirmed_success", receipt_evidence=receipt(tx=TX2))
    dropped = apply_dropped_state(submitted)
    assert dropped.current_state == "dropped_external"
    with pytest.raises(ValueError):
        transition_transaction_lifecycle(dropped, "confirmed_success", receipt_evidence=receipt())


def test_unknown_state_requires_incomplete_or_conflicting_external_data():
    assert not validate_state_transition("prepared", "unknown_external_state").allowed
    assert validate_state_transition("prepared", "unknown_external_state", incomplete_external_evidence=True).allowed
    unknown = transition_transaction_lifecycle(create_prepared_lifecycle(envelope()), "unknown_external_state", incomplete_external_evidence=True)
    assert unknown.current_state == "unknown_external_state"


def test_fingerprints_are_deterministic_and_change_with_evidence_or_state():
    r1 = receipt()
    r2 = receipt()
    e1 = placed_event()
    e2 = placed_event(order_id=8)
    assert compute_receipt_fingerprint(r1) == compute_receipt_fingerprint(r2)
    assert compute_event_fingerprint(e1) != compute_event_fingerprint(e2)
    first = create_prepared_lifecycle(envelope())
    second = create_prepared_lifecycle(envelope())
    assert first.lifecycle_fingerprint == second.lifecycle_fingerprint
    assert compute_lifecycle_fingerprint(schema_version="1", operation="place_order", request_fingerprint="a", envelope_fingerprint="b", transaction_hash=TX, current_state="prepared", previous_state=None, receipt_fingerprint=None, event_fingerprint=None, order_id=None, replacement_transaction_hash=None, evidence_source="test_fixture", evidence_status="observed") != compute_lifecycle_fingerprint(schema_version="1", operation="place_order", request_fingerprint="a", envelope_fingerprint="b", transaction_hash=TX, current_state="externally_submitted", previous_state="prepared", receipt_fingerprint=None, event_fingerprint=None, order_id=None, replacement_transaction_hash=None, evidence_source="test_fixture", evidence_status="observed")


def test_lifecycle_diagnostics_redact_sensitive_values_and_expose_safe_evidence():
    submitted = import_external_submission(envelope(), submission(envelope()))
    confirmed = apply_receipt_evidence(submitted, receipt(), (placed_event(),))
    diagnostics = serialize_transaction_lifecycle_diagnostics(confirmed)
    rendered = repr(confirmed) + repr(build_transaction_lifecycle_preview(confirmed)) + str(diagnostics)
    assert TX not in rendered
    assert OWNER not in rendered
    assert POOL not in rendered
    assert '"raw_topics":' not in rendered
    assert '"raw_data":' not in rendered
    assert diagnostics["transaction_hash_masked"] != TX
    assert diagnostics["receipt_evidence"]["receipt_fingerprint"]
    assert diagnostics["event_evidence"][0]["event_fingerprint"]


def test_capabilities_are_offline_only_and_no_io_surface_exists():
    capabilities = describe_transaction_lifecycle_capabilities()
    for name in ("create_prepared_lifecycle", "import_external_submission", "validate_receipt_evidence", "validate_event_evidence", "validate_state_transition", "build_lifecycle_preview", "serialize_safe_diagnostics"):
        assert capabilities[name] == "available_offline"
    for name in ("submit_transaction", "poll_transaction", "fetch_receipt", "fetch_logs", "detect_replacement_live", "wait_for_confirmations"):
        assert capabilities[name] == "unavailable"
    record = create_prepared_lifecycle(envelope())
    assert not hasattr(record, "submit")
    assert not hasattr(record, "poll")
    assert not hasattr(record, "fetch_receipt")
