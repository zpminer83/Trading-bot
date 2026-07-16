"""Deterministic end-to-end orchestration boundary.

The orchestrator has no concrete signer, submitter, RPC, HTTP, or secret
loader.  All executable dependencies are supplied explicitly by the caller;
the production path therefore remains inert by construction.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from hashlib import sha256
import json
from typing import Any, Callable, Mapping, Sequence

from bot.execution.dreamdex_runtime_launch_gate import (
    DreamDexRuntimeLaunchDecision,
    DreamDexRuntimeLaunchEvidence,
    DreamDexRuntimeLaunchPolicy,
    evaluate_runtime_launch_gate,
)

SCHEMA_VERSION = "1"


class DreamDexDryRunState(str, Enum):
    NOT_STARTED = "not_started"
    GATE_REJECTED = "gate_rejected"
    INTENT_CREATED = "intent_created"
    UNSIGNED_REQUEST_BUILT = "unsigned_request_built"
    ENVELOPE_BUILT = "envelope_built"
    PREFLIGHT_RESOLVED = "preflight_resolved"
    JOURNAL_INTENT_CREATED = "journal_intent_created"
    NONCE_RESERVED = "nonce_reserved"
    SIGNING_LEASE_ACQUIRED = "signing_lease_acquired"
    SIGNING_STARTED = "signing_started"
    SIGNED_VERIFIED = "signed_verified"
    SUBMISSION_STARTED = "submission_started"
    SUBMITTED = "submitted"
    CONFIRMATION_PENDING = "confirmation_pending"
    CONFIRMED_SUCCESS = "confirmed_success"
    CONFIRMED_REVERTED = "confirmed_reverted"
    CONFIRMED_MISSING_EVENT = "confirmed_missing_event"
    RECONCILIATION_COMPLETE = "reconciliation_complete"
    FAILED = "failed"
    RECOVERY_REQUIRED = "recovery_required"


_ORDER = tuple(DreamDexDryRunState)
_INDEX = {item: index for index, item in enumerate(_ORDER)}


def _fp(value: Any, domain: str = "dry_run") -> str:
    return sha256((domain + ":" + json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)).encode()).hexdigest()


def _tuple(values: Sequence[str] | None) -> tuple[str, ...]:
    return tuple(dict.fromkeys(str(value) for value in (values or ()) if str(value)))


@dataclass(frozen=True, repr=False)
class DreamDexDryRunStageResult:
    stage: str
    status: str
    execution_performed: bool
    input_fingerprint: str
    output_fingerprint: str
    journal_state: str
    RPC_call_count: int = 0
    signer_invocation_count: int = 0
    submission_call_count: int = 0
    blockers: tuple[str, ...] = ()
    validation_errors: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if any(isinstance(v, bool) or not isinstance(v, int) or v < 0 for v in (self.RPC_call_count, self.signer_invocation_count, self.submission_call_count)):
            raise ValueError("stage_counter_invalid")
        object.__setattr__(self, "blockers", _tuple(self.blockers)); object.__setattr__(self, "validation_errors", _tuple(self.validation_errors))

    def safe_dict(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}

    def __repr__(self) -> str:
        return f"DreamDexDryRunStageResult(stage={self.stage!r}, status={self.status!r}, execution_performed={self.execution_performed!r})"


@dataclass(frozen=True)
class DreamDexDryRunDependencies:
    """Typed callback boundary; concrete implementations belong to tests/scripts."""
    build_unsigned_request: Callable[[str, int], Any]
    build_envelope: Callable[[str, Any], Any]
    preflight: Callable[[str, Any], Any]
    create_journal_intent: Callable[[str, Any], Any]
    reserve_nonce: Callable[[str, Any], Any]
    acquire_signing_lease: Callable[[str, Any], Any]
    sign_and_verify: Callable[[str, Any], Any]
    submit_once: Callable[[str, Any], Any]
    confirm: Callable[[str, Any], Any]
    reconcile: Callable[[str, Any], Any]
    network_execution_performed: bool = False
    production_secret_used: bool = False

    def __post_init__(self) -> None:
        if self.network_execution_performed or self.production_secret_used:
            raise ValueError("synthetic_dependencies_must_be_offline")


@dataclass(frozen=True, repr=False)
class DreamDexEndToEndDryRunResult:
    schema_version: str
    scenario_name: str
    launch_decision: DreamDexRuntimeLaunchDecision
    final_state: DreamDexDryRunState
    stage_results: tuple[DreamDexDryRunStageResult, ...]
    place_intent_id: str | None = None
    place_transaction_hash: str | None = None
    confirmed_order_identity_status: str = "unavailable"
    cancel_intent_id: str | None = None
    cancel_transaction_hash: str | None = None
    cancel_confirmation_status: str = "unavailable"
    final_open_order_status: str = "unavailable"
    journal_integrity_status: str = "unavailable"
    reconciliation_status: str = "incomplete"
    signer_invocation_count: int = 0
    submission_call_count: int = 0
    receipt_observation_count: int = 0
    automatic_retry_count: int = 0
    replacement_count: int = 0
    network_execution_performed: bool = False
    production_secret_used: bool = False
    dry_run_fingerprint: str = ""
    synthetic_dry_run_passed: bool = False
    production_dry_run_approved: bool = False
    ready_for_production_signer_integration: bool = False
    ready_for_real_submission: bool = False
    blockers: tuple[str, ...] = ()
    validation_errors: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.network_execution_performed or self.production_secret_used or self.production_dry_run_approved or self.ready_for_real_submission:
            raise ValueError("dry_run_production_safety_invariant")
        if any(isinstance(v, bool) or not isinstance(v, int) or v < 0 for v in (self.signer_invocation_count, self.submission_call_count, self.receipt_observation_count, self.automatic_retry_count, self.replacement_count)):
            raise ValueError("dry_run_counter_invalid")
        if self.automatic_retry_count != 0 or self.replacement_count != 0:
            raise ValueError("dry_run_retry_replacement_forbidden")
        object.__setattr__(self, "blockers", _tuple(self.blockers)); object.__setattr__(self, "validation_errors", _tuple(self.validation_errors))

    def safe_dict(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__ if name != "launch_decision"} | {"launch_decision": self.launch_decision.safe_dict(), "final_state": self.final_state.value, "stage_results": tuple(item.safe_dict() for item in self.stage_results), "network_execution_performed": False, "production_secret_used": False, "production_dry_run_approved": False, "ready_for_real_submission": False}

    def __repr__(self) -> str:
        return f"DreamDexEndToEndDryRunResult(scenario={self.scenario_name!r}, final_state={self.final_state.value!r}, passed={self.synthetic_dry_run_passed!r})"


def _stage(results: list[DreamDexDryRunStageResult], name: str, status: str, value: Any, *, blockers: Sequence[str] = (), error: Sequence[str] = (), journal_state: str = "unavailable", signer: int = 0, submission: int = 0) -> None:
    results.append(DreamDexDryRunStageResult(name, status, True, _fp({"stage": name, "input": value}), _fp({"stage": name, "output": value}), journal_state, 0, signer, submission, tuple(blockers), tuple(error)))


def run_dreamdex_dry_run(*, policy: DreamDexRuntimeLaunchPolicy, evidence: DreamDexRuntimeLaunchEvidence, dependencies: DreamDexDryRunDependencies, scenario_name: str = "happy-path") -> DreamDexEndToEndDryRunResult:
    decision = evaluate_runtime_launch_gate(policy, evidence, synthetic_dependencies_supplied=True)
    stages: list[DreamDexDryRunStageResult] = []
    if not decision.allowed_to_run_synthetic_dry_run:
        _stage(stages, "launch_gate", "failed", None, blockers=decision.blockers, journal_state="not_started")
        return DreamDexEndToEndDryRunResult(SCHEMA_VERSION, scenario_name, decision, DreamDexDryRunState.GATE_REJECTED, tuple(stages), blockers=decision.blockers, dry_run_fingerprint=_fp({"scenario": scenario_name, "decision": decision.decision_fingerprint}))

    counters = {"signer": 0, "submission": 0, "receipt": 0}
    place_id = place_hash = cancel_id = cancel_hash = None
    order_identity = "unavailable"; confirmed_order_id: Any = None; cancel_status = "unavailable"; final_open = "unavailable"; journal_status = "unavailable"; reconciliation = "incomplete"
    current = DreamDexDryRunState.NOT_STARTED

    def flow(operation: str, nonce: int) -> tuple[bool, str | None, str | None]:
        nonlocal current, journal_status, reconciliation, order_identity, confirmed_order_id, cancel_status, final_open
        flow_state = DreamDexDryRunState.NOT_STARTED

        def advance(target: DreamDexDryRunState) -> None:
            nonlocal flow_state
            if _INDEX[target] < _INDEX[flow_state]:
                raise RuntimeError("state_transition_backward")
            flow_state = target

        try:
            intent_seed = {"operation": operation, "nonce": nonce, "intent_id": _fp({"operation": operation, "nonce": nonce}, "intent")}
            advance(DreamDexDryRunState.INTENT_CREATED); _stage(stages, f"{operation}.intent_created", "passed", intent_seed, journal_state=flow_state.value)
            unsigned = dependencies.build_unsigned_request(operation, nonce)
            advance(DreamDexDryRunState.UNSIGNED_REQUEST_BUILT); _stage(stages, f"{operation}.unsigned_request", "passed", unsigned, journal_state=flow_state.value)
            envelope = dependencies.build_envelope(operation, unsigned)
            advance(DreamDexDryRunState.ENVELOPE_BUILT); _stage(stages, f"{operation}.envelope", "passed", envelope, journal_state=flow_state.value)
            preflight = dependencies.preflight(operation, envelope)
            advance(DreamDexDryRunState.PREFLIGHT_RESOLVED); _stage(stages, f"{operation}.preflight", "passed", preflight, journal_state=flow_state.value)
            intent = dependencies.create_journal_intent(operation, preflight)
            advance(DreamDexDryRunState.JOURNAL_INTENT_CREATED); journal_status = "clean"; _stage(stages, f"{operation}.journal_intent", "passed", intent, journal_state=flow_state.value)
            reservation = dependencies.reserve_nonce(operation, intent)
            advance(DreamDexDryRunState.NONCE_RESERVED); _stage(stages, f"{operation}.nonce", "passed", reservation, journal_state=flow_state.value)
            lease = dependencies.acquire_signing_lease(operation, reservation)
            advance(DreamDexDryRunState.SIGNING_LEASE_ACQUIRED); _stage(stages, f"{operation}.lease", "passed", lease, journal_state=flow_state.value)
            advance(DreamDexDryRunState.SIGNING_STARTED); _stage(stages, f"{operation}.signing_started", "passed", lease, journal_state=flow_state.value)
            signed = dependencies.sign_and_verify(operation, lease); counters["signer"] += 1
            advance(DreamDexDryRunState.SIGNED_VERIFIED); _stage(stages, f"{operation}.signed_verified", "passed", signed, journal_state=flow_state.value, signer=1)
            advance(DreamDexDryRunState.SUBMISSION_STARTED); _stage(stages, f"{operation}.submission_started", "passed", signed, journal_state=flow_state.value)
            submitted = dependencies.submit_once(operation, signed); counters["submission"] += 1
            advance(DreamDexDryRunState.SUBMITTED); _stage(stages, f"{operation}.submitted", "passed", submitted, journal_state=flow_state.value, submission=1)
            signed_hash = signed.get("transaction_hash") if isinstance(signed, Mapping) else getattr(signed, "transaction_hash", None)
            submitted_hash = submitted.get("transaction_hash") if isinstance(submitted, Mapping) else getattr(submitted, "transaction_hash", None)
            if signed_hash is not None and submitted_hash is not None and str(signed_hash) != str(submitted_hash):
                raise RuntimeError("rpc_hash_mismatch")
            advance(DreamDexDryRunState.CONFIRMATION_PENDING); _stage(stages, f"{operation}.confirmation_pending", "passed", submitted, journal_state=flow_state.value)
            confirmed = dependencies.confirm(operation, submitted); counters["receipt"] += 1
            advance(DreamDexDryRunState.CONFIRMED_SUCCESS); _stage(stages, f"{operation}.confirmed", "passed", confirmed, journal_state=flow_state.value)
            if not isinstance(confirmed, Mapping) or confirmed.get("status") != "confirmed_success":
                raise RuntimeError("confirmation_failed")
            if operation == "place_order":
                confirmed_order_id = confirmed.get("order_id")
                order_identity = "confirmed" if confirmed_order_id is not None else "unavailable"
            elif confirmed.get("order_id") != confirmed_order_id:
                raise RuntimeError("cancel_order_id_mismatch")
            else:
                cancel_status = "confirmed_success"
            reconciled = dependencies.reconcile(operation, confirmed)
            if not isinstance(reconciled, Mapping) or reconciled.get("status") != "complete":
                raise RuntimeError("reconciliation_incomplete")
            advance(DreamDexDryRunState.RECONCILIATION_COMPLETE); _stage(stages, f"{operation}.reconciliation", "passed", reconciled, journal_state=flow_state.value)
            reconciliation = "complete"; final_open = "absent" if operation == "cancel_order" else "present"
            intent_id = str((intent.get("intent_id") if isinstance(intent, Mapping) else getattr(intent, "intent_id", None)) or intent_seed["intent_id"])
            tx_hash = (submitted.get("transaction_hash") or submitted.get("signed_transaction_hash") or submitted.get("hash")) if isinstance(submitted, Mapping) else getattr(submitted, "transaction_hash", None)
            return True, intent_id, str(tx_hash) if tx_hash else None
        except Exception as exc:
            current = DreamDexDryRunState.RECOVERY_REQUIRED if "recovery" in str(exc).lower() else DreamDexDryRunState.FAILED
            _stage(stages, f"{operation}.failed", "failed", None, blockers=("synthetic_dry_run_failed",), error=(type(exc).__name__,), journal_state=current.value)
            return False, None, None

    ok, place_id, place_hash = flow("place_order", 100)
    if ok:
        ok, cancel_id, cancel_hash = flow("cancel_order", 101)
    passed = ok and order_identity == "confirmed" and cancel_status == "confirmed_success" and final_open == "absent" and reconciliation == "complete" and counters["signer"] == 2 and counters["submission"] == 2
    if passed:
        current = DreamDexDryRunState.RECONCILIATION_COMPLETE
    blockers = () if passed else ("synthetic_dry_run_failed",)
    fingerprint = _fp({"scenario": scenario_name, "place_intent": place_id, "cancel_intent": cancel_id, "order_identity": order_identity, "cancel_status": cancel_status, "final_open": final_open, "stages": tuple((item.stage, item.status, item.input_fingerprint, item.output_fingerprint) for item in stages)})
    return DreamDexEndToEndDryRunResult(SCHEMA_VERSION, scenario_name, decision, current, tuple(stages), place_id, place_hash, order_identity, cancel_id, cancel_hash, cancel_status, final_open, journal_status, reconciliation, counters["signer"], counters["submission"], counters["receipt"], 0, 0, False, False, fingerprint, passed, False, passed, False, blockers)


def serialize_dry_run_diagnostics(value: DreamDexEndToEndDryRunResult | DreamDexDryRunStageResult) -> dict[str, Any]:
    return value.safe_dict()


__all__ = ["SCHEMA_VERSION", "DreamDexDryRunState", "DreamDexDryRunStageResult", "DreamDexDryRunDependencies", "DreamDexEndToEndDryRunResult", "run_dreamdex_dry_run", "serialize_dry_run_diagnostics"]
