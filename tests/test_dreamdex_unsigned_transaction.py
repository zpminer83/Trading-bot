from decimal import Decimal

from bot.execution.dreamdex_direct_order_encoding import DreamDexDirectOrderSpecification
from bot.execution.dreamdex_unsigned_transaction import (
    CHAIN_ID,
    CANCEL_SELECTOR,
    PLACE_SELECTOR,
    REDUCE_SELECTOR,
    UnavailableDreamDexTransactionTransport,
    build_unsigned_cancel_order_request,
    build_unsigned_place_order_request,
    build_unsigned_reduce_order_request,
    build_unsigned_transaction_preview,
    build_unsigned_transaction_requirements,
    validate_unsigned_transaction_request,
)


POOL = "0x1111111111111111111111111111111111111111"
OWNER = "0x2222222222222222222222222222222222222222"
OTHER = "0x3333333333333333333333333333333333333333"


def place_spec(**overrides):
    values = {
        "symbol": "SOMI:USDso",
        "side": "buy",
        "order_type": "limit",
        "price": Decimal("10"),
        "quantity": Decimal("1"),
        "time_in_force": "gtc",
        "post_only": False,
        "reduce_only": False,
        "deadline": 999999999999999999,
        "owner_subject": OWNER,
        "signer_subject": OWNER,
        "target_contract": POOL,
        "tick_size": Decimal("0.0001"),
        "quantity_step": Decimal("0.01"),
        "minimum_quantity": Decimal("1"),
        "minimum_notional": Decimal("1"),
    }
    values.update(overrides)
    return DreamDexDirectOrderSpecification(**values)


def common_kwargs():
    return {
        "chain_id": CHAIN_ID,
        "from_address": OWNER,
        "to_address": POOL,
        "source_confirmed_pool_address": POOL,
        "declared_signer_address": OWNER,
    }


def test_place_request_is_immutable_and_deterministic():
    first = build_unsigned_place_order_request(place_spec(), value_wei=123, input_asset_kind="native", native_requirement_wei=123, **common_kwargs())
    second = build_unsigned_place_order_request(place_spec(), value_wei=123, input_asset_kind="native", native_requirement_wei=123, **common_kwargs())
    assert first == second
    assert first.calldata_sha256 == second.calldata_sha256
    assert first.calldata_length == 292
    assert first.calldata[:4].hex() == PLACE_SELECTOR[2:]
    assert first.validation_errors == ()
    assert first.authoritative is False
    assert "calldata" not in first.safe_dict()
    assert "raw" not in repr(first).lower()


def test_cancel_and_reduce_use_exact_selectors_and_zero_value():
    cancel = build_unsigned_cancel_order_request(order_id=7, **common_kwargs())
    reduce = build_unsigned_reduce_order_request(order_id=7, reduce_quantity=3, **common_kwargs())
    assert cancel.calldata[:4].hex() == CANCEL_SELECTOR[2:]
    assert reduce.calldata[:4].hex() == REDUCE_SELECTOR[2:]
    assert cancel.value_wei == 0
    assert reduce.value_wei == 0
    assert cancel.calldata_length == 36
    assert reduce.calldata_length == 68
    assert not cancel.validation_errors
    assert not reduce.validation_errors


def test_chain_from_and_target_binding_fail_closed():
    wrong_chain = build_unsigned_cancel_order_request(order_id=1, **{**common_kwargs(), "chain_id": 1})
    wrong_from = build_unsigned_cancel_order_request(order_id=1, **{**common_kwargs(), "from_address": OTHER})
    wrong_pool = build_unsigned_cancel_order_request(order_id=1, **{**common_kwargs(), "to_address": OTHER})
    missing_from = build_unsigned_cancel_order_request(order_id=1, **{k: v for k, v in common_kwargs().items() if k != "from_address"})
    assert "chain_id_mismatch" in wrong_chain.validation_errors
    assert "from_signer_mismatch" in wrong_from.validation_errors
    assert "target_pool_mismatch" in wrong_pool.validation_errors
    assert "from_address_unavailable" in missing_from.validation_errors
    missing_source = build_unsigned_cancel_order_request(order_id=1, **{k: v for k, v in common_kwargs().items() if k != "source_confirmed_pool_address"})
    assert "source_confirmed_pool_unavailable" in missing_source.validation_errors


def test_value_and_order_bounds_fail_closed():
    cancel = build_unsigned_cancel_order_request(order_id=1, **common_kwargs())
    assert validate_unsigned_transaction_request(cancel).valid
    nonzero = cancel.__class__(**{**cancel.__dict__, "value_wei": 1})
    assert "nonzero_value_for_zero_operation" in validate_unsigned_transaction_request(nonzero).errors
    bad_reduce = build_unsigned_reduce_order_request(order_id=1, reduce_quantity=0, **common_kwargs())
    bad_id = build_unsigned_cancel_order_request(order_id=1 << 128, **common_kwargs())
    assert "reduce_quantity_uint256_invalid" in bad_reduce.validation_errors
    assert "order_id_uint128_invalid" in bad_id.validation_errors


def test_place_value_policy_requires_confirmed_native_requirement():
    missing = build_unsigned_place_order_request(place_spec(), value_wei=0, input_asset_kind="native", **common_kwargs())
    exact = build_unsigned_place_order_request(place_spec(), value_wei=123, input_asset_kind="native", native_requirement_wei=123, **common_kwargs())
    erc20 = build_unsigned_place_order_request(place_spec(), value_wei=0, input_asset_kind="erc20", **common_kwargs())
    assert "native_requirement_unavailable" in missing.validation_errors
    assert missing.value_status == "native_requirement_unavailable"
    assert exact.value_status == "native_requirement_confirmed"
    assert exact.validation_errors == ()
    assert erc20.value_status == "zero_required"
    assert erc20.validation_errors == ()
    negative = build_unsigned_place_order_request(place_spec(), value_wei=-1, input_asset_kind="erc20", **common_kwargs())
    overflow = build_unsigned_place_order_request(place_spec(), value_wei=(1 << 256), input_asset_kind="erc20", **common_kwargs())
    assert "invalid_value_wei" in negative.validation_errors
    assert "invalid_value_wei" in overflow.validation_errors


def test_preview_redacts_calldata_and_never_claims_readiness():
    request = build_unsigned_cancel_order_request(order_id=7, **common_kwargs())
    preview = build_unsigned_transaction_preview(request)
    assert preview.calldata_length == 36
    assert preview.calldata_sha256 == request.calldata_sha256
    assert preview.calldata_selector == CANCEL_SELECTOR
    assert preview.ready_for_signing is False
    assert preview.ready_for_submission is False
    assert preview.gas_limit_status == "unresolved"
    assert preview.nonce_status == "unresolved"
    assert preview.fee_status == "unresolved"
    assert "calldata" not in preview.safe_dict()
    assert "0x" + request.calldata.hex() not in repr(preview)


def test_requirements_and_transport_capabilities_are_offline_only():
    requirements = build_unsigned_transaction_requirements(operation="place_order", from_address=OWNER, to_address=POOL)
    assert requirements.required_chain_id == CHAIN_ID
    assert requirements.required_from_address == OWNER.lower()
    assert requirements.required_to_address == POOL.lower()
    assert requirements.production_status == "unavailable"
    transport = UnavailableDreamDexTransactionTransport()
    capabilities = transport.describe_capabilities()
    assert capabilities["build_unsigned_place"] == "available_offline"
    assert capabilities["build_unsigned_cancel"] == "available_offline"
    assert capabilities["build_unsigned_reduce"] == "available_offline"
    assert capabilities["sign_transaction"] == "unavailable"
    assert capabilities["submit_transaction"] == "unavailable"
    assert capabilities["wait_for_receipt"] == "unavailable"
    assert not hasattr(transport, "send")
    assert not hasattr(transport, "sign")
    assert not hasattr(transport, "execute")
