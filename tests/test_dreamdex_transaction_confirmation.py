from dataclasses import FrozenInstanceError

import pytest

from bot.execution.dreamdex_direct_order_encoding import ORDER_CANCELLED_TOPIC, ORDER_PLACED_TOPIC
from bot.execution.dreamdex_readonly_rpc import FixtureDreamDexReadOnlyRpcTransport
from bot.execution.dreamdex_transaction_confirmation import (
    DreamDexTransactionConfirmationPolicy,
    calculate_confirmation_depth,
    normalize_transaction_receipt,
    validate_canonical_block,
    validate_order_cancelled_event,
    validate_order_placed_event,
)


TX = "0x" + "1" * 64
BLOCK = "0x" + "2" * 64
POOL = "0x" + "3" * 40


def _receipt(topic, *, order_id=7, status="0x1", block_hash=BLOCK):
    return {
        "transactionHash": TX,
        "status": status,
        "blockNumber": "0x5",
        "blockHash": block_hash,
        "transactionIndex": "0x0",
        "to": POOL,
        "cumulativeGasUsed": "0x10",
        "gasUsed": "0x8",
        "effectiveGasPrice": "0x2",
        "logs": [{"address": POOL, "topics": [topic, "0x" + format(order_id, "064x")], "data": "0x"}],
    }


def test_policy_is_bounded_and_immutable():
    policy = DreamDexTransactionConfirmationPolicy(minimum_confirmations=2, maximum_observation_attempts=3)
    assert policy.minimum_confirmations == 2
    with pytest.raises(FrozenInstanceError):
        policy.minimum_confirmations = 4
    with pytest.raises(ValueError):
        DreamDexTransactionConfirmationPolicy(minimum_confirmations=0)
    with pytest.raises(ValueError):
        DreamDexTransactionConfirmationPolicy(automatic_resend_allowed=True)


def test_receipt_normalization_keeps_only_safe_evidence():
    evidence = normalize_transaction_receipt(_receipt(ORDER_PLACED_TOPIC), TX, expected_target=POOL)
    assert evidence.receipt_found is True
    assert evidence.receipt_status == "success"
    assert evidence.block_number == 5
    assert evidence.log_count == 1
    assert "topics" not in evidence.safe_dict()
    assert "data" not in repr(evidence)


def test_reverted_and_malformed_receipts_fail_closed():
    assert normalize_transaction_receipt(_receipt(ORDER_PLACED_TOPIC, status="0x0"), TX).receipt_status == "reverted"
    malformed = dict(_receipt(ORDER_PLACED_TOPIC)); malformed["status"] = "0x2"
    assert normalize_transaction_receipt(malformed, TX).receipt_status == "malformed"
    malformed = dict(_receipt(ORDER_PLACED_TOPIC)); malformed["blockHash"] = "0x"
    assert "transaction_receipt_malformed" in normalize_transaction_receipt(malformed, TX).validation_errors


def test_canonical_depth_and_block_match():
    assert calculate_confirmation_depth(5, 5) == 1
    assert calculate_confirmation_depth(9, 5) == 5
    with pytest.raises(ValueError):
        calculate_confirmation_depth(4, 5)
    evidence = validate_canonical_block(receipt_block_number=5, receipt_block_hash=BLOCK, canonical_block={"hash": BLOCK}, latest_block_number=6, required_confirmations=2)
    assert evidence.block_hash_match is True
    assert evidence.finality_reached is True


def test_place_and_cancel_events_require_exact_pool_and_order_id():
    placed = validate_order_placed_event(_receipt(ORDER_PLACED_TOPIC), expected_pool=POOL, transaction_hash=TX, expected_order_id=7)
    assert placed.event_found and placed.order_id_match is True
    cancelled = validate_order_cancelled_event(_receipt(ORDER_CANCELLED_TOPIC), expected_pool=POOL, transaction_hash=TX, expected_order_id=7)
    assert cancelled.event_found and cancelled.order_id_match is True
    wrong = validate_order_cancelled_event(_receipt(ORDER_CANCELLED_TOPIC), expected_pool=POOL, transaction_hash=TX, expected_order_id=8)
    assert wrong.order_id_match is False


def test_rpc_fixture_exposes_only_typed_receipt_and_block_calls():
    rpc = FixtureDreamDexReadOnlyRpcTransport({"eth_getTransactionReceipt": _receipt(ORDER_PLACED_TOPIC), "eth_getBlockByNumber": {"hash": BLOCK}, "eth_blockNumber": "0x6"})
    assert rpc.get_transaction_receipt(TX)["transactionHash"] == TX
    assert rpc.get_block_by_number(5)["hash"] == BLOCK
    assert rpc.get_block_number() == 6
    assert [call[0] for call in rpc.calls] == ["eth_getTransactionReceipt", "eth_getBlockByNumber", "eth_blockNumber"]
    assert not hasattr(rpc, "call")
