import json
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from bot.analytics.paper_run_analyzer import PaperRunAnalyzer


def make_record(
    timestamp: str,
    *,
    market_safe=True,
    mid_price: str | None = "0.1040",
    fills_count: int = 0,
    submitted_orders_count: int = 1,
    open_orders_count: int = 1,
    cash_balance: str = "150",
    base_position: str = "0",
    equity: str = "150",
    realized_pnl: str = "0",
    unrealized_pnl: str = "0",
    drawdown: str = "0",
    total_volume: str = "0",
    weekly_volume: str = "0",
    estimated_score: str = "0",
    raffle_tickets: int = 0,
):
    return {
        "timestamp": timestamp,
        "symbol": "SOMI:USDso",
        "best_bid": "0.1039",
        "best_ask": "0.1041",
        "mid_price": mid_price,
        "spread": "0.0002",
        "market_safe": market_safe,
        "market_safety_reason": "ok" if market_safe else "spread_too_wide",
        "intents_count": 1,
        "decisions_count": 1,
        "fills_count": fills_count,
        "submitted_orders_count": submitted_orders_count,
        "open_orders_count": open_orders_count,
        "cash_balance": cash_balance,
        "base_position": base_position,
        "equity": equity,
        "realized_pnl": realized_pnl,
        "unrealized_pnl": unrealized_pnl,
        "drawdown": drawdown,
        "total_volume": total_volume,
        "weekly_volume": weekly_volume,
        "estimated_score": estimated_score,
        "raffle_tickets": raffle_tickets,
        "notes": [],
    }


def write_jsonl(path, records):
    with path.open("w", encoding="utf-8") as file:
        for record in records:
            file.write(json.dumps(record))
            file.write("\n")


def test_analyzer_builds_summary_from_jsonl(tmp_path):
    path = tmp_path / "paper_run.jsonl"

    write_jsonl(
        path,
        [
            make_record(
                "2026-07-13T12:00:00+00:00",
            ),
            make_record(
                "2026-07-13T12:01:00+00:00",
                fills_count=1,
                submitted_orders_count=2,
                open_orders_count=1,
                cash_balance="145",
                base_position="48.1",
                equity="150.2",
                unrealized_pnl="0.2",
                total_volume="5",
                weekly_volume="5",
                estimated_score="6",
            ),
        ],
    )

    summary = PaperRunAnalyzer().analyze_file(path)

    assert summary.records_count == 2
    assert summary.duration_seconds == 60
    assert summary.fills_count == 1
    assert summary.submitted_orders_count == 3

    assert summary.final_cash_balance == Decimal("145")
    assert summary.final_base_position == Decimal("48.1")
    assert summary.final_equity == Decimal("150.2")
    assert summary.final_unrealized_pnl == Decimal("0.2")
    assert summary.final_total_volume == Decimal("5")
    assert summary.final_weekly_volume == Decimal("5")
    assert summary.final_estimated_score == Decimal("6")
    assert summary.final_open_orders == 1


def test_analyzer_counts_market_safety_states():
    records = [
        make_record(
            "2026-07-13T12:00:00+00:00",
            market_safe=True,
        ),
        make_record(
            "2026-07-13T12:01:00+00:00",
            market_safe=False,
        ),
        make_record(
            "2026-07-13T12:02:00+00:00",
            market_safe=None,
        ),
    ]

    summary = PaperRunAnalyzer().analyze_records(records)

    assert summary.safe_market_count == 1
    assert summary.unsafe_market_count == 1
    assert summary.unknown_market_count == 1


def test_analyzer_calculates_max_drawdown_and_price_range():
    records = [
        make_record(
            "2026-07-13T12:00:00+00:00",
            mid_price="0.1040",
            drawdown="0.01",
        ),
        make_record(
            "2026-07-13T12:01:00+00:00",
            mid_price="0.1020",
            drawdown="0.03",
        ),
        make_record(
            "2026-07-13T12:02:00+00:00",
            mid_price="0.1060",
            drawdown="0.02",
        ),
    ]

    summary = PaperRunAnalyzer().analyze_records(records)

    assert summary.max_drawdown == Decimal("0.03")
    assert summary.min_mid_price == Decimal("0.1020")
    assert summary.max_mid_price == Decimal("0.1060")


def test_analyzer_supports_z_timestamp():
    records = [
        make_record("2026-07-13T12:00:00Z"),
    ]

    summary = PaperRunAnalyzer().analyze_records(records)

    assert summary.first_timestamp == datetime(
        2026,
        7,
        13,
        12,
        0,
        tzinfo=timezone.utc,
    )


def test_analyzer_rejects_empty_file(tmp_path):
    path = tmp_path / "empty.jsonl"
    path.write_text("", encoding="utf-8")

    with pytest.raises(ValueError, match="contains no records"):
        PaperRunAnalyzer().analyze_file(path)


def test_analyzer_reports_invalid_json_line(tmp_path):
    path = tmp_path / "invalid.jsonl"

    path.write_text(
        '{"timestamp": "2026-07-13T12:00:00+00:00"}\n'
        'this is not json\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="line 2"):
        PaperRunAnalyzer().analyze_file(path)