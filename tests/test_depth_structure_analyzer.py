import json
import socket
from datetime import datetime, timezone
from decimal import Decimal

from bot.analytics.depth_structure_analyzer import DepthStructureAnalyzer
from bot.analytics.paper_run_analyzer import PaperRunAnalyzer
from bot.analytics.paper_run_recorder import PaperRunRecord
from bot.market.models import OrderBook, OrderBookLevel
from bot.market.orderbook_depth_diagnostics import (
    calculate_orderbook_depth_diagnostics,
)


def make_book(bids, asks):
    return OrderBook(
        symbol="SOMI:USDso",
        bids=[OrderBookLevel(Decimal(str(price)), Decimal(str(quantity))) for price, quantity in bids],
        asks=[OrderBookLevel(Decimal(str(price)), Decimal(str(quantity))) for price, quantity in asks],
        timestamp=1,
    )


def test_multi_depth_calculation_and_positive_l1_negative_l5_case():
    book = make_book(
        [(99 - i, quantity) for i, quantity in enumerate([3, 1, 1, 1, 1, 1, 1])],
        [(101 + i, quantity) for i, quantity in enumerate([1, 2, 2, 2, 2, 1, 1])],
    )
    diagnostic = calculate_orderbook_depth_diagnostics(
        book,
        observed_at=datetime(2026, 7, 14, tzinfo=timezone.utc),
    )
    assert diagnostic.imbalance_l1 == Decimal("0.5")
    assert diagnostic.imbalance_l5 == Decimal("-0.125")
    assert diagnostic.microprice_edge_bps > 0
    assert diagnostic.l1_edge_sign_consistent is True
    assert diagnostic.bid_depth_l1 == Decimal("3")
    assert diagnostic.ask_depth_l1 == Decimal("1")
    assert diagnostic.bid_depth_l5 == Decimal("7")
    assert diagnostic.ask_depth_l5 == Decimal("9")
    assert diagnostic.bid_depth_concentration_l2_to_l5 == Decimal("4") / Decimal("7")
    assert diagnostic.ask_depth_concentration_l2_to_l5 == Decimal("8") / Decimal("9")


def test_balanced_zero_depth_and_fewer_than_ten_levels():
    balanced = make_book([(99, 2), (98, 1)], [(101, 2), (102, 1)])
    diagnostic = calculate_orderbook_depth_diagnostics(balanced)
    assert diagnostic.imbalance_l1 == 0
    assert diagnostic.microprice_edge_bps == 0
    assert diagnostic.imbalance_l10 == diagnostic.imbalance_l2
    assert diagnostic.l1_edge_sign_consistent is True

    zero = make_book([(99, 0)], [(101, 0)])
    zero_diagnostic = calculate_orderbook_depth_diagnostics(zero)
    assert zero_diagnostic.imbalance_l1 is None
    assert zero_diagnostic.microprice_edge_bps is None
    assert zero_diagnostic.l1_edge_sign_consistent is None


def test_depth_changes_do_not_change_top_level_microprice_edge():
    first = make_book([(99, 3), (98, 1), (97, 1)], [(101, 1), (102, 2), (103, 2)])
    second = make_book([(99, 3), (98, 5), (97, 5)], [(101, 1), (102, 1), (103, 1)])
    first_diagnostic = calculate_orderbook_depth_diagnostics(first)
    second_diagnostic = calculate_orderbook_depth_diagnostics(second)
    assert first_diagnostic.microprice_edge_bps == second_diagnostic.microprice_edge_bps
    assert first_diagnostic.imbalance_l5 != second_diagnostic.imbalance_l5


def test_sign_invariant_for_positive_negative_and_balanced_l1():
    books = (
        make_book([(99, 3)], [(101, 1)]),
        make_book([(99, 1)], [(101, 3)]),
        make_book([(99, 1)], [(101, 1)]),
    )
    diagnostics = [calculate_orderbook_depth_diagnostics(book) for book in books]
    assert diagnostics[0].imbalance_l1 > 0 and diagnostics[0].microprice_edge_bps > 0
    assert diagnostics[1].imbalance_l1 < 0 and diagnostics[1].microprice_edge_bps < 0
    assert diagnostics[2].imbalance_l1 == 0 and diagnostics[2].microprice_edge_bps == 0
    assert all(item.l1_edge_sign_consistent is True for item in diagnostics)


def test_recorder_and_paper_analyzer_depth_telemetry(tmp_path):
    timestamp = datetime(2026, 7, 14, tzinfo=timezone.utc)
    record = PaperRunRecord(
        timestamp=timestamp,
        symbol="SOMI:USDso",
        depth_imbalance_l1=Decimal("0.5"),
        depth_imbalance_l5=Decimal("-0.125"),
        depth_bid_l1=Decimal("3"),
        depth_ask_l1=Decimal("1"),
        depth_bid_l5=Decimal("7"),
        depth_ask_l5=Decimal("9"),
        l1_edge_sign_consistent=True,
        bid_depth_concentration_l2_to_l5=Decimal("0.5"),
        ask_depth_concentration_l2_to_l5=Decimal("0.8"),
    )
    data = record.to_dict()
    assert data["depth_imbalance_l1"] == "0.5"
    assert data["depth_imbalance_l5"] == "-0.125"
    assert data["bid_depth_concentration_l2_to_l5"] == "0.5"
    path = tmp_path / "depth.jsonl"
    path.write_text(json.dumps(data) + "\n", encoding="utf-8")
    summary = PaperRunAnalyzer().analyze_file(path)
    assert summary.positive_imbalance_l1_count == 1
    assert summary.negative_imbalance_l5_count == 1
    assert summary.l1_positive_l5_negative_count == 1
    assert summary.sign_consistency_failure_count == 0
    assert summary.average_imbalance_l1 == Decimal("0.5")


def test_depth_analyzer_aggregates_and_supports_old_records_without_network(tmp_path, monkeypatch):
    monkeypatch.setattr(socket, "create_connection", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("offline only")))
    records = [
        {
            "timestamp": "2026-07-14T00:00:00+00:00",
            "depth_imbalance_l1": "0.5",
            "depth_imbalance_l2": "0.1",
            "depth_imbalance_l3": "-0.1",
            "depth_imbalance_l5": "-0.2",
            "depth_imbalance_l10": "-0.3",
            "depth_bid_l1": "3",
            "depth_bid_l5": "7",
            "depth_ask_l1": "1",
            "depth_ask_l5": "9",
            "bid_depth_concentration_l2_to_l5": "0.5",
            "ask_depth_concentration_l2_to_l5": "0.8",
            "l1_edge_sign_consistent": True,
        },
        {"timestamp": "2026-07-14T00:00:01+00:00"},
    ]
    first = tmp_path / "first.jsonl"
    second = tmp_path / "second.jsonl"
    first.write_text("\n".join(json.dumps(item) for item in records) + "\n", encoding="utf-8")
    second.write_text(json.dumps(records[0]) + "\n", encoding="utf-8")
    summary = DepthStructureAnalyzer().analyze_files([first, second])
    assert summary.record_count == 3
    assert summary.depth_record_count == 2
    assert summary.distributions["l1"].count == 2
    assert summary.sign_counts["l1"]["positive"] == 2
    assert summary.l1_positive_l5_negative_count == 2
    assert summary.average_ask_depth_concentration_l2_to_l5 == Decimal("0.8")
    assert summary.ask_depth_grows_faster_percentage == Decimal("100")
    assert len(summary.per_file) == 2
