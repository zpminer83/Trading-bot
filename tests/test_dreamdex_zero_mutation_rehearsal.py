from decimal import Decimal
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from bot.execution.dreamdex_zero_mutation_rehearsal import (
    DreamDexLiveReadOnlyEvidenceStatus,
    DreamDexLiveReadOnlyEndpointConfigurationStatus,
    DreamDexLiveReadOnlyConfigurationStatus,
    DreamDexZeroMutationRehearsalEvidence,
    DreamDexLiveReadOnlyRehearsalDependencies,
    DreamDexZeroMutationRehearsalPolicy,
    READ_ONLY_REHEARSAL_RPC_ALLOWLIST,
    READ_ONLY_REHEARSAL_FORBIDDEN_PREFIXES,
    build_rehearsal_candidate,
    collect_live_read_only_rehearsal_evidence_from_dependencies,
    collect_live_read_only_rehearsal_evidence,
    run_zero_mutation_rehearsal,
)


def test_live_evidence_status_is_typed_and_safe():
    item = DreamDexLiveReadOnlyEvidenceStatus(
        evidence_name="account_identity", request_performed=True,
        source_category="authenticated_account", transport_status="confirmed",
        authentication_status="authenticated_success", schema_status="confirmed",
        authority_status="authoritative", result_status="confirmed",
        response_shape_fingerprint="0x" + "a" * 64,
        validation_errors=("https://secret.example/token",),
    )
    safe = item.safe_dict()
    assert safe["result_status"] == "confirmed"
    assert "https://" not in repr(item) and "secret" not in str(safe)
    assert safe["response_shape_fingerprint"] != "0x" + "a" * 64


def test_safe_configuration_status_has_no_endpoint_or_secret_values():
    status = DreamDexLiveReadOnlyConfigurationStatus(
        public_api_configured=True, rpc_configured=True,
        authenticated_session_configured=False, required_chain_id=5031,
        market_symbol="SOMI:USDso", public_transport_ready=True,
        rpc_transport_ready=True, blockers=("authenticated_session_not_configured",),
        configuration_fingerprint="0x" + "b" * 64,
    )
    text = repr(status) + str(status.safe_dict())
    assert "http" not in text.lower()
    assert "token" not in text.lower()
    assert "0x" + "b" * 64 not in text
    assert status.safe_dict()["blockers"] == ["authenticated_session_not_configured"]


def test_endpoint_configuration_status_is_value_free_and_source_allowlisted():
    from scripts.run_dreamdex_zero_mutation_rehearsal import _endpoint_status

    status = _endpoint_status("https://api.dreamdex.io/v0", endpoint_type="public_api", source_name="pinned_production")
    assert status.ready is True
    assert "api.dreamdex.io" not in repr(status)
    assert status.safe_dict()["redirects_allowed"] is False
    with pytest.raises(ValueError):
        DreamDexLiveReadOnlyEndpointConfigurationStatus(endpoint_type="public_api", source_name="arbitrary_env_name")


def test_endpoint_validation_rejects_userinfo_fragment_and_credential_query_without_echoing_values():
    from scripts.run_dreamdex_zero_mutation_rehearsal import _endpoint_status

    for value in ("https://user:pass@rpc.example", "https://rpc.example/#fragment", "https://rpc.example/path?token=secret"):
        status = _endpoint_status(value, endpoint_type="rpc", source_name="dedicated_rpc_read_only")
        text = repr(status) + str(status.safe_dict())
        assert status.ready is False
        assert "user:pass" not in text and "token=secret" not in text


def test_live_causal_gas_stage_is_not_attempted_without_candidate():
    evidence = _evidence(
        evidence_statuses=(DreamDexLiveReadOnlyEvidenceStatus(
            evidence_name="gas_estimate", result_status="not_attempted_due_to_prerequisite",
            prerequisite="formed_unsigned_candidate", blocker="gas_estimate_prerequisite_unavailable"),),
        gas_estimate_status="not_attempted_due_to_prerequisite",
        native_gas_balance_evidence="confirmed", authenticated_trading_balance_evidence="confirmed",
        available_order_currency_balance="confirmed", available_base_asset_balance="confirmed",
    )
    result = run_zero_mutation_rehearsal(policy=_policy(), evidence=evidence, candidate=None)
    assert "gas_estimate_prerequisite_unavailable" in result.derived_blockers
    assert "gas_estimate_unavailable" not in result.primary_blockers
    assert "approval_preview_not_constructed" in result.not_attempted_stages


def test_live_balance_evidence_is_separate_from_native_gas():
    evidence = _evidence(
        evidence_statuses=(DreamDexLiveReadOnlyEvidenceStatus(evidence_name="trading_balances", result_status="confirmed"),),
        native_gas_balance_evidence="confirmed", authenticated_trading_balance_evidence="confirmed_unavailable_from_source",
        available_order_currency_balance="confirmed_unavailable_from_source", available_base_asset_balance="confirmed_unavailable_from_source",
    )
    result = run_zero_mutation_rehearsal(policy=_policy(), evidence=evidence, candidate=None)
    assert "native_gas_balance_unavailable" not in result.primary_blockers
    assert "authenticated_trading_balance_unavailable" in result.primary_blockers


def _policy(**kwargs):
    return DreamDexZeroMutationRehearsalPolicy(required_market_symbol="SOMI:USDso", **kwargs)


def _evidence(**kwargs):
    values = dict(market_status="available", orderbook_status="available", account_status="available", rpc_status="available", chain_id=5031,
                  target_code_status="available", pending_nonce_status="available", native_balance_status="available",
                  gas_estimate_status="available", fee_status="available", market_rules_status="available",
                  runtime_gate_status="available", risk_status="available", fair_play_status="available",
                  market_age_ms=1, account_age_ms=1, source_authority="authoritative", network_read_call_count=7,
                  market_identity_status="confirmed", account_identity_status="confirmed", trading_enabled=True,
                  contract_code_present=True, pending_nonce=7, gas_estimate=21000, estimated_fee_wei=1000, native_balance_wei=1000000,
                  gap_risk_status="available", gap_risk_budget_approved=True,
                  account_authority_status="confirmed", open_order_status="available_empty", fills_status="available_empty")
    values.update(drawdown_fraction=Decimal("0"), preemptive_drawdown=Decimal("0.08"), hard_drawdown_limit=Decimal("0.10"), projected_shocked_drawdown=Decimal("0.02"))
    values.update(kwargs)
    return DreamDexZeroMutationRehearsalEvidence(**values)


def _candidate(policy):
    return build_rehearsal_candidate(market_symbol="SOMI:USDso", side="BUY", price="1.0000", quantity="1.00",
                                     market_rules={"tick_size": "0.0001", "quantity_step": "0.01", "minimum_quantity": "1", "minimum_notional": "1"},
                                     best_ask="1.0010", policy=policy)


def test_default_policy_is_fail_closed_and_side_effect_flags_false():
    p = _policy()
    assert p.allow_submission is False and p.allow_signing is False and p.allow_approval_prompt is False
    assert p.authoritative is False


def test_policy_rejects_enabling_side_effects():
    with pytest.raises(ValueError):
        _policy(allow_submission=True)


def test_complete_fixture_is_ready_only_for_human_review():
    p = _policy()
    result = run_zero_mutation_rehearsal(policy=p, evidence=_evidence(), candidate=_candidate(p))
    assert result.ready_for_human_review is True
    assert result.ready_for_signing is False and result.ready_for_submission is False
    assert result.mutation_call_count == 0 and result.signer_invocation_count == 0 and result.submission_attempt_count == 0
    assert result.production_journal_write_performed is False


@pytest.mark.parametrize("field", ["market_status", "account_status", "rpc_status", "target_code_status", "pending_nonce_status", "fee_status", "risk_status", "fair_play_status"])
def test_missing_evidence_blocks(field):
    p = _policy()
    result = run_zero_mutation_rehearsal(policy=p, evidence=_evidence(**{field: "unavailable"}), candidate=_candidate(p))
    assert result.readiness_status == "blocked"
    assert result.blockers


def test_stale_evidence_and_wrong_chain_block():
    p = _policy(maximum_market_age_ms=5)
    result = run_zero_mutation_rehearsal(policy=p, evidence=_evidence(market_age_ms=6, chain_id=1), candidate=_candidate(p))
    assert "rpc_chain_mismatch" in result.blockers
    assert "market_evidence_unavailable_or_stale" in result.blockers


def test_candidate_requires_known_rules_and_non_crossing_price():
    p = _policy()
    assert _candidate(p) is not None
    assert build_rehearsal_candidate(market_symbol="SOMI:USDso", side="BUY", price="1.0010", quantity="1.00",
                                     market_rules={"tick_size": "0.0001", "quantity_step": "0.01", "minimum_quantity": "1", "minimum_notional": "1"},
                                     best_ask="1.0010", policy=p) is None
    assert build_rehearsal_candidate(market_symbol="SOMI:USDso", side="BUY", price="1.0000", quantity="1.00",
                                     market_rules={}, best_ask="1.0010", policy=p) is None


def test_read_only_collector_requires_explicit_invocation():
    calls = []
    def collector():
        calls.append(1)
        return _evidence()
    p = _policy()
    result = run_zero_mutation_rehearsal(policy=p, evidence=DreamDexZeroMutationRehearsalEvidence(), candidate=None, collector=collector)
    assert calls == [] and "read_only_collector_unavailable" not in result.blockers
    result = run_zero_mutation_rehearsal(policy=p, evidence=DreamDexZeroMutationRehearsalEvidence(), candidate=_candidate(p), execute_read_only=True, collector=collector)
    assert calls == [1]


def test_safe_diagnostics_do_not_expose_raw_address_or_calldata():
    p = _policy(required_market_address="0x1111111111111111111111111111111111111111", expected_signer_address="0x2222222222222222222222222222222222222222")
    result = run_zero_mutation_rehearsal(policy=p, evidence=_evidence(), candidate=_candidate(p))
    text = repr(p) + repr(result) + str(result.safe_dict())
    assert "0x1111111111111111111111111111111111111111" not in text
    assert "calldata" not in text.lower() and "private" not in text.lower()


def test_evidence_collector_accepts_typed_mapping():
    evidence = collect_live_read_only_rehearsal_evidence(lambda: {"market_status": "available", "network_read_call_count": 1})
    assert evidence.market_status == "available" and evidence.network_read_call_count == 1


def test_live_dependency_bundle_is_reader_only_and_rejects_secret_configuration():
    reader = lambda: None
    deps = DreamDexLiveReadOnlyRehearsalDependencies(reader, reader, reader, safe_config={"required_market_symbol": "SOMI:USDso"})
    assert "read-only" in repr(deps)
    assert not any(hasattr(deps, name) for name in ("signer", "submitter", "secret_provider", "journal"))
    with pytest.raises(ValueError):
        DreamDexLiveReadOnlyRehearsalDependencies(reader, reader, reader, safe_config={"access_token": "not-accepted"})


def test_dependency_collector_uses_typed_sources_and_clamps_future_age():
    observed = datetime.now(timezone.utc) + timedelta(seconds=10)
    market = SimpleNamespace(
        status="available", observed_at=observed,
        metadata=SimpleNamespace(symbol="SOMI:USDso", trading_rules=SimpleNamespace(available=True, trading_enabled=True)),
    )
    account = SimpleNamespace(
        observed_at=observed, account_address_semantics="unresolved",
        open_orders_status="source_unavailable", fills_status="source_unavailable",
    )
    calls = []
    deps = DreamDexLiveReadOnlyRehearsalDependencies(
        lambda: calls.append("market") or market,
        lambda: calls.append("account") or account,
        lambda: calls.append("rpc") or {"status": "unavailable", "read_only_rpc_call_count": 7},
        monotonic_clock=lambda: 1.0,
        safe_config={"required_market_symbol": "SOMI:USDso"},
    )
    evidence = collect_live_read_only_rehearsal_evidence_from_dependencies(deps)
    assert calls == ["market", "account", "rpc"]
    assert evidence.market_age_ms == 0 and evidence.account_age_ms == 0
    assert evidence.account_authority_status == "unresolved"
    assert evidence.open_order_status == "source_unavailable"
    assert evidence.fills_status == "source_unavailable"
    assert evidence.read_only_rpc_call_count == 7


def test_absent_account_session_does_not_suppress_public_or_rpc_readers():
    calls = []
    metadata = SimpleNamespace(
        symbol="SOMI:USDso", pool_contract="0x1111111111111111111111111111111111111111",
        observed_at=datetime.now(timezone.utc),
        trading_rules=SimpleNamespace(available=True, trading_enabled=True),
    )
    market = SimpleNamespace(
        status="available", observed_at=datetime.now(timezone.utc), metadata=metadata,
        orderbook={"bids": [{"price": "1", "quantity": "1"}], "asks": [{"price": "2", "quantity": "1"}]},
    )
    config = DreamDexLiveReadOnlyConfigurationStatus(
        public_api_configured=True, rpc_configured=True, public_transport_ready=True,
        rpc_transport_ready=True, market_symbol="SOMI:USDso",
    )
    deps = DreamDexLiveReadOnlyRehearsalDependencies(
        lambda: calls.append("market") or market,
        lambda: None,
        lambda: calls.append("rpc") or {"status": "available", "chain_id": 5031, "read_only_rpc_call_count": 6},
        safe_config={"required_market_symbol": "SOMI:USDso"}, configuration_status=config,
    )
    evidence = collect_live_read_only_rehearsal_evidence_from_dependencies(deps)
    assert calls == ["market", "rpc"]
    assert evidence.public_market_call_count == 1
    assert evidence.authenticated_account_call_count == 0
    assert evidence.read_only_rpc_call_count == 6
    assert evidence.account_status == "unavailable"


def test_dependency_collector_blocks_crossed_or_wide_orderbooks():
    metadata = SimpleNamespace(
        symbol="SOMI:USDso", pool_contract="0x1111111111111111111111111111111111111111",
        observed_at=datetime.now(timezone.utc),
        trading_rules=SimpleNamespace(available=True, trading_enabled=True),
    )
    account = SimpleNamespace(
        observed_at=datetime.now(timezone.utc), account_address_semantics="resolved",
        open_orders_status="available_empty", fills_status="available_empty",
    )
    rpc = {"status": "available", "chain_id": 5031, "read_only_rpc_call_count": 7}
    def collect(book):
        deps = DreamDexLiveReadOnlyRehearsalDependencies(
            lambda: SimpleNamespace(status="available", observed_at=datetime.now(timezone.utc), metadata=metadata, orderbook=book),
            lambda: account, lambda: rpc, safe_config={"required_market_symbol": "SOMI:USDso"},
        )
        return collect_live_read_only_rehearsal_evidence_from_dependencies(deps)
    assert collect({"bids": [{"price": "1.01", "quantity": "1"}], "asks": [{"price": "1.00", "quantity": "1"}]}).orderbook_status == "unavailable"
    assert collect({"bids": [{"price": "1.00", "quantity": "1"}], "asks": [{"price": "2.00", "quantity": "1"}]}).orderbook_status == "unavailable"


def test_explicit_disabled_trading_status_is_distinct_from_unavailable():
    metadata = SimpleNamespace(
        symbol="SOMI:USDso", pool_contract="0x1111111111111111111111111111111111111111",
        observed_at=datetime.now(timezone.utc),
        trading_rules=SimpleNamespace(
            available=True, trading_enabled=False,
            status_for=lambda name: "confirmed" if name == "trading_enabled" else "unavailable",
        ),
    )
    market = SimpleNamespace(
        status="available", observed_at=datetime.now(timezone.utc), metadata=metadata,
        orderbook={"bids": [{"price": "1", "quantity": "1"}], "asks": [{"price": "2", "quantity": "1"}]},
    )
    evidence = collect_live_read_only_rehearsal_evidence_from_dependencies(
        DreamDexLiveReadOnlyRehearsalDependencies(
            lambda: market, lambda: None, lambda: {"status": "not_configured"},
            safe_config={"required_market_symbol": "SOMI:USDso"},
        )
    )
    status = next(item for item in evidence.evidence_statuses if item.evidence_name == "trading_status")
    assert status.result_status == "confirmed"
    assert status.blocker == "market_trading_disabled"
    assert evidence.trading_enabled is False


def test_unavailable_trading_authority_propagates_specific_fail_closed_blockers():
    metadata = SimpleNamespace(
        symbol="SOMI:USDso", pool_contract="0x1111111111111111111111111111111111111111",
        observed_at=datetime.now(timezone.utc),
        trading_rules=SimpleNamespace(available=True, trading_enabled=False, status_for=lambda _name: "unavailable"),
    )
    market = SimpleNamespace(
        status="available", observed_at=datetime.now(timezone.utc), metadata=metadata,
        orderbook={"bids": [{"price": "1", "quantity": "1"}], "asks": [{"price": "2", "quantity": "1"}]},
    )
    deps = DreamDexLiveReadOnlyRehearsalDependencies(
        lambda: market, lambda: None,
        lambda: {"status": "not_configured", "gas_estimate_status": "not_attempted_due_to_prerequisite",
                 "call_statuses": {"gas_estimate": "not_attempted_due_to_prerequisite"}},
        safe_config={"required_market_symbol": "SOMI:USDso"},
    )
    evidence = collect_live_read_only_rehearsal_evidence_from_dependencies(deps)
    result = run_zero_mutation_rehearsal(policy=_policy(), evidence=evidence, candidate=None)
    assert "trading_status_authoritative_source_unavailable" in result.primary_blockers
    assert "market_lifecycle_unconfirmed" in result.primary_blockers
    assert "place_operation_support_unconfirmed" in result.primary_blockers
    assert "cancel_operation_support_unconfirmed" in result.primary_blockers
    assert result.gas_estimate_status == "not_attempted_due_to_prerequisite"
    assert result.ready_for_real_submission is False


def test_rehearsal_rpc_allowlist_has_only_read_only_methods():
    assert READ_ONLY_REHEARSAL_RPC_ALLOWLIST == {"eth_chainId", "eth_getCode", "eth_getTransactionCount", "eth_estimateGas", "eth_gasPrice", "eth_maxPriorityFeePerGas", "eth_getBalance"}
    assert all(not method.startswith(READ_ONLY_REHEARSAL_FORBIDDEN_PREFIXES) for method in READ_ONLY_REHEARSAL_RPC_ALLOWLIST)
    assert not any(method in READ_ONLY_REHEARSAL_RPC_ALLOWLIST for method in ("eth_sendTransaction", "eth_sendRawTransaction", "personal_sign", "wallet_sendTransaction"))


def test_fixture_cli_is_deterministic_and_live_mode_is_explicit(monkeypatch, capsys):
    import scripts.run_dreamdex_zero_mutation_rehearsal as script

    assert script.main(["--mode", "fixture"]) == 0
    fixture_output = capsys.readouterr().out
    assert "mode: fixture" in fixture_output
    assert "mutation RPC calls: 0" in fixture_output
    assert "submission call count: 0" in fixture_output
    assert "http" not in fixture_output.lower()

    monkeypatch.setattr(script, "_live_dependencies", lambda symbol: (_ for _ in ()).throw(RuntimeError("unavailable")))
    assert script.main(["--mode", "live-read-only"]) != 0
    live_output = capsys.readouterr().out
    assert "mode: live-read-only" in live_output
    assert "submission call count: 0" in live_output
    assert "private" not in live_output.lower()
    assert "token" not in live_output.lower()
