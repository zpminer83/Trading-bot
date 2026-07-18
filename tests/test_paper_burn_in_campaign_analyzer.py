from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest

from bot.analytics.paper_burn_in_campaign_analyzer import analyze_paper_burn_in_campaign
from scripts.run_live_public_paper_burn_in import (
    BurnInConfiguration,
    _configuration_fingerprint,
    _run_fingerprint,
)


def _write_run(root: Path, name: str, *, fingerprint: str | None = None, symbol: str = "SOMI:USDso", duration_steps: int = 301, risk_limit: str = "0.10", privacy: bool = False, start_offset_minutes: int = 0) -> Path:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=start_offset_minutes)
    fingerprint = fingerprint or name
    rows: list[dict] = [{
        "timestamp": start.isoformat(), "symbol": symbol, "record_type": "run_start",
        "sequence_number": 1, "run_fingerprint": fingerprint, "iteration_index": 0,
        "iteration_ok": True, "notes": [],
    }]
    sequence = 2
    for index in range(1, duration_steps + 1):
        timestamp = start + timedelta(seconds=index * 5)
        mid = f"1.{index % 7:04d}"
        common = dict(
            timestamp=timestamp.isoformat(), symbol=symbol, sequence_number=sequence,
            run_fingerprint=fingerprint, iteration_index=index, iteration_ok=True,
            best_bid=mid, best_ask=f"{float(mid) + 0.01:.4f}", mid_price=mid, spread="0.01",
            cash_balance="100", base_position="0", equity="100", peak_equity="100", drawdown="0",
            fees_paid="0", open_orders_count=0, portfolio_risk_allowed=True,
            risk_max_drawdown=risk_limit, projected_shocked_drawdown="0.02",
            preemptive_halt_latched=False, hard_kill_latched=False,
            gap_risk_assumptions_available=True, notes=[],
        )
        rows.append({**common, "record_type": "market_snapshot", "sequence_number": sequence})
        sequence += 1
        rows.append({**common, "record_type": "portfolio_snapshot", "sequence_number": sequence})
        sequence += 1
    notes = [
        "result=PASS", f"audit.market_snapshots={duration_steps}",
        f"audit.accepted_snapshots={duration_steps}", "audit.rejected_snapshots=0",
        "audit.stale_snapshots=0", "audit.crossed_books=0", "audit.malformed_snapshots=0",
        "audit.strategy_intents=0", "audit.risk_approved_intents=0",
        "audit.risk_rejected_intents=0", "audit.fair_play_rejected_intents=0",
        "audit.paper_orders_created=0", "audit.paper_replacements=0",
        "audit.partial_fills=0", "audit.full_fills=0", "audit.open_orders_after_shutdown=0",
    ]
    summary_time = start + timedelta(seconds=duration_steps * 5)
    rows.append({
        "timestamp": summary_time.isoformat(), "symbol": symbol, "record_type": "run_summary",
        "sequence_number": sequence, "run_fingerprint": fingerprint, "iteration_index": duration_steps,
        "iteration_ok": True, "cash_balance": "100", "base_position": "0", "equity": "100",
        "drawdown": "0", "open_orders_count": 0, "fees_paid": "0", "notes": notes,
    })
    if privacy:
        rows[1]["notes"] = ["https://sensitive.example"]
    path = root / name
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    return path


def _three(tmp_path: Path) -> list[Path]:
    return [_write_run(tmp_path, f"run{i}.jsonl", fingerprint=f"fp{i}", start_offset_minutes=i * 30) for i in range(3)]


def test_three_healthy_runs_aggregate_and_require_no_glob(tmp_path):
    paths = _three(tmp_path)
    result = analyze_paper_burn_in_campaign([p.name for p in paths], repository_root=tmp_path)
    assert result.qualifying_run_count == 3
    assert result.market.status == "PASS"
    assert result.market.total_accepted_snapshots == 903
    assert result.execution.strategy_activity == "NO_ACTIVITY"
    assert result.result == "PASS_WITH_WARNINGS" or result.result == "PASS"
    assert result.input_files == ("run0.jsonl", "run1.jsonl", "run2.jsonl")


def test_one_run_is_insufficient_not_failed(tmp_path):
    path = _write_run(tmp_path, "one.jsonl")
    result = analyze_paper_burn_in_campaign([path.name], repository_root=tmp_path)
    assert result.result == "INSUFFICIENT_EVIDENCE"
    assert result.exit_code == 3


def test_duplicate_path_is_rejected(tmp_path):
    path = _write_run(tmp_path, "one.jsonl")
    with pytest.raises(ValueError, match="duplicate_input_path"):
        analyze_paper_burn_in_campaign([path.name, path.name], repository_root=tmp_path)


def test_duplicate_fingerprint_and_symbol_mismatch_fail(tmp_path):
    first = _write_run(tmp_path, "one.jsonl", fingerprint="same")
    second = _write_run(tmp_path, "two.jsonl", fingerprint="same", symbol="OTHER")
    result = analyze_paper_burn_in_campaign([first.name, second.name], repository_root=tmp_path)
    assert result.result == "FAIL"
    assert "duplicate_run_fingerprint" in result.blockers
    assert "symbol_mismatch" in result.blockers


def test_overlapping_windows_fail(tmp_path):
    paths = [_write_run(tmp_path, f"overlap{i}.jsonl", fingerprint=f"overlap{i}") for i in range(3)]
    result = analyze_paper_burn_in_campaign([p.name for p in paths], repository_root=tmp_path)
    assert "overlapping_run_windows" in result.blockers
    assert result.result == "FAIL"


def test_privacy_failure_and_single_run_failure_propagate(tmp_path):
    paths = _three(tmp_path)
    bad = _write_run(tmp_path, "bad.jsonl", fingerprint="bad", privacy=True)
    result = analyze_paper_burn_in_campaign([paths[0].name, paths[1].name, bad.name], repository_root=tmp_path)
    assert result.privacy_status == "FAIL"
    assert result.result == "FAIL"


def test_safety_counters_and_real_submission_fail_campaign(tmp_path):
    paths = _three(tmp_path)
    rows = [json.loads(line) for line in paths[0].read_text(encoding="utf-8").splitlines()]
    rows[-1]["notes"].append("audit.mutation_rpc_calls=1")
    paths[0].write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    result = analyze_paper_burn_in_campaign([p.name for p in paths], repository_root=tmp_path)
    assert result.mutation_rpc_calls == 1
    assert "unsafe_counter_nonzero" in result.blockers
    assert result.result == "FAIL"


def test_path_traversal_and_cli_no_live_arguments(tmp_path):
    _write_run(tmp_path, "one.jsonl")
    with pytest.raises(ValueError):
        analyze_paper_burn_in_campaign(["../one.jsonl"], repository_root=tmp_path)
    from scripts import analyze_paper_burn_in_campaign as cli
    with pytest.raises(SystemExit):
        cli.main(["--input", "one.jsonl", "--rpc-url", "https://example"])


def test_configuration_fingerprint_is_stable_but_run_identity_is_unique(tmp_path):
    config = BurnInConfiguration(output_dir=tmp_path)
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert _configuration_fingerprint(config) == _configuration_fingerprint(config)
    assert _run_fingerprint(config, start) != _run_fingerprint(config, start)
