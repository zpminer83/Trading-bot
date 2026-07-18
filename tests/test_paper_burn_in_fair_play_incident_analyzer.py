from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest

from bot.analytics.paper_burn_in_fair_play_incident_analyzer import (
    PaperBurnInFairPlayReason,
    analyze_paper_burn_in_fair_play_incident,
    normalize_fair_play_reason,
)


def _write(path, *, reason="opposite_side_cooldown", halt=True, missing=False, bad_order=False, after=False, final_open=0):
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows = [{
        "timestamp": start.isoformat(), "symbol": "SOMI:USDso", "record_type": "run_start",
        "sequence_number": 1, "run_fingerprint": "run-a", "configuration_fingerprint": "cfg-a",
    }]
    event = {
        "sequence_number": 7, "fair_play_allowed": False,
        "fair_play_reason": None if missing else reason,
        "resulting_order_id": 99 if bad_order else None,
    }
    rows.append({
        "timestamp": (start + timedelta(seconds=1)).isoformat(), "symbol": "SOMI:USDso",
        "record_type": "strategy_intent", "sequence_number": 2, "run_fingerprint": "run-a",
        "trade_intent_events": [event], "base_position": "10", "open_orders_count": 1,
    })
    if halt:
        rows.append({
            "timestamp": (start + timedelta(seconds=2)).isoformat(), "symbol": "SOMI:USDso",
            "record_type": "fair_play_decision", "sequence_number": 3, "run_fingerprint": "run-a",
            "fair_play_allowed": False, "fair_play_reason": "near_flat_cycle_limit",
            "fair_play_latched": True, "near_flat_cycle_count": 2,
            "fair_play_trigger_threshold": "2", "open_orders_count": 0,
        })
        if after:
            rows.append({
                "timestamp": (start + timedelta(seconds=3)).isoformat(), "symbol": "SOMI:USDso",
                "record_type": "strategy_intent", "sequence_number": 4, "run_fingerprint": "run-a",
                "trade_intent_events": [{"fair_play_allowed": True}], "open_orders_count": 0,
            })
    rows.append({
        "timestamp": (start + timedelta(seconds=4)).isoformat(), "symbol": "SOMI:USDso",
        "record_type": "run_summary", "sequence_number": 5, "run_fingerprint": "run-a",
        "base_position": "0", "open_orders_count": final_open,
    })
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    return path


def test_reason_taxonomy_maps_current_guard_codes():
    assert normalize_fair_play_reason("near_flat_cycle_limit") is PaperBurnInFairPlayReason.REPEATED_NEAR_FLAT_CYCLE
    assert normalize_fair_play_reason("opposite_side_cooldown") is PaperBurnInFairPlayReason.OPPOSITE_SIDE_COOLDOWN


def test_known_guard_codes_are_typed_and_unknown_codes_are_hashed():
    assert normalize_fair_play_reason("short_window_round_trip") is PaperBurnInFairPlayReason.RAPID_ROUND_TRIP
    assert normalize_fair_play_reason("near_flat_cycle_limit") is PaperBurnInFairPlayReason.REPEATED_NEAR_FLAT_CYCLE
    assert normalize_fair_play_reason("unsupported_side") is PaperBurnInFairPlayReason.UNSUPPORTED_SIDE
    assert normalize_fair_play_reason("ok") is PaperBurnInFairPlayReason.OK
    from bot.analytics.paper_burn_in_fair_play_incident_analyzer import safe_reason_code
    assert "made-up-code" not in safe_reason_code("made-up-code")


def test_no_halt_is_a_valid_no_incident_result(tmp_path):
    path = tmp_path / "no-halt.jsonl"
    path.write_text(
        json.dumps({
            "record_type": "run_start", "sequence_number": 1,
            "run_fingerprint": "unique-run-fingerprint", "symbol": "SOMI:USDso",
            "timestamp": "2026-01-01T00:00:00+00:00",
        }) + "\n" + json.dumps({
            "record_type": "run_summary", "sequence_number": 2,
            "run_fingerprint": "unique-run-fingerprint", "symbol": "SOMI:USDso",
            "timestamp": "2026-01-01T00:00:01+00:00", "open_orders_count": 0,
        }) + "\n",
        encoding="utf-8",
    )
    result = analyze_paper_burn_in_fair_play_incident(path, repository_root=tmp_path)
    assert result.halt_sequence is None
    assert result.result == "NO_INCIDENT"
    assert result.evidence_sufficiency == "SUFFICIENT"


def test_rejection_and_halt_evidence_are_separate(tmp_path):
    path = _write(tmp_path / "separated.jsonl")
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    rows[1].update({
        "rejection_reason_code": "opposite_side_cooldown",
        "rejection_reason_normalized": "opposite_side_cooldown",
    })
    rows[2].update({
        "halt_trigger_code": "near_flat_cycle_limit",
        "halt_trigger_normalized": "repeated_near_flat_cycle",
        "halt_observed_value": "2",
        "halt_threshold": "2",
        "open_orders_before_halt": 1,
        "paper_orders_cancelled_by_halt": 1,
        "halt_rejection_streak": 0,
    })
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    result = analyze_paper_burn_in_fair_play_incident(path, repository_root=tmp_path)
    assert result.dominant_reason == "opposite_side_cooldown"
    assert result.halt_trigger == "repeated_near_flat_cycle"
    assert result.halt_threshold == result.observed_trigger_value == 2


def test_exact_halt_trigger_and_enforcement(tmp_path):
    result = analyze_paper_burn_in_fair_play_incident(_write(tmp_path / "run.jsonl"), repository_root=tmp_path)
    assert result.halt_sequence == 3
    assert result.halt_trigger == "repeated_near_flat_cycle"
    assert result.halt_threshold == result.observed_trigger_value == 2
    assert result.paper_orders_cancelled_by_halt == 1
    assert result.fair_play_enforcement == "PASS"
    assert result.strategy_fair_play_compatibility == "FAIL"
    assert result.result == "FAIL"


def test_unknown_reason_warns_without_echoing_payload(tmp_path):
    result = analyze_paper_burn_in_fair_play_incident(
        _write(tmp_path / "run.jsonl", reason="made-up-safe-code"), repository_root=tmp_path
    )
    assert result.dominant_reason == "unknown_explicit_reason"
    assert "unknown_fair_play_reason_code" in result.warnings
    assert "made-up-safe-code" not in " ".join(result.warnings)


def test_missing_reason_is_insufficient(tmp_path):
    result = analyze_paper_burn_in_fair_play_incident(
        _write(tmp_path / "run.jsonl", missing=True), repository_root=tmp_path
    )
    assert result.result == "INSUFFICIENT_RECORDED_EVIDENCE"
    assert "fair_play_reason_code" in result.missing_fields


def test_rejected_order_and_post_halt_intent_fail_enforcement(tmp_path):
    result = analyze_paper_burn_in_fair_play_incident(
        _write(tmp_path / "run.jsonl", bad_order=True, after=True), repository_root=tmp_path
    )
    assert result.fair_play_enforcement == "FAIL"
    assert "rejected_intent_created_order" in result.blockers
    assert "normal_intent_after_halt" in result.blockers


def test_unresolved_open_orders_fail_shutdown_audit(tmp_path):
    result = analyze_paper_burn_in_fair_play_incident(
        _write(tmp_path / "run.jsonl", final_open=1), repository_root=tmp_path
    )
    assert result.fair_play_enforcement == "FAIL"
    assert "open_orders_after_shutdown" in result.blockers


def test_path_is_repository_relative_and_never_network(tmp_path):
    path = _write(tmp_path / "run.jsonl")
    result = analyze_paper_burn_in_fair_play_incident(path.name, repository_root=tmp_path)
    assert result.network_access_used is False
    with pytest.raises(ValueError):
        analyze_paper_burn_in_fair_play_incident("https://example.invalid/run.jsonl", repository_root=tmp_path)


def test_no_incident_cli_fields_are_separate_and_fingerprints_are_masked(tmp_path, capsys, monkeypatch):
    from scripts import analyze_paper_burn_in_fair_play_incident as cli

    path = tmp_path / "no-incident.jsonl"
    path.write_text(
        json.dumps({
            "record_type": "run_start", "sequence_number": 1,
            "run_fingerprint": "unique-run-fingerprint", "configuration_fingerprint": "unique-config-fingerprint",
            "symbol": "SOMI:USDso", "timestamp": "2026-01-01T00:00:00+00:00",
        }) + "\n" + json.dumps({
            "record_type": "run_summary", "sequence_number": 2,
            "run_fingerprint": "unique-run-fingerprint", "configuration_fingerprint": "unique-config-fingerprint",
            "symbol": "SOMI:USDso", "timestamp": "2026-01-01T00:00:01+00:00", "open_orders_count": 0,
        }) + "\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    assert cli.main(["--input", path.name]) == 0
    output = capsys.readouterr().out
    lines = output.splitlines()
    assert "  symbol: SOMI:USDso" in lines
    assert "  first rejection sequence: None" in lines
    assert "  last rejection sequence: None" in lines
    assert "  halt sequence: None" in lines
    assert "  rejection count: 0" in lines
    assert "  halt trigger: none" in lines
    assert "  evidence sufficiency: SUFFICIENT" in lines
    assert "  result: NO_INCIDENT" in lines
    assert "  run fingerprint: run-a" not in lines
    assert "  configuration fingerprint: cfg-a" not in lines
    assert any(line.startswith("  run fingerprint: ") for line in lines)
    assert any(line.startswith("  configuration fingerprint: ") for line in lines)
    assert "SOMI:USDsorun-a" not in output
    assert "SOMI:USDso7d7" not in output
    assert "http" not in output.lower()
    assert "token" not in output.lower()


def test_symbol_is_authoritative_exact_and_mismatch_fails(tmp_path):
    path = _write(tmp_path / "mismatch.jsonl", halt=False)
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    rows[1]["symbol"] = "SOMI:OTHER"
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    result = analyze_paper_burn_in_fair_play_incident(path, repository_root=tmp_path)
    assert result.symbol is None
    assert result.integrity == "FAIL"
    assert "symbol_mismatch" in result.blockers


def test_missing_symbol_record_fails_closed(tmp_path):
    path = _write(tmp_path / "missing-symbol.jsonl", halt=False)
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    rows[1].pop("symbol")
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    result = analyze_paper_burn_in_fair_play_incident(path, repository_root=tmp_path)
    assert result.symbol == "SOMI:USDso"
    assert result.integrity == "FAIL"
    assert "symbol_missing" in result.blockers


def test_long_symbol_is_rejected_by_incident_schema(tmp_path):
    path = _write(tmp_path / "long-symbol.jsonl", halt=False)
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    long_symbol = "A" * 65 + ":USDso"
    for row in rows:
        row["symbol"] = long_symbol
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    result = analyze_paper_burn_in_fair_play_incident(path, repository_root=tmp_path)
    assert result.symbol is None
    assert result.integrity == "FAIL"
    assert "symbol_schema_invalid" in result.blockers


def test_current_verification_canary_has_exact_symbol_and_safe_fields():
    path = Path("data/paper_runs/paper_run_burn_in_20260718_211804_1799622763d9.jsonl")
    result = analyze_paper_burn_in_fair_play_incident(path)
    assert result.symbol == "SOMI:USDso"
    assert result.result == "NO_INCIDENT"
    assert result.exit_code == 0
