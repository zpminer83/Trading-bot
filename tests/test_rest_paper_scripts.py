import json
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from bot.risk.market_freshness import (
    MarketFreshnessDecision,
    MarketFreshnessGuard,
    MarketFreshnessLimits,
)
from scripts import run_rest_paper_loop, run_rest_paper_once


def make_loop_engine():
    portfolio = SimpleNamespace(
        cash_balance=Decimal("150"),
        base_position=Decimal("0"),
        average_entry_price=Decimal("0"),
        equity=Decimal("150"),
        realized_pnl=Decimal("0"),
        unrealized_pnl=Decimal("0"),
        drawdown=Decimal("0"),
        total_volume=Decimal("0"),
    )

    return SimpleNamespace(
        portfolio=portfolio,
        competition=None,
        broker=SimpleNamespace(open_orders=[]),
    )


def disable_iteration_prints(monkeypatch):
    for name in (
        "print_market_snapshot",
        "print_market_freshness",
        "print_market_safety",
        "print_fills",
        "print_decisions",
        "print_open_orders",
        "print_portfolio",
        "print_competition",
    ):
        monkeypatch.setattr(run_rest_paper_loop, name, lambda *args: None)


@pytest.mark.parametrize("value", ["1", "TRUE", "Yes", "on"])
def test_paper_run_fsync_accepts_true_values(value, monkeypatch):
    monkeypatch.setenv("PAPER_RUN_FSYNC", value)

    assert run_rest_paper_loop.env_bool("PAPER_RUN_FSYNC") is True


@pytest.mark.parametrize(
    "build_engine",
    [
        run_rest_paper_once.build_engine,
        run_rest_paper_loop.build_engine,
    ],
)
def test_rest_paper_engine_uses_configured_market_freshness(build_engine):
    limits = MarketFreshnessLimits(
        max_exchange_age_seconds=Decimal("11"),
        max_unchanged_seconds=Decimal("12"),
        max_future_skew_seconds=Decimal("13"),
    )

    engine = build_engine(
        symbol="SOMI:USDso",
        market_cache=run_rest_paper_loop.MarketCache(),
        initial_cash=Decimal("150"),
        order_size_usd=Decimal("5"),
        max_open_orders=2,
        pair_boost=Decimal("1"),
        max_spread_percent=Decimal("0.02"),
        min_best_bid_quantity=Decimal("1"),
        min_best_ask_quantity=Decimal("1"),
        market_freshness_limits=limits,
    )

    assert isinstance(engine.market_freshness, MarketFreshnessGuard)
    assert engine.market_freshness.limits == limits


def test_loop_build_record_captures_market_freshness():
    freshness = MarketFreshnessDecision(
        fresh=False,
        reason="repeated_snapshot",
        exchange_age_seconds=Decimal("4.25"),
        unchanged_seconds=Decimal("31.5"),
    )
    result = SimpleNamespace(
        market_safety_decision=None,
        market_freshness_decision=freshness,
        intents=[],
        decisions=[],
        fills=[],
        submitted_orders=[],
    )
    snapshot = SimpleNamespace(
        best_bid=None,
        best_ask=None,
        mid_price=None,
        spread=None,
    )
    portfolio = SimpleNamespace(
        cash_balance=Decimal("150"),
        base_position=Decimal("0"),
        equity=Decimal("150"),
        realized_pnl=Decimal("0"),
        unrealized_pnl=Decimal("0"),
        drawdown=Decimal("0"),
        total_volume=Decimal("0"),
    )
    engine = SimpleNamespace(
        portfolio=portfolio,
        competition=None,
        broker=SimpleNamespace(open_orders=[]),
    )

    record = run_rest_paper_loop.build_record(
        timestamp=datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc),
        symbol="SOMI:USDso",
        snapshot=snapshot,
        result=result,
        engine=engine,
        iteration_index=4,
    )

    assert record.iteration_index == 4
    assert record.iteration_ok is True
    assert record.error_type is None
    assert record.error_message is None
    assert record.market_fresh is False
    assert record.market_freshness_reason == "repeated_snapshot"
    assert record.exchange_age_seconds == Decimal("4.25")
    assert record.unchanged_seconds == Decimal("31.5")


def test_successful_loop_iteration_is_persisted_immediately(
    tmp_path,
    monkeypatch,
):
    snapshot = SimpleNamespace(
        best_bid=None,
        best_ask=None,
        mid_price=None,
        spread=None,
    )
    result = SimpleNamespace(
        market_safety_decision=None,
        market_freshness_decision=None,
        intents=[],
        decisions=[],
        fills=[],
        submitted_orders=[],
    )
    market_data = SimpleNamespace(
        handle_orderbook_payload=lambda **kwargs: snapshot,
    )
    engine = make_loop_engine()
    engine.step = lambda timestamp: result
    recorder = run_rest_paper_loop.PaperRunRecorder()
    output_path = tmp_path / "paper_run.jsonl"

    monkeypatch.setattr(run_rest_paper_loop, "fetch_json", lambda url: {})
    monkeypatch.setattr(
        run_rest_paper_loop,
        "extract_orderbook_payload",
        lambda response, symbol: {},
    )
    monkeypatch.setattr(
        recorder,
        "write_jsonl",
        lambda path: pytest.fail("loop must not rewrite the JSONL file"),
    )
    disable_iteration_prints(monkeypatch)

    run_rest_paper_loop.run_iteration(
        index=3,
        url="https://example.invalid/orderbook",
        symbol="SOMI:USDso",
        market_data=market_data,
        engine=engine,
        recorder=recorder,
        output_path=output_path,
    )

    records = [
        json.loads(line)
        for line in output_path.read_text(encoding="utf-8").splitlines()
    ]

    assert len(records) == 1
    assert recorder.count == 1
    assert recorder.latest is not None
    assert records[0]["iteration_index"] == 3
    assert records[0]["iteration_ok"] is True
    assert records[0]["error_type"] is None
    assert records[0]["error_message"] is None


def test_failed_loop_iterations_are_recorded_and_persisted(
    tmp_path,
    monkeypatch,
):
    output_path = tmp_path / "paper_run.jsonl"
    engine = make_loop_engine()
    persisted_counts = []
    sync_values = []
    original_append_jsonl = run_rest_paper_loop.PaperRunRecorder.append_jsonl

    def fail_iteration(**kwargs):
        raise RuntimeError("request failed")

    def track_append(recorder, path, record, *, sync_to_disk=False):
        result = original_append_jsonl(
            recorder,
            path,
            record,
            sync_to_disk=sync_to_disk,
        )
        persisted_counts.append(recorder.count)
        sync_values.append(sync_to_disk)
        return result

    monkeypatch.setenv("PAPER_RUN_OUTPUT", str(output_path))
    monkeypatch.setenv("PAPER_LOOP_ITERATIONS", "2")
    monkeypatch.setenv("PAPER_LOOP_INTERVAL_SECONDS", "0")
    monkeypatch.setenv("PAPER_RUN_FSYNC", "YeS")
    monkeypatch.setattr(run_rest_paper_loop, "build_engine", lambda **kwargs: engine)
    monkeypatch.setattr(run_rest_paper_loop, "run_iteration", fail_iteration)
    monkeypatch.setattr(run_rest_paper_loop, "print_header", lambda **kwargs: None)
    monkeypatch.setattr(
        run_rest_paper_loop,
        "print_final_summary",
        lambda **kwargs: None,
    )
    monkeypatch.setattr(
        run_rest_paper_loop.PaperRunRecorder,
        "append_jsonl",
        track_append,
    )
    monkeypatch.setattr(
        run_rest_paper_loop.PaperRunRecorder,
        "write_jsonl",
        lambda *args, **kwargs: pytest.fail(
            "loop must not rewrite the JSONL file"
        ),
    )

    run_rest_paper_loop.main()

    records = [
        json.loads(line)
        for line in output_path.read_text(encoding="utf-8").splitlines()
    ]

    assert persisted_counts == [1, 2]
    assert sync_values == [True, True]
    assert [record["iteration_index"] for record in records] == [1, 2]
    assert all(record["iteration_ok"] is False for record in records)
    assert all(record["error_type"] == "RuntimeError" for record in records)
    assert all(record["error_message"] == "request failed" for record in records)
    assert all(record["cash_balance"] == "150" for record in records)
    assert all(record["equity"] == "150" for record in records)


def test_keyboard_interrupt_is_not_recorded_as_failure(tmp_path, monkeypatch):
    output_path = tmp_path / "paper_run.jsonl"
    engine = make_loop_engine()

    def interrupt_iteration(**kwargs):
        raise KeyboardInterrupt

    monkeypatch.setenv("PAPER_RUN_OUTPUT", str(output_path))
    monkeypatch.setenv("PAPER_LOOP_ITERATIONS", "1")
    monkeypatch.setattr(run_rest_paper_loop, "build_engine", lambda **kwargs: engine)
    monkeypatch.setattr(run_rest_paper_loop, "run_iteration", interrupt_iteration)
    monkeypatch.setattr(run_rest_paper_loop, "print_header", lambda **kwargs: None)
    monkeypatch.setattr(
        run_rest_paper_loop,
        "print_final_summary",
        lambda **kwargs: None,
    )

    run_rest_paper_loop.main()

    assert output_path.exists() is False


def test_failed_record_redacts_sensitive_values_and_truncates_message():
    engine = make_loop_engine()
    exc = RuntimeError(
        "token=super-secret "
        "-----BEGIN PRIVATE KEY----- private-secret "
        "-----END PRIVATE KEY----- "
        "headers={'Authorization': 'Bearer header-secret'} "
        + ("x" * 600)
    )

    record = run_rest_paper_loop.build_failed_record(
        timestamp=datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc),
        symbol="SOMI:USDso",
        iteration_index=1,
        exc=exc,
        engine=engine,
    )

    assert record.error_message is not None
    assert "super-secret" not in record.error_message
    assert "header-secret" not in record.error_message
    assert "private-secret" not in record.error_message
    assert "token=[REDACTED]" in record.error_message
    assert "headers=[REDACTED]" in record.error_message
    assert "[REDACTED PRIVATE KEY]" in record.error_message
    assert len(record.error_message) == 500
