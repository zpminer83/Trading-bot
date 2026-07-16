from dataclasses import FrozenInstanceError, replace
from decimal import Decimal

import pytest

from bot.execution.dreamdex_direct_order_encoding import DreamDexDirectOrderSpecification
from bot.execution.dreamdex_transaction_envelope import (
    MAX_UINT256,
    VENDOR_FEE_POLICY_SOURCE_PATHS,
    DreamDexTransactionEnvelopeEvidence,
    build_transaction_type_policy_evidence,
    DreamDexUnsignedTransactionEnvelope,
    build_transaction_envelope_preview,
    build_unsigned_transaction_envelope,
    describe_transaction_envelope_capabilities,
    serialize_transaction_envelope_diagnostics,
    validate_unsigned_transaction_envelope,
)
from bot.execution.dreamdex_unsigned_transaction import CHAIN_ID, build_unsigned_place_order_request


POOL = "0x1111111111111111111111111111111111111111"
OWNER = "0x2222222222222222222222222222222222222222"


def _request():
    spec = DreamDexDirectOrderSpecification(
        symbol="SOMI:USDso", side="buy", order_type="limit", price=Decimal("10"), quantity=Decimal("1"),
        time_in_force="gtc", post_only=False, reduce_only=False, deadline=999999999999999999,
        owner_subject=OWNER, signer_subject=OWNER, target_contract=POOL,
        tick_size=Decimal("0.0001"), quantity_step=Decimal("0.01"), minimum_quantity=Decimal("1"), minimum_notional=Decimal("1"),
    )
    return build_unsigned_place_order_request(
        spec, chain_id=CHAIN_ID, from_address=OWNER, to_address=POOL,
        source_confirmed_pool_address=POOL, declared_signer_address=OWNER,
        value_wei=0, input_asset_kind="erc20",
    )


def _evidence(**overrides):
    values = dict(
        source_type="external_manual", source_status="available",
        chain_id_status="externally_supplied", nonce_status="externally_supplied",
        gas_limit_status="externally_supplied", transaction_type_status="externally_supplied",
        fee_status="externally_supplied", base_fee_status="unavailable",
        priority_fee_status="externally_supplied", max_fee_status="externally_supplied",
        block_reference_status="unavailable",
    )
    values.update(overrides)
    return DreamDexTransactionEnvelopeEvidence(**values)


def _envelope(**overrides):
    values = dict(
        nonce=0, gas_limit=700000, transaction_type="eip1559",
        max_fee_per_gas_wei=20, max_priority_fee_per_gas_wei=2,
        evidence=_evidence(),
    )
    values.update(overrides)
    return build_unsigned_transaction_envelope(_request(), **values)


def test_structurally_complete_external_fixture_is_immutable_but_never_ready():
    envelope = _envelope()
    assert envelope.validation_status == "structurally_complete"
    assert envelope.authoritative is False
    assert envelope.ready_for_signing is False
    assert envelope.ready_for_submission is False
    assert validate_unsigned_transaction_envelope(envelope, _request()).valid
    with pytest.raises(FrozenInstanceError):
        envelope.nonce = 1


def test_request_and_envelope_fingerprints_are_deterministic_and_bind_fields():
    first = _envelope()
    second = _envelope()
    assert first.request_fingerprint == second.request_fingerprint
    assert first.envelope_fingerprint == second.envelope_fingerprint
    assert first == second
    assert validate_unsigned_transaction_envelope(replace(first, request_fingerprint="0" * 64), _request()).valid is False
    assert "request_fingerprint_mismatch" in validate_unsigned_transaction_envelope(replace(first, request_fingerprint="0" * 64), _request()).errors


def test_calldata_hash_length_and_operation_integrity_fail_closed():
    envelope = _envelope()
    altered = replace(envelope, calldata=bytes([0x00]) + envelope.calldata[1:])
    errors = validate_unsigned_transaction_envelope(altered, _request()).errors
    assert "calldata_hash_mismatch" in errors
    assert "selector_operation_mismatch" in errors
    altered_length = replace(envelope, calldata_length=envelope.calldata_length + 1)
    assert "calldata_length_mismatch" in validate_unsigned_transaction_envelope(altered_length, _request()).errors
    assert "operation_request_mismatch" in validate_unsigned_transaction_envelope(replace(envelope, operation="cancel_order"), _request()).errors
    assert "chain_id_request_mismatch" in validate_unsigned_transaction_envelope(replace(envelope, chain_id=1), _request()).errors
    assert "address_request_mismatch" in validate_unsigned_transaction_envelope(replace(envelope, from_address=POOL), _request()).errors
    assert "value_request_mismatch" in validate_unsigned_transaction_envelope(replace(envelope, value_wei=1), _request()).errors


def test_nonce_policy_accepts_zero_and_rejects_invalid_values_without_rpc():
    assert _envelope(nonce=0).nonce == 0
    assert _envelope(nonce=MAX_UINT256).nonce == MAX_UINT256
    for value in (-1, True, MAX_UINT256 + 1):
        result = _envelope(nonce=value)
        assert "nonce_invalid" in result.blockers
        assert validate_unsigned_transaction_envelope(result, _request()).valid is False
    missing = _envelope(nonce=None)
    assert "transaction_nonce_unresolved" in missing.blockers
    assert missing.ready_for_signing is False


def test_gas_limit_is_positive_structural_input_without_floor_or_headroom():
    assert _envelope(gas_limit=1).gas_limit == 1
    assert _envelope(gas_limit=MAX_UINT256).gas_limit == MAX_UINT256
    for value in (0, -1, True, MAX_UINT256 + 1):
        assert "gas_limit_invalid" in _envelope(gas_limit=value).blockers


def test_legacy_and_eip1559_fee_modes_are_exclusive():
    legacy = _envelope(transaction_type="legacy", gas_price_wei=10, max_fee_per_gas_wei=None, max_priority_fee_per_gas_wei=None)
    assert validate_unsigned_transaction_envelope(legacy, _request()).valid
    assert legacy.validation_status == "structurally_complete"
    mixed_legacy = _envelope(transaction_type="legacy", gas_price_wei=10, max_fee_per_gas_wei=20, max_priority_fee_per_gas_wei=None)
    assert "legacy_mixed_fee_fields" in mixed_legacy.blockers
    mixed_1559 = _envelope(transaction_type="eip1559", gas_price_wei=10)
    assert "eip1559_mixed_gas_price" in mixed_1559.blockers
    below = _envelope(transaction_type="eip1559", max_fee_per_gas_wei=1, max_priority_fee_per_gas_wei=2)
    assert "max_fee_below_priority_fee" in below.blockers
    for value in (-1, True, MAX_UINT256 + 1):
        assert "max_fee_per_gas_wei_invalid" in _envelope(max_fee_per_gas_wei=value).blockers


def test_unresolved_fee_mode_is_informational_only():
    envelope = _envelope(transaction_type="unresolved", gas_price_wei=None, max_fee_per_gas_wei=None, max_priority_fee_per_gas_wei=None)
    assert envelope.validation_status == "incomplete"
    assert "transaction_type_policy_unresolved" in envelope.blockers
    assert "transaction_fees_unresolved" in envelope.blockers
    assert validate_unsigned_transaction_envelope(envelope, _request()).valid is False
    assert envelope.ready_for_signing is False


def test_evidence_is_non_authoritative_and_source_type_is_allowlisted():
    with pytest.raises(ValueError):
        DreamDexTransactionEnvelopeEvidence(source_type="rpc")
    with pytest.raises(ValueError):
        DreamDexTransactionEnvelopeEvidence(authoritative=True)
    conflicted = _envelope(evidence=DreamDexTransactionEnvelopeEvidence(source_type="test_fixture", conflicts=("source_conflict",)))
    assert "source_conflict" in conflicted.blockers


def test_preview_and_diagnostics_redact_raw_calldata_and_full_addresses():
    envelope = _envelope()
    preview = build_transaction_envelope_preview(envelope)
    diagnostics = serialize_transaction_envelope_diagnostics(envelope)
    raw_hex = "0x" + envelope.calldata.hex()
    for rendered in (repr(envelope), repr(preview), str(diagnostics)):
        assert raw_hex not in rendered
        assert OWNER not in rendered
        assert POOL not in rendered
    assert preview.selector == "0x4e978373"
    assert diagnostics["selector"] == "0x4e978373"
    assert "calldata" not in diagnostics
    assert diagnostics["from_address_masked"] != OWNER
    assert diagnostics["to_address_masked"] != POOL


def test_capability_matrix_and_vendor_evidence_are_offline_only():
    capabilities = describe_transaction_envelope_capabilities()
    assert capabilities["build_unsigned_envelope"] == "available_offline"
    assert capabilities["validate_unsigned_envelope"] == "available_offline"
    assert capabilities["preview_unsigned_envelope"] == "available_offline"
    assert capabilities["resolve_nonce"] == "unavailable"
    assert capabilities["estimate_gas"] == "unavailable"
    assert capabilities["resolve_fees"] == "unavailable"
    assert capabilities["sign_transaction"] == "unavailable"
    assert capabilities["serialize_signed_transaction"] == "unavailable"
    assert capabilities["submit_transaction"] == "unavailable"
    assert capabilities["wait_for_receipt"] == "unavailable"
    assert "packages/core/src/execute.ts" in VENDOR_FEE_POLICY_SOURCE_PATHS
    assert "packages/core-py/dreamdex_core/nonce.py" in VENDOR_FEE_POLICY_SOURCE_PATHS
    policy = build_transaction_type_policy_evidence({"packages/core/src/execute.ts": "a" * 64})
    assert policy.source_status == "observed"
    assert policy.source_fingerprints == (("packages/core/src/execute.ts", "a" * 64),)
    assert policy.transaction_type_status == "unavailable"
