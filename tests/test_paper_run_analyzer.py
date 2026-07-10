import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from bot.analytics.paper_run_analyzer import PaperRunAnalyzer
from scripts.analyze_paper_run import print_summary


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


def test_analyzer_summarizes_run_reliability(capsys):
    records = [
        make_record("2026-07-13T12:00:00+00:00"),
        make_record("2026-07-13T12:01:00+00:00"),
        make_record("2026-07-13T12:02:00+00:00"),
    ]
    records[0].update(iteration_ok=True)
    records[1].update(
        iteration_ok=False,
        error_type="TimeoutError",
        error_message="request timed out",
    )
    records[2].update(
        iteration_ok=False,
        error_type="TimeoutError",
        error_message="request timed out again",
    )

    summary = PaperRunAnalyzer().analyze_records(records)

    assert summary.successful_iterations == 1
    assert summary.failed_iterations == 2
    assert summary.success_rate == Decimal("1") / Decimal("3")
    assert summary.error_type_counts == {"TimeoutError": 2}

    print_summary(Path("paper_run.jsonl"), summary)
    output = capsys.readouterr().out

    assert "Run reliability:" in output
    assert "Successful iterations: 1" in output
    assert "Failed iterations    : 2" in output
    assert "Success rate         : 33.33%" in output
    assert "TimeoutError: 2" in output


def test_analyzer_treats_old_records_as_successful_iterations():
    records = [
        make_record("2026-07-13T12:00:00+00:00"),
        make_record("2026-07-13T12:01:00+00:00"),
    ]

    summary = PaperRunAnalyzer().analyze_records(records)

    assert summary.successful_iterations == 2
    assert summary.failed_iterations == 0
    assert summary.success_rate == Decimal("1")
    assert summary.error_type_counts == {}


def test_analyzer_summarizes_market_freshness(capsys):
    records = [
        make_record("2026-07-13T12:00:00+00:00"),
        make_record("2026-07-13T12:01:00+00:00"),
        make_record("2026-07-13T12:02:00+00:00"),
        make_record("2026-07-13T12:03:00+00:00"),
    ]

    records[0].update(
        market_fresh=True,
        market_freshness_reason="ok",
        exchange_age_seconds="1.25",
        unchanged_seconds="0",
    )
    records[1].update(
        market_fresh=False,
        market_freshness_reason="repeated_snapshot",
        exchange_age_seconds="4.5",
        unchanged_seconds="31.75",
    )
    records[2].update(
        market_fresh=False,
        market_freshness_reason="repeated_snapshot",
        exchange_age_seconds=None,
        unchanged_seconds="35",
    )
    records[3].update(
        market_fresh=None,
        market_freshness_reason=None,
        exchange_age_seconds=None,
        unchanged_seconds=None,
    )

    summary = PaperRunAnalyzer().analyze_records(records)

    assert summary.fresh_market_count == 1
    assert summary.stale_market_count == 2
    assert summary.unknown_freshness_count == 1
    assert summary.max_exchange_age_seconds == Decimal("4.5")
    assert summary.max_unchanged_seconds == Decimal("35")
    assert summary.freshness_reason_counts == {
        "ok": 1,
        "repeated_snapshot": 2,
    }

    print_summary(Path("paper_run.jsonl"), summary)
    output = capsys.readouterr().out

    assert "Market freshness:" in output
    assert "Fresh records   : 1" in output
    assert "Stale records   : 2" in output
    assert "Unknown records : 1" in output
    assert "Max exchange age: 4.5s" in output
    assert "Max unchanged time: 35s" in output
    assert "repeated_snapshot: 2" in output


def test_analyzer_supports_records_without_freshness_fields():
    records = [
        make_record("2026-07-13T12:00:00+00:00"),
        make_record("2026-07-13T12:01:00+00:00"),
    ]

    summary = PaperRunAnalyzer().analyze_records(records)

    assert summary.fresh_market_count == 0
    assert summary.stale_market_count == 0
    assert summary.unknown_freshness_count == 2
    assert summary.max_exchange_age_seconds is None
    assert summary.max_unchanged_seconds is None
    assert summary.freshness_reason_counts == {}


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
