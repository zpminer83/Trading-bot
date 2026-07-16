from dataclasses import FrozenInstanceError
from decimal import Decimal
from dataclasses import replace

import pytest

from bot.execution.dreamdex_direct_order_encoding import DreamDexDirectOrderSpecification
from bot.execution.dreamdex_transaction_envelope import DreamDexTransactionEnvelopeEvidence, build_unsigned_transaction_envelope
from bot.execution.dreamdex_unsigned_transaction import CHAIN_ID, build_unsigned_place_order_request
from bot.execution.dreamdex_transaction_signer import (
    POOL_ADDRESS,
    DreamDexSignedTransactionArtifact,
    DreamDexTransactionSignerCapabilities,
    DreamDexTransactionSigningPolicy,
    UnavailableDreamDexTransactionSigner,
    build_production_transaction_signing_policy,
    build_signing_preview,
    build_signing_request,
    describe_transaction_signer_capabilities,
    validate_signing_policy,
    validate_signing_request,
)
from bot.execution.dreamdex_execution_primitives import build_execution_capability_matrix

OWNER = "0x2222222222222222222222222222222222222222"


class _FixtureSigner:
    """Test-only signer: synthetic artifact, no keys, crypto, or I/O."""
    def __init__(self):
        self.calls = 0

    def get_address(self):
        return OWNER

    def describe_capabilities(self):
        return DreamDexTransactionSignerCapabilities(
            signer_type="test_fixture", address_discovery="test_fixture", transaction_signing="test_fixture",
            supported_chain_ids=(CHAIN_ID,), supported_transaction_types=("eip1559",),
            supported_operations=("place_order", "cancel_order", "reduce_order"), production_status="test_fixture",
        )

    def sign_transaction(self, request):
        self.calls += 1
        if not request.policy_approved or not request.ready_for_signer_invocation:
            raise ValueError("request_not_approved")
        return DreamDexSignedTransactionArtifact("1", OWNER, request.signing_request_fingerprint, "c" * 64, 32, "test_fixture", "test_fixture", False, False)


def _envelope(**overrides):
    spec = DreamDexDirectOrderSpecification(
        symbol="SOMI:USDso", side="buy", order_type="limit", price=Decimal("10"), quantity=Decimal("1"),
        time_in_force="gtc", post_only=False, reduce_only=False, deadline=999999999999999999,
        owner_subject=OWNER, signer_subject=OWNER, target_contract=POOL_ADDRESS,
        tick_size=Decimal("0.0001"), quantity_step=Decimal("0.01"), minimum_quantity=Decimal("1"), minimum_notional=Decimal("1"),
    )
    request = build_unsigned_place_order_request(spec, chain_id=CHAIN_ID, from_address=OWNER, to_address=POOL_ADDRESS,
                                                  source_confirmed_pool_address=POOL_ADDRESS, declared_signer_address=OWNER,
                                                  value_wei=0, input_asset_kind="erc20")
    evidence = DreamDexTransactionEnvelopeEvidence(
        source_type="external_manual", source_status="available", chain_id_status="externally_supplied",
        nonce_status="externally_supplied", gas_limit_status="externally_supplied", transaction_type_status="test_confirmed",
        fee_status="externally_supplied", priority_fee_status="externally_supplied", max_fee_status="externally_supplied",
    )
    values = dict(nonce=1, gas_limit=700000, transaction_type="eip1559", max_fee_per_gas_wei=20, max_priority_fee_per_gas_wei=2, evidence=evidence)
    values.update(overrides)
    return build_unsigned_transaction_envelope(request, **values)


def _policy(**overrides):
    values = dict(required_signer_address=OWNER, maximum_gas_limit=1_000_000, maximum_total_fee_wei=50_000_000, production_status="test_fixture", unresolved_reasons=())
    values.update(overrides)
    return DreamDexTransactionSigningPolicy(**values)


def test_production_policy_is_fail_closed_and_has_exact_allowlist():
    policy = build_production_transaction_signing_policy()
    assert policy.required_chain_id == 5031
    assert policy.allowed_target_addresses == (POOL_ADDRESS,)
    assert policy.required_signer_address is None
    assert policy.maximum_gas_limit is None
    assert policy.maximum_total_fee_wei is None
    assert policy.authoritative is False


def test_valid_envelope_approved_only_with_explicit_offline_policy():
    result = validate_signing_policy(_envelope(), _policy(), signer_address=OWNER)
    assert result.approved is True
    request = build_signing_request(_envelope(), _policy(), signer_address=OWNER)
    assert request.policy_approved is True
    assert request.ready_for_signer_invocation is True
    assert validate_signing_request(request, envelope=_envelope()).approved is True


def test_allowlist_and_critical_field_changes_fail_closed():
    policy = _policy()
    assert "target_address_not_allowlisted" in validate_signing_policy(replace(_envelope(), to_address=OWNER), policy, signer_address=OWNER).blockers
    assert "chain_id_mismatch" in validate_signing_policy(replace(_envelope(), chain_id=1), policy, signer_address=OWNER).blockers
    assert "signer_address_mismatch" in validate_signing_policy(_envelope(), policy, signer_address="0x3333333333333333333333333333333333333333").blockers


def test_value_policy_rejects_nonzero_erc20_place_and_negative_or_overflow_values():
    policy = _policy()
    assert "value_policy_rejected" in validate_signing_policy(replace(_envelope(), value_wei=1), policy, signer_address=OWNER).blockers
    assert "value_policy_rejected" in validate_signing_policy(replace(_envelope(), value_wei=-1), policy, signer_address=OWNER).blockers
    assert "value_policy_rejected" in validate_signing_policy(replace(_envelope(), value_wei=1 << 256), policy, signer_address=OWNER).blockers


def test_native_value_requires_explicit_cap_and_respects_cap():
    assert "transaction_value_limit_unresolved" in validate_signing_policy(replace(_envelope(), value_wei=5), _policy(allow_native_value=True, maximum_native_value_wei=None), signer_address=OWNER).blockers
    assert validate_signing_policy(replace(_envelope(), value_wei=5), _policy(allow_native_value=True, maximum_native_value_wei=5), signer_address=OWNER).approved
    assert "value_policy_rejected" in validate_signing_policy(replace(_envelope(), value_wei=6), _policy(allow_native_value=True, maximum_native_value_wei=5), signer_address=OWNER).blockers


def test_gas_and_fee_limits_and_models_are_checked():
    policy = _policy(maximum_gas_limit=10, maximum_total_fee_wei=100)
    result = validate_signing_policy(_envelope(gas_limit=11), policy, signer_address=OWNER)
    assert "gas_limit_exceeded" in result.blockers
    result = validate_signing_policy(_envelope(max_fee_per_gas_wei=200), _policy(maximum_total_fee_wei=100 * 700000), signer_address=OWNER)
    assert "total_fee_exceeded" in result.blockers
    assert "transaction_type_unresolved" in validate_signing_policy(_envelope(transaction_type="unresolved", max_fee_per_gas_wei=None, max_priority_fee_per_gas_wei=None), _policy(), signer_address=OWNER).blockers


def test_fingerprint_binding_changes_when_critical_field_changes():
    envelope = _envelope()
    policy = _policy()
    request = build_signing_request(envelope, policy, signer_address=OWNER)
    assert validate_signing_request(request).approved
    altered = replace(request, value_wei=1)
    assert "signing_request_fingerprint_mismatch" in validate_signing_request(altered).blockers
    assert request.signing_request_fingerprint != build_signing_request(replace(envelope, nonce=2), policy, signer_address=OWNER).signing_request_fingerprint


def test_unavailable_signer_never_invokes_io_or_creates_artifact():
    signer = UnavailableDreamDexTransactionSigner()
    assert signer.get_address() == "<unavailable>"
    caps = signer.describe_capabilities()
    assert caps.transaction_signing == "unavailable"
    with pytest.raises(RuntimeError, match="transaction_signer_unavailable"):
        signer.sign_transaction(build_signing_request(_envelope(), _policy(), signer_address=OWNER))


def test_test_only_fake_signer_invokes_once_for_approved_request_and_never_exposes_payload():
    signer = _FixtureSigner()
    request = build_signing_request(_envelope(), _policy(), signer_address=OWNER)
    artifact = signer.sign_transaction(request)
    assert signer.calls == 1
    assert artifact.signature_status == "test_fixture"
    assert artifact.ready_for_submission is False
    assert "calldata" not in artifact.safe_dict()
    with pytest.raises(ValueError):
        signer.sign_transaction(replace(request, policy_approved=False, ready_for_signer_invocation=False))
    assert signer.calls == 2


def test_preview_and_models_are_redacted_and_immutable():
    preview = build_signing_preview()
    safe = preview.safe_dict()
    assert safe["raw_calldata_output_allowed"] is False
    assert "calldata" not in safe
    assert OWNER not in repr(preview)
    with pytest.raises(FrozenInstanceError):
        preview.operation = "place_order"
    artifact = DreamDexSignedTransactionArtifact("1", OWNER, "a" * 64, "b" * 64, 32, "test_fixture", "test_fixture", False, False)
    assert OWNER not in repr(artifact)
    assert artifact.safe_dict()["ready_for_submission"] is False


def test_capabilities_are_offline_and_siwe_not_replaced():
    caps = describe_transaction_signer_capabilities()
    assert caps.authoritative is False
    assert caps.arbitrary_calldata is False
    assert caps.transaction_signing == "unavailable"
    assert caps.supported_operations == ()
    matrix = build_execution_capability_matrix()
    assert matrix.by_name("build_signing_request").status == "available_offline"
    assert matrix.by_name("transaction_signer_protocol").status == "available_offline"
    assert matrix.by_name("signer_address_discovery").status == "unavailable"
