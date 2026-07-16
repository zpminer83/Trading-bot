from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from bot.execution.dreamdex_reconciliation_bridge import (
    DreamDexReconciliationBridgePreview,
    DreamDexReconciliationEvidenceBundle,
    DreamDexReconciliationEvidenceInventory,
    adapt_authenticated_open_orders_for_reconciliation,
    adapt_authenticated_orders_for_reconciliation,
    adapt_lifecycle_records_for_reconciliation,
    adapt_onchain_fills_for_reconciliation,
    adapt_order_metadata_for_reconciliation,
    build_reconciliation_bridge_from_evidence,
    build_reconciliation_bridge_preview,
    build_reconciliation_evidence_bundle,
    build_reconciliation_evidence_inventory,
    describe_reconciliation_bridge_capabilities,
)


TX = "0x" + "a" * 64
POOL = "0x" + "1" * 40
OWNER = "0x" + "2" * 40


def lifecycle(*, order_id=7, tx=TX, operation="place_order", event_name="OrderPlaced", conflict=()):
    return {
        "operation": operation,
        "request_fingerprint": "request-fingerprint",
        "envelope_fingerprint": "envelope-fingerprint",
        "transaction_hash": tx,
        "current_state": "confirmed_success",
        "order_id": order_id,
        "lifecycle_fingerprint": f"life-{order_id}-{tx[-4:]}",
        "event_evidence": ({"event_name": event_name, "order_id": order_id, "transaction_hash": tx, "source_status": "source_confirmed"},),
        "evidence": {"source_status": "observed", "conflicts": conflict, "replacement_status": "resolved"},
        "conflicts": conflict,
    }


def test_models_are_frozen_safe_and_default_empty():
    inventory = DreamDexReconciliationEvidenceInventory()
    bundle = DreamDexReconciliationEvidenceBundle(inventory=inventory)
    assert inventory.lifecycle_record_count == 0
    assert bundle.safe_dict()["expected_account_address"] == "<missing>"
    with pytest.raises(FrozenInstanceError):
        inventory.lifecycle_record_count = 1
    with pytest.raises(FrozenInstanceError):
        bundle.schema_version = "2"
    assert "0x" not in repr(bundle)
    preview = DreamDexReconciliationBridgePreview("unavailable", "unavailable", "unavailable", "unavailable", "unavailable", "unavailable", False, False, "unavailable", 0, 0, 0, 0, 0, 0, 0, None, None, False, False)
    assert "authoritative=False" in repr(preview)


def test_adapters_preserve_source_status_and_do_not_mutate_input():
    auth = [{"order_id": "7", "symbol": "SOMI:USDso", "status": "open"}]
    metadata = [{"order_id": "7", "symbol": "SOMI:USDso", "status": "open", "market_address": POOL}]
    fills = [{"fill_id": "fill-1", "order_id": 7, "transaction_hash": TX, "quantity": "1", "price": "2"}]
    original = (auth[0].copy(), metadata[0].copy(), fills[0].copy())
    source = {"status": "available", "pagination_complete": True, "authority_status": "non_authoritative"}
    assert adapt_authenticated_orders_for_reconciliation(auth, source_status=source)[0]["source_status"] == "available"
    assert adapt_authenticated_open_orders_for_reconciliation(auth, source_status=source)[0]["pagination_complete"] is True
    assert adapt_order_metadata_for_reconciliation(metadata, source_status=source)[0]["market_address"] == POOL
    assert adapt_onchain_fills_for_reconciliation(fills, source_status=source)[0]["fill_id"] == "fill-1"
    assert (auth[0], metadata[0], fills[0]) == original


def test_root_policy_requires_lifecycle_and_metadata_or_fills_cannot_create_graph():
    for kwargs in (
        {"order_metadata_records": [{"order_id": 7, "symbol": "SOMI:USDso"}]},
        {"authenticated_orders": [{"order_id": 7, "symbol": "SOMI:USDso"}]},
        {"onchain_fills": [{"fill_id": "f", "order_id": 7}]},
    ):
        result = build_reconciliation_bridge_from_evidence(**kwargs)
        assert result.graph_count == 0
        assert result.reconciliation_complete is False


def test_confirmed_lifecycle_creates_one_graph_and_multiple_roots_are_sorted():
    records = [lifecycle(order_id=9, tx="0x" + "b" * 64), lifecycle(order_id=7)]
    result = build_reconciliation_bridge_from_evidence(lifecycle_records=records)
    assert result.root_lifecycle_count == 2
    assert result.eligible_root_count == 2
    assert [graph.root_order_id for graph in result.graphs] == [7, 9]
    assert result.authoritative is False
    assert result.reconciliation_complete is False


def test_root_rejection_for_missing_fingerprints_conflict_and_replacement_lineage():
    missing = lifecycle()
    missing.pop("request_fingerprint")
    conflicting = lifecycle(conflict=("account_identity_conflict",))
    replacement = lifecycle()
    replacement["replacement_transaction_hash"] = "0x" + "b" * 64
    result = build_reconciliation_bridge_from_evidence(lifecycle_records=[missing, conflicting, replacement])
    assert result.graph_count == 0
    assert result.eligible_root_count == 0


def test_matching_evidence_links_and_wrong_market_is_conflict():
    matching = build_reconciliation_bridge_from_evidence(
        lifecycle_records=[lifecycle()],
        order_metadata_records=[{"order_id": 7, "symbol": "SOMI:USDso", "market_address": POOL}],
        onchain_fills=[{"fill_id": "f", "order_id": 7, "pool_address": POOL, "transaction_hash": TX}],
        expected_market_address=POOL,
    )
    assert matching.graph_count == 1
    assert matching.conflicting_root_count == 0
    wrong = build_reconciliation_bridge_from_evidence(
        lifecycle_records=[lifecycle()],
        order_metadata_records=[{"order_id": 7, "symbol": "SOMI:USDso", "market_address": "0x" + "3" * 40}],
        expected_market_address=POOL,
    )
    assert wrong.conflicting_root_count == 1
    assert "market_identity_conflict" in wrong.conflicts


def test_authenticated_pagination_and_fill_duplicate_reorg_semantics():
    source = {"status": "available", "pagination_complete": False, "authority_status": "non_authoritative", "duplicate_count": 2, "reorg_status": "reorg_detected"}
    inventory = build_reconciliation_evidence_inventory(
        authenticated_orders=[{"order_id": 1}],
        authenticated_open_orders=[{"order_id": 1}],
        onchain_fills=[{"fill_id": "f", "order_id": 1}],
        onchain_fill_source_status=source,
        order_metadata_records=[{"order_id": 1}, {"order_id": 1, "status": "filled"}],
        order_metadata_source_status=source,
    )
    assert inventory.authenticated_pagination_status == "incomplete"
    assert inventory.onchain_fill_duplicate_count == 2
    assert inventory.onchain_fill_reorg_status == "reorg_detected"
    assert inventory.order_metadata_conflict_count == 1
    assert "authenticated_pagination_incomplete" in inventory.unresolved_reasons
    assert "reorg_status_unresolved" in inventory.unresolved_reasons


def test_duplicate_identical_evidence_does_not_change_fingerprint_and_order_is_irrelevant():
    one = build_reconciliation_evidence_bundle(
        lifecycle_records=[lifecycle()],
        order_metadata_records=[{"order_id": 7, "status": "open"}],
    )
    two = build_reconciliation_evidence_bundle(
        lifecycle_records=[lifecycle()],
        order_metadata_records=[{"order_id": 7, "status": "open"}, {"order_id": 7, "status": "open"}],
    )
    assert one.bundle_fingerprint == two.bundle_fingerprint
    duplicate_lifecycle = build_reconciliation_evidence_bundle(lifecycle_records=[lifecycle(), lifecycle()])
    assert duplicate_lifecycle.bundle_fingerprint == build_reconciliation_evidence_bundle(lifecycle_records=[lifecycle()]).bundle_fingerprint
    reversed_bundle = build_reconciliation_evidence_bundle(lifecycle_records=[lifecycle(order_id=7), lifecycle(order_id=9, tx="0x" + "b" * 64)])
    ordered_bundle = build_reconciliation_evidence_bundle(lifecycle_records=[lifecycle(order_id=9, tx="0x" + "b" * 64), lifecycle(order_id=7)])
    assert reversed_bundle.bundle_fingerprint == ordered_bundle.bundle_fingerprint


def test_fingerprints_change_for_pagination_reorg_and_conflicts_and_safe_diagnostics():
    base = build_reconciliation_evidence_bundle(lifecycle_records=[lifecycle()])
    pagination = build_reconciliation_evidence_bundle(lifecycle_records=[lifecycle()], authenticated_pagination_complete=True)
    reorg = build_reconciliation_evidence_bundle(lifecycle_records=[lifecycle()], fill_reorg_status="reorg_detected")
    conflict = build_reconciliation_evidence_bundle(lifecycle_records=[lifecycle()], conflicts=("metadata_conflict",))
    assert len({base.bundle_fingerprint, pagination.bundle_fingerprint, reorg.bundle_fingerprint, conflict.bundle_fingerprint}) == 4
    diagnostics = build_reconciliation_bridge_from_evidence(lifecycle_records=[lifecycle()]).safe_dict()
    text = repr(diagnostics)
    assert TX not in text and POOL not in text and OWNER not in text
    assert "raw_topics" not in text and "calldata" not in text


def test_capabilities_are_offline_only_and_bridge_has_no_io_imports():
    capabilities = describe_reconciliation_bridge_capabilities()
    assert capabilities["build_evidence_bundle"] == "available_offline"
    assert capabilities["fetch_authenticated_orders"] == "unavailable"
    assert capabilities["submit_transaction"] == "unavailable"
    source = Path("bot/execution/dreamdex_reconciliation_bridge.py").read_text(encoding="utf-8").lower()
    for forbidden in ("import requests", "import httpx", "import aiohttp", "import web3", "import subprocess", "os.environ", "eth_sendtransaction"):
        assert forbidden not in source
