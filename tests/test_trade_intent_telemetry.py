from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from bot.analytics.paper_run_analyzer import PaperRunAnalyzer
from bot.analytics.paper_run_recorder import PaperRunRecord
from scripts.analyze_paper_run import print_summary


def test_recorder_and_analyzer_aggregate_trade_intent_purposes(capsys):
    record = PaperRunRecord(
        timestamp=datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc),
        symbol="SOMI:USDso",
        trade_intent_events=[
            {
                "timestamp": datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc),
                "purpose": "entry",
                "submitted": True,
                "fair_play_allowed": True,
                "execution_approved": True,
                "price": Decimal("1"),
            },
            {
                "purpose": "inventory_rebalance",
                "submitted": False,
                "fair_play_allowed": False,
                "execution_approved": None,
            },
            {
                "submitted": False,
                "fair_play_allowed": None,
                "execution_approved": False,
            },
        ],
        confirmed_fill_events=[
            {"side": "buy", "purpose": "entry"},
            {"side": "sell"},
        ],
    )
    data = record.to_dict()
    assert data["trade_intent_events"][0]["timestamp"] == "2026-07-13T12:00:00+00:00"
    assert data["trade_intent_events"][0]["price"] == "1"

    summary = PaperRunAnalyzer().analyze_records([data])
    assert summary.generated_intent_count == 3
    assert summary.submitted_intent_count == 1
    assert summary.fair_play_rejected_intent_count == 1
    assert summary.execution_rejected_intent_count == 1
    assert summary.generated_intent_purpose_counts == {
        "entry": 1,
        "inventory_rebalance": 1,
        "unknown": 1,
    }
    assert summary.confirmed_fill_purpose_counts == {"entry": 1, "unknown": 1}
    assert summary.unknown_purpose_intent_count == 1
    assert summary.unknown_purpose_fill_count == 1

    print_summary(Path("paper_run.jsonl"), summary)
    output = capsys.readouterr().out
    assert "Trade intent audit:" in output
    assert "Generated intents        : 3" in output
    assert "Confirmed-fill purposes:" in output


def test_analyzer_supports_old_records_without_intent_telemetry():
    record = {
        "timestamp": "2026-07-13T12:00:00+00:00",
        "symbol": "SOMI:USDso",
    }
    summary = PaperRunAnalyzer().analyze_records([record])
    assert summary.generated_intent_count == 0
    assert summary.generated_intent_purpose_counts == {}
    assert summary.confirmed_fill_purpose_counts == {}
