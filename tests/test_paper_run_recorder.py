import json
from datetime import datetime, timezone
from decimal import Decimal

from bot.analytics.paper_run_recorder import (
    PaperRunRecord,
    PaperRunRecorder,
)


def test_paper_run_record_serializes_decimal_and_datetime():
    record = PaperRunRecord(
        timestamp=datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc),
        symbol="SOMI:USDso",
        best_bid=Decimal("0.1039"),
        best_ask=Decimal("0.1041"),
        mid_price=Decimal("0.1040"),
        spread=Decimal("0.0002"),
        market_safe=True,
        market_safety_reason="ok",
        market_fresh=True,
        market_freshness_reason="ok",
        exchange_age_seconds=Decimal("1.25"),
        unchanged_seconds=Decimal("2.50"),
        cash_balance=Decimal("150"),
        equity=Decimal("150"),
        weekly_volume=Decimal("10.10"),
        estimated_score=Decimal("12.12"),
    )

    data = record.to_dict()

    assert data["timestamp"] == "2026-07-13T12:00:00+00:00"
    assert data["symbol"] == "SOMI:USDso"
    assert data["best_bid"] == "0.1039"
    assert data["best_ask"] == "0.1041"
    assert data["weekly_volume"] == "10.10"
    assert data["estimated_score"] == "12.12"
    assert data["market_safe"] is True
    assert data["market_safety_reason"] == "ok"
    assert data["market_fresh"] is True
    assert data["market_freshness_reason"] == "ok"
    assert data["exchange_age_seconds"] == "1.25"
    assert data["unchanged_seconds"] == "2.50"


def test_paper_run_recorder_appends_records():
    recorder = PaperRunRecorder()

    record = PaperRunRecord(
        timestamp=datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc),
        symbol="SOMI:USDso",
    )

    recorder.append(record)

    assert recorder.count == 1
    assert recorder.latest == record


def test_paper_run_recorder_latest_is_none_when_empty():
    recorder = PaperRunRecorder()

    assert recorder.count == 0
    assert recorder.latest is None


def test_paper_run_recorder_writes_jsonl(tmp_path):
    recorder = PaperRunRecorder()

    recorder.append(
        PaperRunRecord(
            timestamp=datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc),
            symbol="SOMI:USDso",
            best_bid=Decimal("0.1039"),
            market_safe=True,
            market_safety_reason="ok",
            cash_balance=Decimal("150"),
            equity=Decimal("150"),
        )
    )

    recorder.append(
        PaperRunRecord(
            timestamp=datetime(2026, 7, 13, 12, 1, tzinfo=timezone.utc),
            symbol="SOMI:USDso",
            best_bid=Decimal("0.1040"),
            market_safe=True,
            market_safety_reason="ok",
            cash_balance=Decimal("150"),
            equity=Decimal("150"),
        )
    )

    output_path = tmp_path / "paper_run.jsonl"

    recorder.write_jsonl(output_path)

    lines = output_path.read_text(encoding="utf-8").splitlines()

    assert len(lines) == 2

    first = json.loads(lines[0])
    second = json.loads(lines[1])

    assert first["timestamp"] == "2026-07-13T12:00:00+00:00"
    assert first["symbol"] == "SOMI:USDso"
    assert first["best_bid"] == "0.1039"

    assert second["timestamp"] == "2026-07-13T12:01:00+00:00"
    assert second["best_bid"] == "0.1040"
