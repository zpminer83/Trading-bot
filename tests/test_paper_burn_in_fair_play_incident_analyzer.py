from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json

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
    assert normalize_fair_play_reason("opposite_side_cooldown") is PaperBurnInFairPlayReason.UNKNOWN_EXPLICIT_REASON


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
