from datetime import datetime, timezone
from decimal import Decimal

import pytest

from bot.integrations.dreamdex_fill_events import (
    ALLOWED_RPC_METHODS,
    ORDER_FILLED_EVENT_SIGNATURE,
    ORDER_FILLED_TOPIC,
    SOMI_USDSO_POOL,
    FillEventCursor,
    FixtureFillEventRpcTransport,
    HttpFillEventRpcTransport,
    OrderFilledEventIndexer,
    decode_order_filled_log,
)
from bot.integrations.dreamdex_read_only import DreamDexReadOnlyAdapter, FixtureRpcTransport, FixtureTransport


POOL = SOMI_USDSO_POOL
OTHER_POOL = "0x1111111111111111111111111111111111111111"
TX = "0x" + "aa" * 32
BLOCK_HASH = "0x" + "bb" * 32


def _word(value: int) -> str:
    return "0x" + format(value, "064x")


def _data(quantity: int, taker_remaining: int, maker_remaining: int, price: int) -> str:
    return "0x" + "".join(format(value, "064x") for value in (quantity, taker_remaining, maker_remaining, price))


def _log(*, block: int = 16, quantity: int = 2_500_000_000_000_000_000, price: int = 123_450_000_000_000_000_000, address: str = POOL, tx: str = TX, removed: bool = False, block_hash: str = BLOCK_HASH):
    return {
        "address": address,
        "topics": [ORDER_FILLED_TOPIC, _word(7), _word(8)],
        "data": _data(quantity, 0, 1_000_000_000_000_000_000, price),
        "blockNumber": hex(block),
        "blockHash": block_hash,
        "transactionHash": tx,
        "logIndex": "0x0",
        "removed": removed,
    }


def _fixture(logs=None, *, latest=32, blocks=None):
    logs = [_log()] if logs is None else logs
    blocks = blocks or {
        "0x10": {"hash": BLOCK_HASH, "timestamp": "0x65000000"},
        "0x1": {"hash": "0x" + "01" * 32, "timestamp": "0x65000000"},
        "0x2": {"hash": "0x" + "02" * 32, "timestamp": "0x65000001"},
    }
    return {"rpc": {"eth_chainId": "0x14", "eth_blockNumber": hex(latest), "eth_getLogs": logs, "eth_getBlockByNumber": blocks}}


def test_official_order_filled_signature_and_indexed_layout_are_pinned():
    assert ORDER_FILLED_EVENT_SIGNATURE == "OrderFilled(uint128,uint128,uint256,uint256,uint256,uint256)"
    assert ORDER_FILLED_TOPIC == "0xc87f4223e9e7c4e4f39f9b34fc9d64d78cdb95d9035b3748cbde59521261a399"
    raw, fill = decode_order_filled_log(_log(), chain_id=20, pool_address=POOL, symbol="SOMI:USDso", base_decimals=18, quote_decimals=18)
    assert raw.topics[1] == _word(7)
    assert fill.taker_order_id == 7
    assert fill.maker_order_id == 8
    assert fill.raw_quantity == 2_500_000_000_000_000_000
    assert fill.raw_price == 123_450_000_000_000_000_000
    assert fill.quantity == Decimal("2.5")
    assert fill.price == Decimal("123.45")
    assert fill.notional == Decimal("308.625")
    assert fill.owner is None
    assert fill.side is None
    assert fill.is_bid is None
    assert fill.user_data is None


def test_indexer_uses_public_read_only_rpc_and_normalizes_block_metadata():
    transport = FixtureFillEventRpcTransport(_fixture())
    indexer = OrderFilledEventIndexer(transport, chain_id=20, confirmation_depth=2, expected_account="0x" + "12" * 20)
    page = indexer.fetch(from_block=16, to_block=16)
    assert page.source_status.available
    assert page.source_status.latest_block == 32
    assert page.source_status.confirmed_through_block == 30
    assert page.fills[0].confirmed
    assert page.fills[0].block_timestamp == datetime.fromtimestamp(0x65000000, tz=timezone.utc)
    assert [method for method, _ in transport.calls] == ["eth_blockNumber", "eth_getLogs", "eth_getBlockByNumber"]
    assert set(transport.ALLOWED_METHODS) == {"eth_getLogs", "eth_blockNumber", "eth_getBlockByNumber", "eth_chainId"}


def test_missing_owner_is_unresolved_and_never_auto_selects_account():
    indexer = OrderFilledEventIndexer(FixtureFillEventRpcTransport(_fixture()), chain_id=20, confirmation_depth=0, expected_account="0x" + "12" * 20)
    page = indexer.fetch(from_block=16, to_block=16)
    report = indexer.reconcile(page)
    assert page.source_status.account_match_status == "unresolved"
    assert not report.account_fills_authoritative
    assert "authoritative_account_address_unresolved" in report.mismatches


def test_owner_filter_is_applied_after_decoding_when_fixture_metadata_supplies_owner():
    owner = "0x" + "12" * 20
    indexer = OrderFilledEventIndexer(
        FixtureFillEventRpcTransport(_fixture()), chain_id=20, confirmation_depth=0,
        expected_account=owner, owner_by_order_id={7: owner},
    )
    page = indexer.fetch(from_block=16, to_block=16)
    assert page.source_status.account_match_status == "matched"
    assert page.fills[0].account_match is True
    assert indexer.reconcile(page).account_fills_authoritative


def test_duplicate_identical_event_is_counted_once_and_conflict_blocks_source():
    transport = FixtureFillEventRpcTransport(_fixture())
    indexer = OrderFilledEventIndexer(transport, chain_id=20, confirmation_depth=0)
    first = indexer.fetch(from_block=16, to_block=16)
    second = indexer.fetch(from_block=16, to_block=16)
    assert len(first.fills) == 1
    assert len(second.fills) == 0
    assert second.source_status.duplicate_count == 1
    conflicting = dict(_log())
    conflicting["data"] = _data(3_500_000_000_000_000_000, 0, 0, 123_450_000_000_000_000_000)
    transport.fixture["rpc"]["eth_getLogs"] = [conflicting]
    third = indexer.fetch(from_block=16, to_block=16)
    assert third.source_status.status == "malformed"
    assert third.source_status.error_code == "malformed_duplicate"
    assert "malformed_duplicate" in indexer.reconcile(third).mismatches


def test_removed_log_and_block_hash_mismatch_are_reorg_fail_closed():
    removed = OrderFilledEventIndexer(FixtureFillEventRpcTransport(_fixture([_log(removed=True)])), chain_id=20, confirmation_depth=0).fetch(from_block=16, to_block=16)
    assert removed.source_status.reorg_status == "reorg_detected"
    assert removed.source_status.error_code == "reorg_detected"

    mismatch_fixture = _fixture([_log(block_hash="0x" + "cc" * 32)])
    mismatch = OrderFilledEventIndexer(FixtureFillEventRpcTransport(mismatch_fixture), chain_id=20, confirmation_depth=0).fetch(from_block=16, to_block=16)
    assert mismatch.source_status.reorg_status == "reorg_detected"
    assert "reorg_detected" in OrderFilledEventIndexer(FixtureFillEventRpcTransport(mismatch_fixture), chain_id=20, confirmation_depth=0).reconcile(mismatch).mismatches


def test_pagination_returns_cursor_and_cursor_hash_mismatch_blocks():
    fixture = _fixture([_log(block=1, block_hash="0x" + "01" * 32)], latest=5)
    indexer = OrderFilledEventIndexer(FixtureFillEventRpcTransport(fixture), chain_id=20, confirmation_depth=0, max_block_span=2)
    page = indexer.fetch(from_block=1, to_block=5)
    assert not page.pagination_complete
    assert page.next_cursor is not None
    assert page.next_cursor.next_block == 3

    bad_cursor = FillEventCursor(next_block=2, block_number=1, block_hash="0x" + "ff" * 32)
    blocked = indexer.fetch(from_block=1, to_block=5, cursor=bad_cursor)
    assert blocked.source_status.reorg_status == "reorg_detected"
    assert blocked.source_status.error_code == "reorg_detected"


def test_malformed_logs_wrong_contract_and_invalid_data_are_not_authoritative():
    with pytest.raises(ValueError, match="wrong_contract_address"):
        decode_order_filled_log(_log(address=OTHER_POOL), chain_id=20, pool_address=POOL, symbol="SOMI:USDso", base_decimals=18, quote_decimals=18)
    malformed = dict(_log())
    malformed["data"] = "0x1234"
    page = OrderFilledEventIndexer(FixtureFillEventRpcTransport(_fixture([malformed])), chain_id=20, confirmation_depth=0).fetch(from_block=16, to_block=16)
    assert page.source_status.status == "malformed"
    assert page.source_status.malformed_count == 1
    assert not OrderFilledEventIndexer(FixtureFillEventRpcTransport(_fixture([malformed])), chain_id=20, confirmation_depth=0).reconcile(page).completed


def test_transport_has_no_signing_or_mutation_methods_and_no_network_is_used():
    transport = FixtureFillEventRpcTransport(_fixture())
    assert not hasattr(transport, "sign")
    assert not hasattr(transport, "send_transaction")
    assert not hasattr(transport, "send_raw_transaction")
    assert not hasattr(transport, "submit_order")
    assert not hasattr(transport, "cancel_order")
    assert not hasattr(transport, "replace_order")
    with pytest.raises(ValueError):
        transport.call("eth_sendRawTransaction", [])
    with pytest.raises(ValueError):
        HttpFillEventRpcTransport("https://rpc.invalid").call("eth_sendTransaction", [])


def test_adapter_exposes_optional_onchain_source_without_changing_fail_closed_account_rules():
    owner = "0x" + "12" * 20
    public = {
        "markets": [{
            "symbol": "SOMI:USDso", "base": "0x" + "11" * 20, "quote": "0x" + "22" * 20,
            "contract": POOL, "baseDecimals": 18, "quoteDecimals": 18,
            "tickSize": "0.0001", "lotSize": "0.01", "minQuantity": "1",
            "stopRegistry": "0x" + "33" * 20,
        }],
        "orderbook": {"symbol": "SOMI:USDso", "bids": [{"price": "10", "quantity": "2"}], "asks": [{"price": "10.1", "quantity": "2"}], "timestamp": datetime.now(timezone.utc).isoformat()},
        "vault_rest": {"SOMI": "0", "USDso": "0"},
        "rpc": {"base_vault": "0x0", "quote_vault": "0x0", "base_wallet": "0x0", "quote_wallet": "0x0"},
        "native_gas": "0x0",
    }
    indexer = OrderFilledEventIndexer(FixtureFillEventRpcTransport(_fixture()), chain_id=20, confirmation_depth=0, expected_account=owner, owner_by_order_id={7: owner})
    adapter = DreamDexReadOnlyAdapter(
        transport=FixtureTransport(public), rpc_transport=FixtureRpcTransport(public), owner=owner,
        trading_address=owner, fill_event_indexer=indexer,
    )
    snapshot = adapter.fetch_snapshot()
    assert snapshot.onchain_fills.source_status.available
    assert snapshot.account.onchain_fills.fills
    assert snapshot.account.fills_status == "confirmed"
    report = adapter.reconcile(snapshot)
    assert "incomplete_open_orders_source" in report.mismatches
