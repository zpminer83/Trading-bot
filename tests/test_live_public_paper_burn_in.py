from datetime import datetime, timezone
from decimal import Decimal
import json

import pytest

from scripts import run_live_public_paper_burn_in as burn


def _payload(*, timestamp=None, bid="1", ask="1.01", nonce="1"):
    return {
        "symbol": "SOMI:USDso",
        "timestamp": timestamp or int(datetime.now(timezone.utc).timestamp()),
        "nonce": nonce,
        "bids": [{"price": bid, "quantity": "10"}],
        "asks": [{"price": ask, "quantity": "10"}],
    }


def _config(tmp_path, **kwargs):
    values = dict(
        duration_minutes=Decimal("0.01"),
        sample_interval_seconds=Decimal("0.001"),
        max_snapshots=2,
        output_dir=tmp_path,
    )
    values.update(kwargs)
    return burn.BurnInConfiguration(**values)


def test_burn_in_configuration_is_bounded_and_has_safe_defaults(tmp_path):
    config = burn.BurnInConfiguration(output_dir=tmp_path)
    assert config.symbol == "SOMI:USDso"
    assert config.initial_equity == Decimal("150")
    with pytest.raises(ValueError):
        _config(tmp_path, sample_interval_seconds=Decimal("0"))
    with pytest.raises(ValueError):
        _config(tmp_path, max_snapshots=0)
    with pytest.raises(ValueError):
        _config(tmp_path, max_snapshots=10001)


def test_payload_validation_rejects_crossed_negative_and_large_jump():
    assert burn.validate_public_orderbook_payload(_payload(bid="2", ask="1"), symbol="SOMI:USDso")[0] == "crossed_orderbook"
    assert burn.validate_public_orderbook_payload(_payload(bid="-1"), symbol="SOMI:USDso")[0] == "non_positive_depth"
    assert burn.validate_public_orderbook_payload(_payload(bid="2", ask="2.01"), symbol="SOMI:USDso", previous_mid=Decimal("1"))[0] == "extreme_price_jump"


def test_burn_in_uses_only_public_fixture_and_persists_safe_event_types(tmp_path):
    payload = _payload()
    result = burn.run_burn_in(
        _config(tmp_path),
        fetcher=lambda _url: payload,
        sleep_fn=lambda _seconds: None,
        clock=lambda: datetime.now(timezone.utc),
        monotonic=lambda: 0.0,
        output_path=tmp_path / "burn.jsonl",
    )
    assert result.result == "PASS"
    assert result.paper_only is True
    assert result.public_market_data_only is True
    assert result.authenticated_account_data_used is False
    assert result.executable_candidate_created is False
    assert result.live_order_created is False
    assert result.mutation_rpc_calls == 0
    assert result.signer_invoked is False
    assert result.submission_invoked is False
    assert result.open_paper_orders == 0
    rows = [json.loads(line) for line in (tmp_path / "burn.jsonl").read_text().splitlines()]
    types = {row["record_type"] for row in rows}
    assert {"run_start", "market_snapshot", "portfolio_snapshot", "run_summary"}.issubset(types)
    assert rows[0]["run_id_version"] == "uuid4-sha256-v1"
    assert len({row["run_fingerprint"] for row in rows}) == 1
    assert len({row["configuration_fingerprint"] for row in rows}) == 1
    text = (tmp_path / "burn.jsonl").read_text()
    assert "https://" not in text.lower()
    assert "token" not in text.lower()


def test_burn_in_rejects_bad_market_and_stops_after_consecutive_failures(tmp_path):
    result = burn.run_burn_in(
        _config(tmp_path, max_snapshots=5),
        fetcher=lambda _url: {"symbol": "SOMI:USDso", "bids": [], "asks": []},
        sleep_fn=lambda _seconds: None,
        clock=lambda: datetime.now(timezone.utc),
        monotonic=lambda: 0.0,
        output_path=tmp_path / "reject.jsonl",
    )
    assert result.snapshots_rejected == burn.MAX_CONSECUTIVE_FAILURES
    assert "consecutive_market_failures" in result.blockers
    assert result.open_paper_orders == 0


def test_burn_in_can_classify_stale_snapshot_without_using_previous_data(tmp_path):
    old = int(datetime.now(timezone.utc).timestamp()) - 120
    result = burn.run_burn_in(
        _config(tmp_path, max_snapshots=1),
        fetcher=lambda _url: _payload(timestamp=old),
        sleep_fn=lambda _seconds: None,
        clock=lambda: datetime.now(timezone.utc),
        monotonic=lambda: 0.0,
        output_path=tmp_path / "stale.jsonl",
    )
    assert result.snapshots_accepted == 0
    assert result.stale_snapshots == 1
    assert result.open_paper_orders == 0


def test_sampling_attempts_have_one_terminal_market_record_and_count_rejects(tmp_path):
    good = _payload(nonce="1")
    bad = {"symbol": "SOMI:USDso", "bids": [], "asks": []}
    calls = iter((good, bad, good))
    result = burn.run_burn_in(
        _config(tmp_path, max_snapshots=3),
        fetcher=lambda _url: next(calls),
        sleep_fn=lambda _seconds: None,
        clock=lambda: datetime.now(timezone.utc),
        monotonic=lambda: 0.0,
        output_path=tmp_path / "attempts.jsonl",
    )
    rows = [json.loads(line) for line in (tmp_path / "attempts.jsonl").read_text().splitlines()]
    terminal = [row for row in rows if row["record_type"] in {"market_snapshot", "market_reject"}]
    assert result.sampling_attempts == 3
    assert result.snapshots_accepted + result.snapshots_rejected == 3
    assert len(terminal) == 3
    assert [row["terminal_result"] for row in terminal] == ["accepted", "rejected", "accepted"]
    assert all(row["attempt_number"] == index for index, row in enumerate(terminal, 1))
    assert all(row["scheduled_timestamp"] and row["completed_timestamp"] for row in terminal)
    assert result.public_logical_snapshots == result.public_http_requests == 3


def test_unknown_rejection_reason_is_not_silently_classified(tmp_path, monkeypatch):
    monkeypatch.setattr(burn, "validate_public_orderbook_payload", lambda *args, **kwargs: ("unexplained_condition", None))
    result = burn.run_burn_in(
        _config(tmp_path, max_snapshots=1),
        fetcher=lambda _url: _payload(),
        sleep_fn=lambda _seconds: None,
        clock=lambda: datetime.now(timezone.utc),
        monotonic=lambda: 0.0,
        output_path=tmp_path / "unknown.jsonl",
    )
    assert result.other_explicit_rejects == 1
    assert "unclassified_rejection_reason" in result.blockers
