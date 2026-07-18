from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path

import pytest

from bot.analytics.paper_burn_in_analyzer import (
    ALLOWED_RECORD_TYPES,
    analyze_paper_burn_in,
)


def _row(record_type: str, sequence: int, timestamp: datetime, **values):
    row = {
        "timestamp": timestamp.isoformat(),
        "symbol": "SOMI:USDso",
        "record_type": record_type,
        "sequence_number": sequence,
        "run_fingerprint": "abc123",
        "iteration_index": sequence,
        "iteration_ok": True,
        "notes": [],
    }
    row.update(values)
    return row


def _write(root: Path, rows: list[dict], name: str = "run.jsonl") -> Path:
    path = root / name
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    return path


def _valid_rows(*, summary_notes=None, portfolio_values=None):
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows = [_row("run_start", 1, start)]
    portfolio = portfolio_values or dict(
        best_bid="1", best_ask="1.01", mid_price="1.005", spread="0.01",
        cash_balance="99", base_position="1", equity="100.005", peak_equity="100.005",
        drawdown="0", fees_paid="0", open_orders_count=0,
        portfolio_risk_allowed=True, risk_max_drawdown="0.10",
        projected_shocked_drawdown="0.02", preemptive_halt_latched=False,
        hard_kill_latched=False, evaluated_open_orders_count=0,
    )
    rows.append(_row("market_snapshot", 2, start + timedelta(seconds=5), **portfolio))
    rows.append(_row("portfolio_snapshot", 3, start + timedelta(seconds=5), **portfolio))
    summary = _row(
        "run_summary", 4, start + timedelta(seconds=5),
        cash_balance=portfolio["cash_balance"], base_position=portfolio["base_position"],
        equity=portfolio["equity"], drawdown=portfolio["drawdown"], open_orders_count=0,
        notes=list(summary_notes or [
            "result=PASS", "audit.market_snapshots=1", "audit.accepted_snapshots=1",
            "audit.rejected_snapshots=0", "audit.strategy_intents=0",
            "audit.risk_approved_intents=0", "audit.risk_rejected_intents=0",
            "audit.fair_play_rejected_intents=0", "audit.paper_orders_created=0",
            "audit.paper_replacements=0", "audit.partial_fills=0", "audit.full_fills=0",
            "audit.open_orders_after_shutdown=0",
        ]),
    )
    rows.append(summary)
    return rows


def test_valid_file_reconstructs_counters_and_portfolio(tmp_path):
    summary = analyze_paper_burn_in(_write(tmp_path, _valid_rows()), repository_root=tmp_path)
    assert summary.integrity.status == "PASS"
    assert summary.market_quality.accepted_snapshots == 1
    assert summary.summary_counters_match is True
    assert summary.portfolio_reconstruction == "PASS"
    assert summary.ending_equity_match is True


def test_malformed_json_and_unknown_record_type_fail_closed(tmp_path):
    path = tmp_path / "bad.jsonl"
    path.write_text('{"record_type":"run_start"}\nnot-json\n', encoding="utf-8")
    result = analyze_paper_burn_in(path, repository_root=tmp_path)
    assert result.integrity.status == "FAIL"
    assert result.exit_code == 1
    unknown = _write(tmp_path, [{"record_type": "unknown"}], "unknown.jsonl")
    assert analyze_paper_burn_in(unknown, repository_root=tmp_path).integrity.status == "FAIL"


def test_order_sequence_timestamp_fingerprint_and_symbol_validation(tmp_path):
    rows = _valid_rows()
    rows[2]["sequence_number"] = 2
    rows[2]["timestamp"] = datetime(2025, 12, 31, tzinfo=timezone.utc).isoformat()
    rows[3]["run_fingerprint"] = "other"
    rows[1]["symbol"] = "OTHER"
    result = analyze_paper_burn_in(_write(tmp_path, rows), repository_root=tmp_path)
    assert result.integrity.status == "FAIL"
    assert "sequence_not_strictly_increasing" in result.integrity.errors
    assert "timestamps_not_monotonic" in result.integrity.errors
    assert "symbol_mismatch" in result.integrity.errors
    assert "run_fingerprint_mismatch" in result.integrity.errors


def test_missing_start_duplicate_summary_and_trailing_records_fail(tmp_path):
    rows = _valid_rows()
    rows.pop(0)
    rows.append(rows[-1].copy())
    result = analyze_paper_burn_in(_write(tmp_path, rows), repository_root=tmp_path)
    assert result.integrity.status == "FAIL"
    assert "run_start_count" in result.integrity.errors
    assert "run_summary_count" in result.integrity.errors
    assert "records_after_summary" in result.integrity.errors


def test_nonfinite_and_negative_price_fields_fail(tmp_path):
    rows = _valid_rows()
    rows[1]["best_bid"] = "NaN"
    rows[1]["best_ask"] = "-1"
    result = analyze_paper_burn_in(_write(tmp_path, rows), repository_root=tmp_path)
    assert result.integrity.status == "FAIL"
    assert "best_bid" in result.integrity.errors
    assert "best_ask" in result.integrity.errors


def test_market_quality_and_activity_are_explicit(tmp_path):
    rows = _valid_rows()
    rows[1]["record_type"] = "market_reject"
    rows[1]["notes"] = ["crossed_orderbook"]
    result = analyze_paper_burn_in(_write(tmp_path, rows), repository_root=tmp_path)
    assert result.market_quality.crossed_count == 1
    assert result.market_quality.status == "INVALID"
    assert result.market_quality.book_activity == "NO_ACTIVITY"


def test_counter_mismatch_is_reported_without_mutating_file(tmp_path):
    rows = _valid_rows(summary_notes=["result=PASS", "audit.market_snapshots=99"])
    path = _write(tmp_path, rows)
    before = path.read_bytes()
    result = analyze_paper_burn_in(path, repository_root=tmp_path)
    assert result.summary_counters_match is False
    assert "summary_consistency_failed" in result.blockers
    assert path.read_bytes() == before


def test_portfolio_equity_peak_drawdown_and_fees_checks(tmp_path):
    values = dict(
        best_bid="1", best_ask="1.01", mid_price="1.005", spread="0.01",
        cash_balance="99", base_position="1", equity="101", peak_equity="100",
        drawdown="-0.01", fees_paid="2", open_orders_count=0,
        portfolio_risk_allowed=True, risk_max_drawdown="0.10",
    )
    result = analyze_paper_burn_in(_write(tmp_path, _valid_rows(portfolio_values=values)), repository_root=tmp_path)
    assert result.portfolio_reconstruction == "FAIL"
    assert "drawdown_mismatch" in result.warnings


def test_risk_and_fair_play_violations_fail(tmp_path):
    rows = _valid_rows()
    rows.insert(2, _row(
        "strategy_intent", 3, datetime(2026, 1, 1, 0, 0, 5, tzinfo=timezone.utc),
        trade_intent_events=[{
            "execution_approved": True, "fair_play_allowed": False,
            "fair_play_reason": "", "submitted": True,
        }], portfolio_risk_allowed=True, risk_max_drawdown="0.10",
        risk_drawdown="0.10", hard_kill_latched=True, portfolio_risk_latched=True,
    ))
    for index, row in enumerate(rows, 1):
        row["sequence_number"] = index
    result = analyze_paper_burn_in(_write(tmp_path, rows), repository_root=tmp_path)
    assert result.fair_play.status == "FAIL"
    assert result.risk.status == "FAIL"
    assert result.exit_code == 1


def test_privacy_scan_reports_only_categories_and_line_numbers(tmp_path):
    rows = _valid_rows()
    rows[1]["notes"] = ["https://secret.example"]
    result = analyze_paper_burn_in(_write(tmp_path, rows), repository_root=tmp_path)
    assert result.privacy_status == "FAIL"
    assert result.privacy_findings[0][0] == "url"
    assert "secret.example" not in repr(result)


def test_path_traversal_symlink_and_size_are_rejected(tmp_path, monkeypatch):
    path = _write(tmp_path, _valid_rows())
    with pytest.raises(ValueError):
        analyze_paper_burn_in("../run.jsonl", repository_root=tmp_path)
    link = tmp_path / "link.jsonl"
    try:
        link.symlink_to(path)
    except (OSError, NotImplementedError):
        pass
    else:
        with pytest.raises(ValueError):
            analyze_paper_burn_in("link.jsonl", repository_root=tmp_path)
    from bot.analytics import paper_burn_in_analyzer as analyzer
    monkeypatch.setattr(analyzer, "MAX_FILE_BYTES", 1)
    with pytest.raises(ValueError):
        analyze_paper_burn_in(path, repository_root=tmp_path)


def test_cli_is_offline_and_accepts_only_input(tmp_path, capsys, monkeypatch):
    from scripts import analyze_paper_burn_in as cli
    path = _write(tmp_path, _valid_rows())
    monkeypatch.chdir(tmp_path)
    assert cli.main(["--input", str(path.relative_to(tmp_path))]) == 0
    output = capsys.readouterr().out
    assert "network access used: NO" in output
    assert "http" not in output.lower()
    with pytest.raises(SystemExit):
        cli.main(["--input", "run.jsonl", "--rpc-url", "https://example"])


def test_allowlist_is_explicit():
    assert "run_start" in ALLOWED_RECORD_TYPES
    assert "run_summary" in ALLOWED_RECORD_TYPES
    assert "unknown" not in ALLOWED_RECORD_TYPES
