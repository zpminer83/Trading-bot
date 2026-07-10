from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from bot.analytics.paper_run_analyzer import PaperRunAnalyzer
from bot.analytics.paper_run_recorder import PaperRunRecord
from scripts.analyze_paper_run import print_summary


def test_signal_fields_serialize_and_analyze(capsys):
    timestamp = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)
    records = [
        PaperRunRecord(
            timestamp=timestamp,
            symbol="SOMI:USDso",
            signal_state="bullish",
            signal_reason="bullish_confirmation",
            signal_sample_count=4,
            signal_spread_bps=Decimal("10"),
            signal_bid_depth=Decimal("20"),
            signal_ask_depth=Decimal("10"),
            signal_depth_imbalance=Decimal("0.333"),
            signal_microprice=Decimal("101"),
            signal_microprice_edge_bps=Decimal("2"),
            signal_one_step_return_bps=Decimal("3"),
            signal_rolling_momentum_bps=Decimal("4"),
            signal_confidence=Decimal("0.75"),
        ).to_dict(),
        PaperRunRecord(
            timestamp=timestamp,
            symbol="SOMI:USDso",
            signal_state="bearish",
            signal_reason="bearish_confirmation",
            signal_spread_bps=Decimal("20"),
            signal_depth_imbalance=Decimal("-0.25"),
            signal_rolling_momentum_bps=Decimal("-5"),
            signal_confidence=Decimal("0.5"),
        ).to_dict(),
        PaperRunRecord(
            timestamp=timestamp,
            symbol="SOMI:USDso",
            signal_state="warming_up",
            signal_reason="minimum_samples_not_reached",
            signal_confidence=Decimal("0"),
        ).to_dict(),
    ]

    assert records[0]["signal_microprice"] == "101"
    assert records[0]["signal_confidence"] == "0.75"
    summary = PaperRunAnalyzer().analyze_records(records)

    assert summary.bullish_signal_count == 1
    assert summary.bearish_signal_count == 1
    assert summary.warming_up_signal_count == 1
    assert summary.neutral_signal_count == 0
    assert summary.unknown_signal_count == 0
    assert summary.maximum_signal_confidence == Decimal("0.75")
    assert summary.average_signal_confidence == Decimal("1.25") / Decimal("3")
    assert summary.minimum_depth_imbalance == Decimal("-0.25")
    assert summary.maximum_depth_imbalance == Decimal("0.333")
    assert summary.minimum_rolling_momentum_bps == Decimal("-5")
    assert summary.maximum_rolling_momentum_bps == Decimal("4")
    assert summary.average_spread_bps == Decimal("15")
    assert summary.signal_reason_counts["bullish_confirmation"] == 1

    print_summary(Path("paper_run.jsonl"), summary)
    output = capsys.readouterr().out
    assert "Order-book signal:" in output
    assert "Bullish records     : 1" in output
    assert "uncalibrated diagnostic score" in output


def test_analyzer_supports_records_without_signal_fields():
    summary = PaperRunAnalyzer().analyze_records(
        [{"timestamp": "2026-07-13T12:00:00+00:00", "symbol": "SOMI:USDso"}]
    )
    assert summary.unknown_signal_count == 1
    assert summary.bullish_signal_count == 0
    assert summary.maximum_signal_confidence is None
