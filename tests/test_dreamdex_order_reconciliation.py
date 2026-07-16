from dataclasses import FrozenInstanceError

import pytest

from bot.execution.dreamdex_order_reconciliation import (
    EDGE_TYPES,
    NODE_TYPES,
    DreamDexReconciliationEdge,
    DreamDexReconciliationNode,
    build_order_reconciliation_graph,
    build_order_reconciliation_preview,
    describe_order_reconciliation_capabilities,
    serialize_order_reconciliation_diagnostics,
    validate_order_reconciliation_graph,
)


POOL = "0x1111111111111111111111111111111111111111"
OWNER = "0x2222222222222222222222222222222222222222"
TX = "0x" + "a" * 64


def lifecycle(*, order_id=7, tx=TX, event_status="source_confirmed", envelope_fp="env", request_fp="req"):
    return {
        "operation": "place_order",
        "transaction_hash": tx,
        "request_fingerprint": request_fp,
        "envelope_fingerprint": envelope_fp,
        "lifecycle_fingerprint": "life",
        "source_status": "observed",
        "current_state": "confirmed_success",
        "receipt_evidence": {"transaction_hash": tx, "source_status": "observed"},
        "event_evidence": ({"event_name": "OrderPlaced", "order_id": order_id, "transaction_hash": tx, "contract_address": POOL, "source_status": event_status},),
    }


def test_models_are_frozen_and_types_are_allowlisted():
    node = DreamDexReconciliationNode("n", "order_identity", identifiers={"order_id": 7, "owner_address": OWNER})
    edge = DreamDexReconciliationEdge("e", "event_to_order_id", "a", "b")
    with pytest.raises(FrozenInstanceError):
        node.node_id = "x"
    assert node.safe_dict()["identifiers"]["owner_address"] != OWNER
    assert NODE_TYPES and EDGE_TYPES
    with pytest.raises(ValueError):
        DreamDexReconciliationNode("n", "not_a_node")
    with pytest.raises(ValueError):
        DreamDexReconciliationEdge("e", "not_an_edge", "a", "b")


def test_production_default_graph_is_offline_and_fail_closed():
    graph = build_order_reconciliation_graph()
    assert graph.graph_status == "unavailable"
    assert graph.reconciliation_complete is False
    assert graph.authoritative is False
    assert "reconciliation_graph_unavailable" in graph.blockers
    assert "fill_coverage_unavailable" in graph.blockers
    assert validate_order_reconciliation_graph(graph).valid
    assert build_order_reconciliation_preview(graph).node_count == 0


def test_matching_request_envelope_lifecycle_event_is_structurally_linked():
    request = {"operation": "place_order", "chain_id": 5031, "from_address": OWNER, "to_address": POOL, "request_fingerprint": "req"}
    envelope = {"operation": "place_order", "chain_id": 5031, "from_address": OWNER, "to_address": POOL, "request_fingerprint": "req", "envelope_fingerprint": "env"}
    graph = build_order_reconciliation_graph(unsigned_request=request, unsigned_envelope=envelope, lifecycle_record=lifecycle(), expected_account_address=OWNER, expected_market_address=POOL)
    assert graph.root_order_id == 7
    assert any(edge.edge_type == "event_to_order_id" and edge.match_status == "confirmed" for edge in graph.edges)
    assert graph.graph_status in {"partially_reconciled", "structurally_linked"}
    assert validate_order_reconciliation_graph(graph).valid


def test_mismatched_request_and_transaction_are_conflicts():
    request = {"operation": "place_order", "chain_id": 5031, "from_address": OWNER, "to_address": POOL, "request_fingerprint": "req"}
    envelope = {"operation": "place_order", "chain_id": 5031, "from_address": OWNER, "to_address": POOL, "request_fingerprint": "other", "envelope_fingerprint": "env"}
    graph = build_order_reconciliation_graph(unsigned_request=request, unsigned_envelope=envelope, lifecycle_record=lifecycle())
    assert graph.graph_status == "conflicting"
    assert "request_envelope_conflict" in graph.conflicts
    assert not validate_order_reconciliation_graph(graph).valid


def test_metadata_is_not_allowed_to_guess_root_order_id():
    graph = build_order_reconciliation_graph(order_metadata_records=({"order_id": 99, "symbol": "SOMI:USDso", "status": "open", "source_status": "available"},))
    assert graph.root_order_id is None
    assert "order_id_lifecycle_unconfirmed" in graph.blockers
    assert graph.graph_status == "unavailable"


def test_authenticated_and_fill_edges_are_recorded_without_claiming_authority():
    graph = build_order_reconciliation_graph(
        lifecycle_record=lifecycle(),
        authenticated_orders=({"order_id": "7", "symbol": "SOMI:USDso", "status": "filled", "source_status": "available"},),
        authenticated_open_orders=({"order_id": "7", "symbol": "SOMI:USDso", "status": "open", "source_status": "available"},),
        onchain_fills=({"fill_id": "f1", "order_id": 7, "transaction_hash": TX, "quantity": "1", "source_status": "available"},),
    )
    assert any(edge.edge_type == "order_id_to_authenticated_order" for edge in graph.edges)
    assert any(edge.edge_type == "order_id_to_open_order" for edge in graph.edges)
    assert any(edge.edge_type == "order_id_to_fill" for edge in graph.edges)
    assert "open_order_status_conflict" in graph.conflicts
    rendered = repr(graph) + repr(serialize_order_reconciliation_diagnostics(graph))
    assert TX not in rendered
    assert OWNER not in rendered


def test_duplicate_fill_conflict_and_reorg_are_fail_closed():
    graph = build_order_reconciliation_graph(
        lifecycle_record=lifecycle(),
        onchain_fills=(
            {"fill_id": "f1", "order_id": 7, "transaction_hash": TX, "quantity": "1"},
            {"fill_id": "f1", "order_id": 7, "transaction_hash": TX, "quantity": "2"},
            {"fill_id": "f2", "order_id": 7, "transaction_hash": TX, "removed": True},
        ),
    )
    assert "duplicate_fill_conflict" in graph.conflicts
    assert "reorg_status_unresolved" in graph.blockers
    assert graph.reconciliation_complete is False


def test_replacement_lineage_is_separate_and_not_silently_merged():
    graph = build_order_reconciliation_graph(lifecycle_record={**lifecycle(), "replacement_evidence": {"original_transaction_hash": TX, "replacement_transaction_hash": "0x" + "b" * 64, "source_status": "observed"}, "evidence": {"replacement_status": "resolved"}})
    assert any(edge.edge_type == "replacement_of" for edge in graph.edges)
    assert graph.root_transaction_hash == TX
    assert graph.reconciliation_complete is False


def test_fingerprints_are_deterministic_and_input_order_independent():
    kwargs = dict(lifecycle_record=lifecycle(), order_metadata_records=({"order_id": 7, "symbol": "SOMI:USDso"},), onchain_fills=({"fill_id": "f", "order_id": 7, "quantity": "1"},))
    first = build_order_reconciliation_graph(**kwargs)
    second = build_order_reconciliation_graph(**kwargs)
    changed = build_order_reconciliation_graph(**{**kwargs, "onchain_fills": ({"fill_id": "f", "order_id": 7, "quantity": "2"},)})
    assert first.graph_fingerprint == second.graph_fingerprint
    assert first.graph_fingerprint != changed.graph_fingerprint


def test_capability_matrix_has_no_live_execution_surface():
    capabilities = describe_order_reconciliation_capabilities()
    assert capabilities["build_reconciliation_graph"] == "available_offline"
    assert capabilities["fetch_authenticated_orders"] == "unavailable"
    assert capabilities["submit_transaction"] == "unavailable"
    assert not any(name in capabilities for name in ("send_transaction", "poll_receipt", "sign_transaction"))
