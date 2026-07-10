import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from bot.execution.conservative_paper_broker import PaperOrder
from bot.execution.order import OrderIntent
from bot.execution.passive_fill_evidence import PassiveFillEvidenceTracker
from bot.market.models import OrderBook, OrderBookLevel
from bot.risk.market_freshness import (
    MarketFreshnessDecision,
    MarketFreshnessGuard,
    MarketFreshnessLimits,
)
from bot.risk.portfolio_risk_guard import (
    PortfolioRiskDecision,
    PortfolioRiskLimits,
)
from scripts import run_rest_paper_loop, run_rest_paper_once


def make_loop_engine():
    cancellations = []
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
        order_manager=SimpleNamespace(
            cancel_all=lambda: cancellations.append(True),
        ),
        cancellations=cancellations,
    )


def disable_iteration_prints(monkeypatch):
    for name in (
        "print_market_snapshot",
        "print_market_freshness",
        "print_market_safety",
        "print_portfolio_risk",
        "print_passive_fill_evidence",
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


def test_calculate_error_backoff_is_exponential_and_capped():
    assert run_rest_paper_loop.calculate_error_backoff(1, 2, 5) == 2.0
    assert run_rest_paper_loop.calculate_error_backoff(2, 2, 5) == 4.0
    assert run_rest_paper_loop.calculate_error_backoff(3, 2, 5) == 5.0
    assert run_rest_paper_loop.calculate_error_backoff(10_000, 2, 5) == 5.0


@pytest.mark.parametrize(
    ("max_failures", "base_seconds", "max_seconds", "message"),
    [
        (0, 2, 30, "PAPER_MAX_CONSECUTIVE_FAILURES"),
        (5, -1, 30, "PAPER_ERROR_BACKOFF_BASE_SECONDS"),
        (5, 3, 2, "PAPER_ERROR_BACKOFF_MAX_SECONDS"),
    ],
)
def test_failure_configuration_validation(
    max_failures,
    base_seconds,
    max_seconds,
    message,
):
    with pytest.raises(ValueError, match=message):
        run_rest_paper_loop.validate_failure_configuration(
            max_consecutive_failures=max_failures,
            base_seconds=base_seconds,
            max_seconds=max_seconds,
        )


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
    risk_limits = PortfolioRiskLimits(max_drawdown=Decimal("0.20"))

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
        portfolio_risk_limits=risk_limits,
    )

    assert isinstance(engine.market_freshness, MarketFreshnessGuard)
    assert engine.market_freshness.limits == limits
    assert engine.portfolio_risk_guard.limits == risk_limits


@pytest.mark.parametrize(
    "script",
    [run_rest_paper_once, run_rest_paper_loop],
)
def test_rest_paper_rejects_invalid_drawdown_environment(
    script,
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("PAPER_MAX_DRAWDOWN_RATIO", "1.01")
    monkeypatch.setenv("PAPER_RUN_OUTPUT", str(tmp_path / "paper_run.jsonl"))
    monkeypatch.setattr(
        script,
        "fetch_json",
        lambda url: pytest.fail("invalid config must fail before network fetch"),
    )

    with pytest.raises(ValueError, match="between 0 and 1"):
        script.main()


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
        portfolio_risk_decision=PortfolioRiskDecision(
            allowed=False,
            reason="max_drawdown_reached",
            latched=True,
            drawdown=Decimal("0.10"),
            max_drawdown=Decimal("0.10"),
            equity=Decimal("135"),
            peak_equity=Decimal("150"),
        ),
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
    assert record.portfolio_risk_allowed is False
    assert record.portfolio_risk_reason == "max_drawdown_reached"
    assert record.portfolio_risk_latched is True
    assert record.risk_drawdown == Decimal("0.10")
    assert record.risk_max_drawdown == Decimal("0.10")


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
        portfolio_risk_decision=PortfolioRiskDecision(
            allowed=True,
            reason="ok",
            latched=False,
            drawdown=Decimal("0"),
            max_drawdown=Decimal("0.10"),
            equity=Decimal("150"),
            peak_equity=Decimal("150"),
        ),
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
    assert records[0]["consecutive_failures"] == 0
    assert records[0]["portfolio_risk_allowed"] is True
    assert records[0]["portfolio_risk_reason"] == "ok"
    assert records[0]["portfolio_risk_latched"] is False
    assert records[0]["risk_drawdown"] == "0"
    assert records[0]["risk_max_drawdown"] == "0.10"


def test_rest_loop_records_passive_fill_evidence_without_creating_fill(
    tmp_path,
    monkeypatch,
):
    order = PaperOrder(
        order_id=1,
        intent=OrderIntent(
            symbol="SOMI:USDso",
            side="buy",
            order_type="limit",
            price=Decimal("100"),
            quantity=Decimal("1"),
        ),
    )
    initial_book = OrderBook(
        symbol="SOMI:USDso",
        bids=[OrderBookLevel(price=Decimal("100"), quantity=Decimal("10"))],
        asks=[OrderBookLevel(price=Decimal("101"), quantity=Decimal("10"))],
    )
    current_book = OrderBook(
        symbol="SOMI:USDso",
        bids=[OrderBookLevel(price=Decimal("100"), quantity=Decimal("6"))],
        asks=[OrderBookLevel(price=Decimal("101"), quantity=Decimal("10"))],
    )
    snapshot = SimpleNamespace(
        orderbook=current_book,
        best_bid=current_book.bids[0],
        best_ask=current_book.asks[0],
        mid_price=Decimal("100.5"),
        spread=Decimal("1"),
    )
    result = SimpleNamespace(
        market_safety_decision=None,
        market_freshness_decision=None,
        portfolio_risk_decision=None,
        intents=[],
        decisions=[],
        fills=[],
        submitted_orders=[],
    )
    market_data = SimpleNamespace(
        handle_orderbook_payload=lambda **kwargs: snapshot,
    )
    engine = make_loop_engine()
    engine.broker.open_orders.append(order)
    engine.step = lambda timestamp: result
    tracker = PassiveFillEvidenceTracker()
    tracker.synchronize(
        [order],
        initial_book,
        datetime.now(timezone.utc) - timedelta(seconds=1),
    )
    recorder = run_rest_paper_loop.PaperRunRecorder()
    output_path = tmp_path / "paper_run.jsonl"
    starting_portfolio = (
        engine.portfolio.cash_balance,
        engine.portfolio.base_position,
        engine.portfolio.total_volume,
    )

    monkeypatch.setattr(run_rest_paper_loop, "fetch_json", lambda url: {})
    monkeypatch.setattr(
        run_rest_paper_loop,
        "extract_orderbook_payload",
        lambda response, symbol: {},
    )
    disable_iteration_prints(monkeypatch)

    run_rest_paper_loop.run_iteration(
        index=2,
        url="https://example.invalid/orderbook",
        symbol="SOMI:USDso",
        market_data=market_data,
        engine=engine,
        recorder=recorder,
        output_path=output_path,
        fill_evidence_tracker=tracker,
    )

    record = json.loads(output_path.read_text(encoding="utf-8").splitlines()[0])

    assert record["evaluated_open_orders_count"] == 1
    assert record["orders_at_touch_count"] == 1
    assert record["crossed_order_count"] == 0
    assert record["level_quantity_decreased_count"] == 1
    assert record["level_disappeared_count"] == 0
    assert Decimal(record["max_open_order_age_seconds"]) >= 0
    assert result.fills == []
    assert order.status == "open"
    assert (
        engine.portfolio.cash_balance,
        engine.portfolio.base_position,
        engine.portfolio.total_volume,
    ) == starting_portfolio


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
    monkeypatch.setenv("PAPER_ERROR_BACKOFF_BASE_SECONDS", "0")
    monkeypatch.setenv("PAPER_ERROR_BACKOFF_MAX_SECONDS", "0")
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
    assert [record["consecutive_failures"] for record in records] == [1, 2]
    assert all(record["cash_balance"] == "150" for record in records)
    assert all(record["equity"] == "150" for record in records)


def test_main_fails_fast_for_invalid_failure_configuration(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("PAPER_RUN_OUTPUT", str(tmp_path / "paper_run.jsonl"))
    monkeypatch.setenv("PAPER_MAX_CONSECUTIVE_FAILURES", "0")
    monkeypatch.setenv("PAPER_ERROR_BACKOFF_BASE_SECONDS", "2")
    monkeypatch.setenv("PAPER_ERROR_BACKOFF_MAX_SECONDS", "30")
    monkeypatch.setattr(
        run_rest_paper_loop,
        "build_engine",
        lambda **kwargs: pytest.fail("engine must not be built"),
    )

    with pytest.raises(ValueError, match="PAPER_MAX_CONSECUTIVE_FAILURES"):
        run_rest_paper_loop.main()


def test_success_resets_failure_counter_and_selects_correct_sleep(
    tmp_path,
    monkeypatch,
):
    output_path = tmp_path / "paper_run.jsonl"
    engine = make_loop_engine()
    sleep_calls = []
    failing_iterations = {1, 3}

    def scripted_iteration(
        index,
        symbol,
        recorder,
        output_path,
        sync_to_disk,
        **kwargs,
    ):
        if index in failing_iterations:
            raise RuntimeError(f"failure {index}")

        record = run_rest_paper_loop.PaperRunRecord(
            timestamp=datetime(2026, 7, 13, 12, index, tzinfo=timezone.utc),
            symbol=symbol,
            iteration_index=index,
            iteration_ok=True,
            consecutive_failures=0,
        )
        run_rest_paper_loop.persist_record(
            recorder=recorder,
            record=record,
            output_path=output_path,
            sync_to_disk=sync_to_disk,
        )

    monkeypatch.setenv("PAPER_RUN_OUTPUT", str(output_path))
    monkeypatch.setenv("PAPER_LOOP_ITERATIONS", "4")
    monkeypatch.setenv("PAPER_LOOP_INTERVAL_SECONDS", "7")
    monkeypatch.setenv("PAPER_MAX_CONSECUTIVE_FAILURES", "5")
    monkeypatch.setenv("PAPER_ERROR_BACKOFF_BASE_SECONDS", "2")
    monkeypatch.setenv("PAPER_ERROR_BACKOFF_MAX_SECONDS", "30")
    monkeypatch.setattr(run_rest_paper_loop, "build_engine", lambda **kwargs: engine)
    monkeypatch.setattr(run_rest_paper_loop, "run_iteration", scripted_iteration)
    monkeypatch.setattr(run_rest_paper_loop, "print_header", lambda **kwargs: None)
    monkeypatch.setattr(
        run_rest_paper_loop,
        "print_final_summary",
        lambda **kwargs: None,
    )
    monkeypatch.setattr(
        run_rest_paper_loop.time,
        "sleep",
        lambda seconds: sleep_calls.append(seconds),
    )

    run_rest_paper_loop.main()

    records = [
        json.loads(line)
        for line in output_path.read_text(encoding="utf-8").splitlines()
    ]

    assert [record["iteration_index"] for record in records] == [1, 2, 3, 4]
    assert [record["consecutive_failures"] for record in records] == [1, 0, 1, 0]
    assert sleep_calls == [2.0, 7, 2.0]
    assert engine.cancellations == []


def test_circuit_breaker_caps_backoff_cancels_orders_and_stops(
    tmp_path,
    monkeypatch,
    capsys,
):
    output_path = tmp_path / "paper_run.jsonl"
    engine = make_loop_engine()
    attempted_iterations = []
    sleep_calls = []

    def fail_iteration(index, **kwargs):
        attempted_iterations.append(index)
        raise RuntimeError("request failed")

    monkeypatch.setenv("PAPER_RUN_OUTPUT", str(output_path))
    monkeypatch.setenv("PAPER_LOOP_ITERATIONS", "10")
    monkeypatch.setenv("PAPER_LOOP_INTERVAL_SECONDS", "9")
    monkeypatch.setenv("PAPER_MAX_CONSECUTIVE_FAILURES", "3")
    monkeypatch.setenv("PAPER_ERROR_BACKOFF_BASE_SECONDS", "2")
    monkeypatch.setenv("PAPER_ERROR_BACKOFF_MAX_SECONDS", "3")
    monkeypatch.setattr(run_rest_paper_loop, "build_engine", lambda **kwargs: engine)
    monkeypatch.setattr(run_rest_paper_loop, "run_iteration", fail_iteration)
    monkeypatch.setattr(run_rest_paper_loop, "print_header", lambda **kwargs: None)
    monkeypatch.setattr(
        run_rest_paper_loop,
        "print_final_summary",
        lambda **kwargs: None,
    )
    monkeypatch.setattr(
        run_rest_paper_loop.time,
        "sleep",
        lambda seconds: sleep_calls.append(seconds),
    )

    run_rest_paper_loop.main()

    records = [
        json.loads(line)
        for line in output_path.read_text(encoding="utf-8").splitlines()
    ]
    output = capsys.readouterr().out

    assert attempted_iterations == [1, 2, 3]
    assert len(records) == 3
    assert [record["consecutive_failures"] for record in records] == [1, 2, 3]
    assert sleep_calls == [2.0, 3.0]
    assert engine.cancellations == [True]
    assert "CIRCUIT BREAKER" in output


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
    assert engine.cancellations == [True]


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
        consecutive_failures=4,
    )

    assert record.error_message is not None
    assert "super-secret" not in record.error_message
    assert "header-secret" not in record.error_message
    assert "private-secret" not in record.error_message
    assert "token=[REDACTED]" in record.error_message
    assert "headers=[REDACTED]" in record.error_message
    assert "[REDACTED PRIVATE KEY]" in record.error_message
    assert len(record.error_message) == 500
    assert record.consecutive_failures == 4
