from dataclasses import FrozenInstanceError, replace
from decimal import Decimal

import pytest

from bot.execution.dreamdex_direct_order_encoding import DreamDexDirectOrderSpecification
from bot.execution.dreamdex_readonly_rpc import FixtureDreamDexReadOnlyRpcTransport
from bot.execution.dreamdex_transaction_envelope import DreamDexTransactionEnvelopeEvidence, build_unsigned_transaction_envelope
from bot.execution.dreamdex_transaction_preflight import (
    DreamDexTransactionPreflightPolicy,
    build_transaction_preflight_preview,
    calculate_eip1559_max_fee,
    calculate_gas_limit,
    calculate_legacy_gas_price,
    calculate_required_native_balance,
    calculate_total_fee,
    run_transaction_preflight,
    serialize_transaction_preflight_diagnostics,
    unavailable_preflight_result,
)
from bot.execution.dreamdex_unsigned_transaction import CHAIN_ID, build_unsigned_place_order_request

OWNER = "0x2222222222222222222222222222222222222222"
POOL = "0x035de7403eac6872787779cca7ccf1b4cdB61379"


def _envelope():
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
        request, nonce=None, gas_limit=None, transaction_type="unresolved",
        evidence=DreamDexTransactionEnvelopeEvidence(source_type="external_manual"),
    )


def _policy(**overrides):
    values = dict(
        required_sender_address=OWNER,
        required_target_address=POOL.lower(),
        maximum_gas_limit=100000,
        maximum_total_fee_wei=5000000,
        gas_headroom_bps=12000,
        legacy_gas_multiplier_bps=11000,
        base_fee_multiplier_bps=12000,
        maximum_priority_fee_per_gas_wei=100,
        unresolved_reasons=(),
    )
    values.update(overrides)
    return DreamDexTransactionPreflightPolicy(**values)


def _rpc(**overrides):
    values = dict(
        eth_chainId="0x13a7", eth_getCode="0x6000", eth_getTransactionCount="0x1",
        eth_estimateGas="0x5208", eth_getBlockByNumber={"number": "0x10", "baseFeePerGas": "0x64"},
        eth_maxPriorityFeePerGas="0x2", eth_getBalance="0x100000000",
    )
    values.update(overrides)
    return FixtureDreamDexReadOnlyRpcTransport(values)


def test_policy_and_pure_fee_math_are_strict_and_integer_only():
    assert calculate_gas_limit(21000, 12000, 30000) == 25200
    assert calculate_legacy_gas_price(100, 11000) == 110
    assert calculate_eip1559_max_fee(100, 12000, 2) == 122
    assert calculate_total_fee(10, 5) == 50
    assert calculate_required_native_balance(3, 50) == 53
    with pytest.raises(ValueError):
        calculate_gas_limit(21000, 9999, 30000)
    with pytest.raises(ValueError):
        calculate_gas_limit(21000, 12000, 20000)
    with pytest.raises(ValueError):
        calculate_total_fee(1 << 256, 1)
    with pytest.raises(ValueError):
        DreamDexTransactionPreflightPolicy(maximum_gas_limit=True)


def test_successful_read_only_preflight_call_sequence_and_finalized_envelope():
    envelope = _envelope()
    original_fp = envelope.envelope_fingerprint
    rpc = _rpc()
    result = run_transaction_preflight(envelope, rpc, _policy())
    assert result.preflight_status == "completed"
    assert result.policy_compliant is True
    assert result.finalized_envelope is not None
    assert result.finalized_envelope.validation_status == "structurally_complete"
    assert result.finalized_envelope.nonce == 1
    assert result.finalized_envelope.gas_limit == 25200
    assert result.finalized_envelope.transaction_type == "eip1559"
    assert result.original_envelope_fingerprint == original_fp
    assert result.finalized_envelope_fingerprint != original_fp
    assert result.finalized_envelope.request_fingerprint == envelope.request_fingerprint
    assert result.evidence.target_code_byte_length == 2
    assert result.evidence.target_code_sha256 is not None
    assert envelope.envelope_fingerprint == original_fp
    assert "pending_nonce_snapshot_not_reserved" in result.blockers
    assert result.ready_for_signer_invocation is False
    assert run_transaction_preflight(envelope, _rpc(), _policy(authoritative=True)).authoritative is True
    assert [method for method, _ in rpc.calls] == [
        "eth_chainId", "eth_getCode", "eth_getTransactionCount", "eth_estimateGas",
        "eth_getBlockByNumber", "eth_maxPriorityFeePerGas", "eth_getBalance",
    ]


def test_chain_mismatch_and_empty_code_stop_before_account_specific_calls():
    chain_rpc = _rpc(eth_chainId="0x1")
    result = run_transaction_preflight(_envelope(), chain_rpc, _policy())
    assert result.blockers == ("rpc_chain_mismatch",)
    assert [method for method, _ in chain_rpc.calls] == ["eth_chainId"]

    code_rpc = _rpc(eth_getCode="0x")
    result = run_transaction_preflight(_envelope(), code_rpc, _policy())
    assert "target_contract_code_missing" in result.blockers
    assert [method for method, _ in code_rpc.calls] == ["eth_chainId", "eth_getCode"]

    malformed_rpc = _rpc(eth_getCode="0x12xz")
    malformed = run_transaction_preflight(_envelope(), malformed_rpc, _policy())
    assert "target_contract_code_malformed" in malformed.blockers
    assert [method for method, _ in malformed_rpc.calls] == ["eth_chainId", "eth_getCode"]


def test_gas_revert_fee_cap_and_native_balance_fail_closed():
    reverted = run_transaction_preflight(_envelope(), _rpc(eth_estimateGas=RuntimeError("execution reverted: hidden")), _policy())
    assert "gas_estimate_reverted" in reverted.blockers
    assert "hidden" not in repr(reverted)

    capped = run_transaction_preflight(_envelope(), _rpc(eth_maxPriorityFeePerGas="0x100000"), _policy(maximum_priority_fee_per_gas_wei=2))
    assert "fee_priority_limit_unresolved" in capped.blockers

    insufficient = run_transaction_preflight(_envelope(), _rpc(eth_getBalance="0x1"), _policy())
    assert "native_fee_balance_insufficient" in insufficient.blockers
    assert insufficient.finalized_envelope is None


def test_legacy_fee_model_and_fee_history_fallback():
    rpc = _rpc(
        eth_getBlockByNumber={"number": "0x10"},
        eth_gasPrice="0x64",
    )
    result = run_transaction_preflight(_envelope(), rpc, _policy())
    assert result.resolved_parameters.transaction_type == "legacy"
    assert result.resolved_parameters.gas_price_wei == 110
    assert "eth_gasPrice" in [method for method, _ in rpc.calls]

    class PriorityUnavailable(FixtureDreamDexReadOnlyRpcTransport):
        def get_max_priority_fee_per_gas(self):
            self.calls.append(("eth_maxPriorityFeePerGas", ()))
            raise RuntimeError("method unavailable")

    fallback = PriorityUnavailable({
        "eth_chainId": "0x13a7", "eth_getCode": "0x6000", "eth_getTransactionCount": "0x1",
        "eth_estimateGas": "0x5208", "eth_getBlockByNumber": {"number": "0x10", "baseFeePerGas": "0x64"},
        "eth_feeHistory": {"reward": [["0x2"]]}, "eth_getBalance": "0x100000000",
    })
    result = run_transaction_preflight(_envelope(), fallback, _policy())
    assert result.resolved_parameters.max_priority_fee_per_gas_wei == 2
    assert "eth_feeHistory" in [method for method, _ in fallback.calls]


def test_invalid_envelope_type_and_unavailable_default_are_safe():
    result = run_transaction_preflight({}, _rpc(), _policy())
    assert result.preflight_status == "unavailable"
    assert "envelope_type_invalid" in result.blockers
    default = unavailable_preflight_result()
    preview = build_transaction_preflight_preview(default)
    assert preview.network_execution_performed is False
    assert preview.signer_invocation_allowed is False
    assert preview.transaction_submission_allowed is False
    assert "calldata" not in repr(preview)
    assert "0x222222" not in repr(default)


def test_safe_diagnostics_never_include_raw_calldata_and_models_are_frozen():
    result = run_transaction_preflight(_envelope(), _rpc(), _policy())
    diagnostics = serialize_transaction_preflight_diagnostics(result)
    assert "calldata" not in str(diagnostics).lower()
    assert "0x4e978373" not in str(diagnostics)
    with pytest.raises(FrozenInstanceError):
        result.preflight_status = "completed"


def test_target_code_changes_preflight_fingerprint_without_raw_code_output():
    first = run_transaction_preflight(_envelope(), _rpc(eth_getCode="0x6000"), _policy())
    second = run_transaction_preflight(_envelope(), _rpc(eth_getCode="0x6001"), _policy())
    assert first.preflight_fingerprint != second.preflight_fingerprint
    assert "6000" not in str(first.safe_dict())


def test_nonce_fee_and_balance_evidence_change_preflight_fingerprint():
    first = run_transaction_preflight(_envelope(), _rpc(eth_getTransactionCount="0x1"), _policy())
    second = run_transaction_preflight(_envelope(), _rpc(eth_getTransactionCount="0x2"), _policy())
    assert first.preflight_fingerprint != second.preflight_fingerprint
    third = run_transaction_preflight(_envelope(), _rpc(eth_getBalance="0x1"), _policy())
    assert first.preflight_fingerprint != third.preflight_fingerprint
