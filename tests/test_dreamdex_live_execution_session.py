from dataclasses import FrozenInstanceError

import pytest

from bot.execution.dreamdex_live_execution_session import (
    DreamDexExecutionArmingEvidence,
    DreamDexLiveExecutionSessionDependencies,
    DreamDexLiveExecutionSessionPolicy,
    DreamDexLiveExecutionState,
    build_live_execution_session_request,
    build_live_execution_session_preview,
    cancel_live_execution_session,
    evaluate_execution_arming,
    run_live_execution_session,
    serialize_live_execution_session_diagnostics,
)
from bot.execution.dreamdex_production_rpc import (
    DreamDexProductionRpcPolicy,
    HttpDreamDexRawTransactionSubmitter,
)
from bot.execution.dreamdex_readonly_rpc import DreamDexRpcError
from bot.execution.dreamdex_signed_transaction import DreamDexEphemeralSignedTransaction
from bot.execution.dreamdex_execution_primitives import build_execution_capability_matrix, DreamDexExecutionBlockers


MARKET = "0x" + "1" * 40
SIGNER = "0x" + "2" * 40


def _policy(**changes):
    return DreamDexLiveExecutionSessionPolicy(required_market_address=MARKET, required_signer_address=SIGNER, **changes)


def _request(policy, operation="place_order"):
    return build_live_execution_session_request(
        policy=policy,
        operation=operation,
        market_address=MARKET,
        signer_address=SIGNER,
        unsigned_request_fingerprint="a" * 64,
        launch_decision_fingerprint="b" * 64,
        journal_snapshot_fingerprint="c" * 64,
    )


def _armed():
    return DreamDexExecutionArmingEvidence(
        runtime_launch_approved=True,
        journal_clean=True,
        market_identity_confirmed=True,
        account_identity_confirmed=True,
        signer_metadata_confirmed=True,
        signer_unlock_verified_current_session=True,
        rpc_chain_confirmed=True,
        live_preflight_confirmed=True,
        nonce_revalidated=True,
        signing_lease_active=True,
        operation_allowlisted=True,
        explicit_session_approval=True,
        real_signing_policy_enabled=True,
        real_submission_policy_enabled=True,
    )


def _deps(calls):
    def step(name, value):
        def invoke(request, prior):
            calls.append(name)
            return value
        return invoke
    def preflight(request):
        calls.append("preflight")
        return {"status": "completed"}
    return DreamDexLiveExecutionSessionDependencies(
        preflight=preflight,
        persist_intent=step("intent", {"intent_id": "intent-1"}),
        reserve_nonce=step("nonce", {"reservation_id": "reservation-1"}),
        revalidate_nonce=step("revalidate", {"nonce": 1}),
        acquire_signing_lease=step("lease", {"lease_id": "lease-1"}),
        sign_and_verify=step("sign", {"transaction_hash": "0x" + "a" * 64}),
        submit_once=step("submit", {"transaction_hash": "0x" + "a" * 64}),
        confirm=step("confirm", {"status": "confirmed_success"}),
        reconcile=step("reconcile", {"status": "complete"}),
    )


def test_policy_defaults_are_disarmed_and_immutable():
    policy = _policy()
    assert policy.allow_real_signing is False
    assert policy.allow_real_submission is False
    assert policy.maximum_submission_attempts_per_operation == 1
    with pytest.raises(FrozenInstanceError):
        policy.allow_real_submission = True


def test_arming_requires_all_factors_and_environment_is_not_a_factor():
    policy = _policy(allow_real_signing=True, allow_real_submission=True)
    evidence = evaluate_execution_arming(policy, _armed())
    assert evidence.armed_for_signing is True
    assert evidence.armed_for_submission is True
    blocked = evaluate_execution_arming(_policy(), _armed())
    assert blocked.armed_for_signing is False
    assert blocked.armed_for_submission is False
    assert "live_execution_signing_disabled" in blocked.blockers
    assert "live_execution_submission_disabled" in blocked.blockers


def test_default_session_has_zero_side_effects():
    policy = _policy()
    request = _request(policy)
    calls = []
    result = run_live_execution_session(policy=policy, arming_evidence=DreamDexExecutionArmingEvidence(), request=request, dependencies=_deps(calls))
    assert result.final_state == DreamDexLiveExecutionState.GATE_REJECTED.value
    assert result.signer_invocation_count == 0
    assert result.submission_call_count == 0
    assert result.production_network_used is False
    assert result.production_secret_used is False
    assert calls == []


def test_test_armed_session_runs_once_and_reconciles():
    policy = _policy(allow_real_signing=True, allow_real_submission=True)
    calls = []
    result = run_live_execution_session(policy=policy, arming_evidence=_armed(), request=_request(policy), dependencies=_deps(calls))
    assert result.final_state == DreamDexLiveExecutionState.COMPLETED.value
    assert result.completed is True
    assert result.signer_invocation_count == 1
    assert result.submission_call_count == 1
    assert result.receipt_observation_count == 1
    assert result.automatic_retry_count == 0
    assert result.replacement_count == 0
    assert calls == ["preflight", "intent", "nonce", "revalidate", "lease", "sign", "submit", "confirm", "reconcile"]


def test_submission_failure_requires_recovery_and_never_retries():
    policy = _policy(allow_real_signing=True, allow_real_submission=True)
    calls = []
    base = _deps(calls)
    def fail_submit(request, prior):
        calls.append("submit")
        raise TimeoutError("provider timeout")
    deps = DreamDexLiveExecutionSessionDependencies(
        base.preflight, base.persist_intent, base.reserve_nonce, base.revalidate_nonce,
        base.acquire_signing_lease, base.sign_and_verify, fail_submit, base.confirm, base.reconcile,
    )
    result = run_live_execution_session(policy=policy, arming_evidence=_armed(), request=_request(policy), dependencies=deps)
    assert result.final_state == DreamDexLiveExecutionState.SUBMISSION_UNKNOWN.value
    assert result.recovery_required is True
    assert result.submission_call_count == 1
    assert calls.count("submit") == 1


def test_safe_session_diagnostics_reject_sensitive_mapping():
    with pytest.raises(ValueError):
        serialize_live_execution_session_diagnostics({"raw_transaction": "0xdead"})
    assert build_live_execution_session_preview()["production_network_used"] is False


def test_cancel_after_terminal_state_does_not_rewind_session():
    policy = _policy(allow_real_signing=True, allow_real_submission=True)
    result = run_live_execution_session(policy=policy, arming_evidence=_armed(), request=_request(policy), dependencies=_deps([]))
    cancelled = cancel_live_execution_session(result=result)
    assert cancelled.final_state == DreamDexLiveExecutionState.COMPLETED.value
    assert "live_execution_cancel_not_allowed_after_signing" in cancelled.blockers


class _Response:
    status_code = 200
    content = b'{"jsonrpc":"2.0","id":1,"result":"0x' + b"a" * 64 + b'"}'
    def json(self):
        return {"jsonrpc": "2.0", "id": 1, "result": "0x" + "a" * 64}


class _Client:
    def __init__(self): self.calls = []
    def post(self, *args, **kwargs): self.calls.append((args, kwargs)); return _Response()


def test_rpc_policy_is_bounded_and_submitter_is_disarmed_by_default():
    policy = DreamDexProductionRpcPolicy()
    assert policy.required_chain_id == 5031
    assert policy.allow_mutation_rpc is False
    assert policy.automatic_retry_allowed is False
    assert policy.allow_redirects is False
    client = _Client()
    submitter = HttpDreamDexRawTransactionSubmitter("https://rpc.invalid", http_client=client)
    assert submitter.describe_capabilities()["arbitrary_rpc"] == "unavailable"
    ephemeral = DreamDexEphemeralSignedTransaction(b"\x01", SIGNER, "request", "lease")
    with pytest.raises(DreamDexRpcError):
        submitter.submit_raw_transaction(ephemeral)
    assert client.calls == []


def test_rpc_policy_rejects_retry_redirects_and_bad_limits():
    with pytest.raises(ValueError):
        DreamDexProductionRpcPolicy(allow_redirects=True)
    with pytest.raises(ValueError):
        DreamDexProductionRpcPolicy(maximum_submission_attempts=2)
    with pytest.raises(ValueError):
        DreamDexProductionRpcPolicy(maximum_response_bytes=0)


def test_capabilities_and_blocker_registry_include_live_session_surface():
    matrix = build_execution_capability_matrix()
    assert matrix.by_name("production_rpc_policy").status == "available_offline"
    assert matrix.by_name("production_raw_transaction_submitter").status == "partial"
    assert matrix.by_name("live_execution_session_model").status == "available_offline"
    assert matrix.by_name("production_live_submission").status == "unavailable"
    assert DreamDexExecutionBlockers.normalize(("live_execution_session_unavailable",)) == ("live_execution_session_unavailable",)


def test_opt_in_submitter_uses_one_allowlisted_method_and_returns_local_hash(tmp_path):
    from test_dreamdex_transaction_submission import _signed

    journal, material, artifact, ephemeral = _signed(tmp_path / "submitter.sqlite")
    class Client:
        def __init__(self): self.calls = []
        def post(self, url, **kwargs):
            self.calls.append((url, kwargs))
            class Response:
                status_code = 200
                content = b"{}"
                def json(self): return {"jsonrpc": "2.0", "id": 1, "result": artifact.signed_transaction_hash}
            return Response()
    client = Client()
    policy = DreamDexProductionRpcPolicy(rpc_configuration_status="test_confirmed", allow_mutation_rpc=True)
    submitter = HttpDreamDexRawTransactionSubmitter("https://rpc.invalid", policy=policy, http_client=client)
    response = submitter.submit_raw_transaction(ephemeral)
    assert response.response_status == "accepted"
    assert response.locally_calculated_transaction_hash == artifact.signed_transaction_hash
    assert response.exact_hash_match is True
    assert submitter.invocation_count == 1
    assert len(client.calls) == 1
    assert client.calls[0][1]["json"]["method"] == "eth_sendRawTransaction"
    assert set(client.calls[0][1]["json"]) == {"jsonrpc", "id", "method", "params"}
    journal.close()
