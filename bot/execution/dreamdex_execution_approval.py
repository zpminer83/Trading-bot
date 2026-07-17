"""One-time, in-memory human approval ceremony for safe transaction previews.

This is deliberately not an authentication, signing, submission, or persistence
mechanism.  It has no environment/file input and performs no network I/O.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal
from hashlib import sha256
import json
import secrets
import sys
from typing import Any, Callable, Mapping, Protocol, Sequence

from bot.execution.dreamdex_execution_primitives import mask_evm_address, mask_hex_hash, validate_evm_address

SCHEMA_VERSION = "1"


def _tuple(values: Sequence[str] | None = None) -> tuple[str, ...]:
    return tuple(dict.fromkeys(str(value) for value in (values or ()) if str(value)))


def _fp(value: Any, domain: str) -> str:
    return sha256((domain + ":" + json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)).encode()).hexdigest()


def _decimal(value: Any, field: str) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(field + "_invalid")
    try:
        parsed = Decimal(str(value))
    except Exception as exc:
        raise ValueError(field + "_invalid") from exc
    if not parsed.is_finite() or parsed < 0:
        raise ValueError(field + "_invalid")
    return parsed


def _challenge_value(generator: Callable[[], str] | None = None) -> str:
    if generator is None:
        alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
        token = "".join(secrets.choice(alphabet) for _ in range(8))
        value = f"APPROVE-{token[:4]}-{token[4:]}"
    else:
        value = generator()
    # Permit the documented APPROVE-XXXX-XXXX format only.
    if not isinstance(value, str) or len(value) != 17 or not value.startswith("APPROVE-") or value[12] != "-":
        raise ValueError("approval_challenge_generator_invalid")
    return value


@dataclass(frozen=True, repr=False)
class DreamDexExecutionApprovalPreview:
    schema_version: str
    operation: str
    chain_id: int
    market_symbol: str
    market_address: str
    signer_address: str
    transaction_type: str
    side: str | None
    order_type: str | None
    normalized_price: Decimal | None
    normalized_quantity: Decimal | None
    order_notional: Decimal | None
    value_wei: int | None
    nonce: int | None
    gas_limit: int | None
    gas_price_wei: int | None
    max_fee_per_gas_wei: int | None
    max_priority_fee_per_gas_wei: int | None
    maximum_total_fee_wei: int | None
    selector: str | None
    unsigned_request_fingerprint: str | None
    envelope_fingerprint: str | None
    preflight_fingerprint: str | None
    intent_fingerprint: str | None
    reservation_fingerprint: str | None
    lease_fingerprint: str | None
    approval_binding_fingerprint: str
    preview_status: str
    authoritative: bool = False
    blockers: tuple[str, ...] = ()
    validation_errors: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION or self.operation not in {"place_order", "cancel_order", "reduce_order"} or self.chain_id != 5031:
            raise ValueError("execution_approval_preview_invalid")
        object.__setattr__(self, "market_address", validate_evm_address(self.market_address, field="market_address"))
        object.__setattr__(self, "signer_address", validate_evm_address(self.signer_address, field="signer_address"))
        for name in ("normalized_price", "normalized_quantity", "order_notional"):
            object.__setattr__(self, name, _decimal(getattr(self, name), name))
        for name in ("value_wei", "nonce", "gas_limit", "gas_price_wei", "max_fee_per_gas_wei", "max_priority_fee_per_gas_wei", "maximum_total_fee_wei"):
            value = getattr(self, name)
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
                raise ValueError(name + "_invalid")
        object.__setattr__(self, "authoritative", False)
        object.__setattr__(self, "blockers", _tuple(self.blockers)); object.__setattr__(self, "validation_errors", _tuple(self.validation_errors))

    def safe_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version, "operation": self.operation, "chain_id": self.chain_id, "market_symbol": self.market_symbol,
            "market_address_masked": mask_evm_address(self.market_address), "signer_address_masked": mask_evm_address(self.signer_address),
            "transaction_type": self.transaction_type, "side": self.side, "order_type": self.order_type,
            "normalized_price": str(self.normalized_price) if self.normalized_price is not None else None,
            "normalized_quantity": str(self.normalized_quantity) if self.normalized_quantity is not None else None,
            "order_notional": str(self.order_notional) if self.order_notional is not None else None,
            "value_wei": self.value_wei, "nonce": self.nonce, "gas_limit": self.gas_limit, "gas_price_wei": self.gas_price_wei,
            "max_fee_per_gas_wei": self.max_fee_per_gas_wei, "max_priority_fee_per_gas_wei": self.max_priority_fee_per_gas_wei,
            "maximum_total_fee_wei": self.maximum_total_fee_wei, "selector": self.selector,
            "unsigned_request_fingerprint": mask_hex_hash(self.unsigned_request_fingerprint), "envelope_fingerprint": mask_hex_hash(self.envelope_fingerprint),
            "preflight_fingerprint": mask_hex_hash(self.preflight_fingerprint), "intent_fingerprint": mask_hex_hash(self.intent_fingerprint),
            "reservation_fingerprint": mask_hex_hash(self.reservation_fingerprint), "lease_fingerprint": mask_hex_hash(self.lease_fingerprint),
            "approval_binding_fingerprint": mask_hex_hash(self.approval_binding_fingerprint), "preview_status": self.preview_status,
            "authoritative": False, "blockers": self.blockers, "validation_errors": self.validation_errors,
        }

    def __repr__(self) -> str:
        return f"DreamDexExecutionApprovalPreview(operation={self.operation!r}, market={mask_evm_address(self.market_address)!r}, signer={mask_evm_address(self.signer_address)!r}, binding={mask_hex_hash(self.approval_binding_fingerprint)!r})"


def build_execution_approval_preview(*, operation: str, chain_id: int, market_symbol: str, market_address: str, signer_address: str, transaction_type: str, side: str | None = None, order_type: str | None = None, normalized_price: Any = None, normalized_quantity: Any = None, order_notional: Any = None, value_wei: int | None = None, nonce: int | None = None, gas_limit: int | None = None, gas_price_wei: int | None = None, max_fee_per_gas_wei: int | None = None, max_priority_fee_per_gas_wei: int | None = None, maximum_total_fee_wei: int | None = None, calldata: bytes | str | None = None, selector: str | None = None, session_request_fingerprint: str | None = None, unsigned_request_fingerprint: str | None = None, envelope_fingerprint: str | None = None, preflight_fingerprint: str | None = None, intent_id: str | None = None, intent_fingerprint: str | None = None, reservation_id: str | None = None, reservation_fingerprint: str | None = None, lease_id: str | None = None, lease_fingerprint: str | None = None, runtime_launch_decision_fingerprint: str | None = None, journal_snapshot_fingerprint: str | None = None, market_evidence_fingerprint: str | None = None, account_evidence_fingerprint: str | None = None, risk_decision_fingerprint: str | None = None, fair_play_decision_fingerprint: str | None = None, preview_status: str = "available", blockers: Sequence[str] = (), validation_errors: Sequence[str] = ()) -> DreamDexExecutionApprovalPreview:
    raw = bytes.fromhex(calldata[2:] if isinstance(calldata, str) and calldata.startswith("0x") else calldata) if calldata is not None else b""
    calldata_hash = sha256(raw).hexdigest()
    selector_value = selector or ("0x" + raw[:4].hex() if raw else None)
    binding = {
        "schema_version": SCHEMA_VERSION, "operation": operation, "chain_id": chain_id, "market_address": market_address.lower(), "signer_address": signer_address.lower(),
        "transaction_type": transaction_type, "nonce": nonce, "value_wei": value_wei, "gas_limit": gas_limit, "gas_price_wei": gas_price_wei,
        "max_fee_per_gas_wei": max_fee_per_gas_wei, "max_priority_fee_per_gas_wei": max_priority_fee_per_gas_wei, "maximum_total_fee_wei": maximum_total_fee_wei,
        "calldata_sha256": calldata_hash, "selector": selector_value, "session_request_fingerprint": session_request_fingerprint, "unsigned_request_fingerprint": unsigned_request_fingerprint,
        "envelope_fingerprint": envelope_fingerprint, "preflight_fingerprint": preflight_fingerprint, "intent_id": intent_id, "intent_fingerprint": intent_fingerprint,
        "reservation_id": reservation_id, "reservation_fingerprint": reservation_fingerprint, "lease_id": lease_id, "lease_fingerprint": lease_fingerprint,
        "runtime_launch_decision_fingerprint": runtime_launch_decision_fingerprint, "journal_snapshot_fingerprint": journal_snapshot_fingerprint,
        "market_evidence_fingerprint": market_evidence_fingerprint, "account_evidence_fingerprint": account_evidence_fingerprint,
        "risk_decision_fingerprint": risk_decision_fingerprint, "fair_play_decision_fingerprint": fair_play_decision_fingerprint,
    }
    return DreamDexExecutionApprovalPreview(SCHEMA_VERSION, operation, chain_id, market_symbol, market_address, signer_address, transaction_type, side, order_type, normalized_price, normalized_quantity, order_notional, value_wei, nonce, gas_limit, gas_price_wei, max_fee_per_gas_wei, max_priority_fee_per_gas_wei, maximum_total_fee_wei, selector_value, unsigned_request_fingerprint, envelope_fingerprint, preflight_fingerprint, intent_fingerprint, reservation_fingerprint, lease_fingerprint, _fp(binding, "dreamdex/execution-approval-binding"), preview_status, False, tuple(blockers), tuple(validation_errors))


@dataclass(frozen=True, repr=False)
class DreamDexExecutionApprovalChallenge:
    schema_version: str
    challenge_status: str
    challenge_display_value: str
    approval_binding_fingerprint: str
    issued_monotonic_ms: int
    expires_monotonic_ms: int
    maximum_attempts: int
    attempt_count: int
    challenge_fingerprint: str
    authoritative: bool = False
    blockers: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION or self.maximum_attempts != 1 or self.expires_monotonic_ms <= self.issued_monotonic_ms:
            raise ValueError("execution_approval_challenge_invalid")
        object.__setattr__(self, "authoritative", False); object.__setattr__(self, "blockers", _tuple(self.blockers))

    def safe_dict(self) -> dict[str, Any]:
        # Deliberately omit the challenge value: it is transient process memory,
        # not a diagnostic or persisted credential.
        return {"schema_version": self.schema_version, "challenge_status": self.challenge_status, "approval_binding_fingerprint": mask_hex_hash(self.approval_binding_fingerprint), "issued_monotonic_ms": self.issued_monotonic_ms, "expires_monotonic_ms": self.expires_monotonic_ms, "maximum_attempts": 1, "attempt_count": self.attempt_count, "challenge_fingerprint": mask_hex_hash(self.challenge_fingerprint), "authoritative": False, "blockers": self.blockers}

    def __repr__(self) -> str:
        return f"DreamDexExecutionApprovalChallenge(status={self.challenge_status!r}, attempts={self.attempt_count!r}, binding={mask_hex_hash(self.approval_binding_fingerprint)!r})"


def create_execution_approval_challenge(preview: DreamDexExecutionApprovalPreview, *, issued_monotonic_ms: int, maximum_age_ms: int = 60_000, challenge_generator: Callable[[], str] | None = None) -> DreamDexExecutionApprovalChallenge:
    if not isinstance(preview, DreamDexExecutionApprovalPreview) or maximum_age_ms <= 0:
        raise ValueError("approval_challenge_inputs_invalid")
    value = _challenge_value(challenge_generator)
    expiry = int(issued_monotonic_ms) + int(maximum_age_ms)
    return DreamDexExecutionApprovalChallenge(SCHEMA_VERSION, "issued", value, preview.approval_binding_fingerprint, int(issued_monotonic_ms), expiry, 1, 0, _fp({"binding": preview.approval_binding_fingerprint, "challenge": value}, "dreamdex/approval-challenge"))


class DreamDexExecutionApprovalProvider(Protocol):
    def request_approval(self, preview: DreamDexExecutionApprovalPreview, challenge_display_value: str, expiry_status: str) -> str | None: ...


class InteractiveDreamDexExecutionApprovalProvider:
    """TTY-only provider; invoked solely by an explicit ceremony call."""
    provider_type = "interactive_tty"

    def __init__(self, *, stdin: Any = None, stdout: Any = None, input_fn: Callable[[str], str] | None = None) -> None:
        self._stdin, self._stdout, self._input = stdin or sys.stdin, stdout or sys.stdout, input_fn or input
        self.invocation_count = 0

    def request_approval(self, preview: DreamDexExecutionApprovalPreview, challenge_display_value: str, expiry_status: str) -> str | None:
        self.invocation_count += 1
        if self.invocation_count != 1 or not bool(getattr(self._stdin, "isatty", lambda: False)()) or not bool(getattr(self._stdout, "isatty", lambda: False)()) or expiry_status != "current":
            return None
        writer = getattr(self._stdout, "write", None)
        if callable(writer):
            writer("Execution approval preview: " + ", ".join((
                f"operation={preview.operation}", f"side={preview.side}", f"price={preview.normalized_price}",
                f"quantity={preview.normalized_quantity}", f"notional={preview.order_notional}",
                f"native_value={preview.value_wei}", f"max_fee={preview.maximum_total_fee_wei}",
                f"nonce={preview.nonce}", f"transaction_type={preview.transaction_type}",
            )) + "\n")
            writer("Approval challenge: " + challenge_display_value + "\n")
        try:
            return self._input("Type the displayed approval challenge exactly: ")
        except (EOFError, KeyboardInterrupt):
            return None


class UnavailableDreamDexExecutionApprovalProvider:
    provider_type = "unavailable"
    def request_approval(self, preview: DreamDexExecutionApprovalPreview, challenge_display_value: str, expiry_status: str) -> str | None:
        return None


@dataclass(frozen=True, repr=False)
class DreamDexExecutionApprovalEvidence:
    schema_version: str
    approval_status: str
    approval_binding_fingerprint: str
    challenge_fingerprint: str
    provider_type: str
    provider_invoked: bool
    terminal_interactive: bool
    attempt_count: int
    approved: bool
    approval_issued_monotonic_ms: int
    approval_expires_monotonic_ms: int
    approval_consumed: bool
    approval_reference_released: bool
    approval_fingerprint: str
    authoritative: bool = False
    blockers: tuple[str, ...] = ()
    validation_errors: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION or self.attempt_count < 0 or self.attempt_count > 1:
            raise ValueError("execution_approval_evidence_invalid")
        object.__setattr__(self, "authoritative", False); object.__setattr__(self, "blockers", _tuple(self.blockers)); object.__setattr__(self, "validation_errors", _tuple(self.validation_errors))

    def safe_dict(self) -> dict[str, Any]:
        return {"schema_version": self.schema_version, "approval_status": self.approval_status, "approval_binding_fingerprint": mask_hex_hash(self.approval_binding_fingerprint), "challenge_fingerprint": mask_hex_hash(self.challenge_fingerprint), "provider_type": self.provider_type, "provider_invoked": self.provider_invoked, "terminal_interactive": self.terminal_interactive, "attempt_count": self.attempt_count, "approved": self.approved, "approval_issued_monotonic_ms": self.approval_issued_monotonic_ms, "approval_expires_monotonic_ms": self.approval_expires_monotonic_ms, "approval_consumed": self.approval_consumed, "approval_reference_released": self.approval_reference_released, "approval_fingerprint": mask_hex_hash(self.approval_fingerprint), "authoritative": False, "blockers": self.blockers, "validation_errors": self.validation_errors}

    def __repr__(self) -> str:
        return f"DreamDexExecutionApprovalEvidence(status={self.approval_status!r}, approved={self.approved!r}, consumed={self.approval_consumed!r})"


def evaluate_execution_approval(*, challenge: DreamDexExecutionApprovalChallenge, preview: DreamDexExecutionApprovalPreview, provider: DreamDexExecutionApprovalProvider, now_monotonic_ms: int) -> DreamDexExecutionApprovalEvidence:
    if challenge.approval_binding_fingerprint != preview.approval_binding_fingerprint:
        raise ValueError("execution_approval_binding_mismatch")
    current = int(now_monotonic_ms) <= challenge.expires_monotonic_ms
    terminal = isinstance(provider, InteractiveDreamDexExecutionApprovalProvider) and bool(getattr(provider._stdin, "isatty", lambda: False)()) and bool(getattr(provider._stdout, "isatty", lambda: False)())
    response = provider.request_approval(preview, challenge.challenge_display_value, "current" if current else "expired")
    invoked = not isinstance(provider, UnavailableDreamDexExecutionApprovalProvider)
    # Exact, case-sensitive comparison; whitespace is deliberately rejected.
    approved = current and response == challenge.challenge_display_value
    status = "approved" if approved else "expired" if not current else "rejected"
    blockers = () if approved else (("execution_approval_expired",) if not current else ("execution_approval_rejected",))
    fp = _fp({"binding": preview.approval_binding_fingerprint, "challenge": challenge.challenge_fingerprint, "status": status, "provider": getattr(provider, "provider_type", type(provider).__name__)}, "dreamdex/approval-evidence")
    return DreamDexExecutionApprovalEvidence(SCHEMA_VERSION, status, preview.approval_binding_fingerprint, challenge.challenge_fingerprint, getattr(provider, "provider_type", type(provider).__name__), invoked, terminal, 1 if invoked else 0, approved, challenge.issued_monotonic_ms, challenge.expires_monotonic_ms, False, False, fp, False, blockers)


class DreamDexExecutionApprovalRegistry:
    """Explicit, process-local replay guard.  It has no persistence surface."""
    def __init__(self) -> None:
        self._consumed: set[tuple[str, str]] = set()

    def consume(self, evidence: DreamDexExecutionApprovalEvidence) -> DreamDexExecutionApprovalEvidence:
        key = (evidence.approval_binding_fingerprint, evidence.challenge_fingerprint)
        if not evidence.approved or evidence.approval_consumed or key in self._consumed:
            return replace(evidence, approval_status="replay_detected", approved=False, approval_consumed=True, approval_reference_released=True, blockers=_tuple((*evidence.blockers, "execution_approval_replay_detected")))
        self._consumed.add(key)
        return replace(evidence, approval_consumed=True, approval_reference_released=True)


def consume_execution_approval(evidence: DreamDexExecutionApprovalEvidence, registry: DreamDexExecutionApprovalRegistry) -> DreamDexExecutionApprovalEvidence:
    if not isinstance(evidence, DreamDexExecutionApprovalEvidence) or not isinstance(registry, DreamDexExecutionApprovalRegistry):
        raise TypeError("typed_execution_approval_consumption_required")
    return registry.consume(evidence)


@dataclass(frozen=True, repr=False)
class DreamDexPostApprovalRevalidationResult:
    schema_version: str
    revalidation_status: str
    original_binding_fingerprint: str
    current_binding_fingerprint: str
    binding_match: bool
    approval_not_expired: bool
    approval_not_consumed: bool
    journal_unchanged: bool
    intent_unchanged: bool
    reservation_unchanged: bool
    signing_lease_unchanged: bool
    market_evidence_unchanged: bool
    account_evidence_unchanged: bool
    risk_evidence_unchanged: bool
    fair_play_evidence_unchanged: bool
    nonce_still_valid: bool
    fees_still_within_cap: bool
    target_code_still_valid: bool
    RPC_chain_still_valid: bool
    revalidation_fingerprint: str
    authoritative: bool = False
    blockers: tuple[str, ...] = ()
    validation_errors: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "authoritative", False); object.__setattr__(self, "blockers", _tuple(self.blockers)); object.__setattr__(self, "validation_errors", _tuple(self.validation_errors))

    def safe_dict(self) -> dict[str, Any]:
        data = {name: getattr(self, name) for name in self.__dataclass_fields__}
        data["original_binding_fingerprint"] = mask_hex_hash(self.original_binding_fingerprint); data["current_binding_fingerprint"] = mask_hex_hash(self.current_binding_fingerprint); data["revalidation_fingerprint"] = mask_hex_hash(self.revalidation_fingerprint); data["authoritative"] = False
        return data


def revalidate_execution_approval(*, approval: DreamDexExecutionApprovalEvidence, original_preview: DreamDexExecutionApprovalPreview, current_preview: DreamDexExecutionApprovalPreview, now_monotonic_ms: int, journal_unchanged: bool = True, intent_unchanged: bool = True, reservation_unchanged: bool = True, signing_lease_unchanged: bool = True, market_evidence_unchanged: bool = True, account_evidence_unchanged: bool = True, risk_evidence_unchanged: bool = True, fair_play_evidence_unchanged: bool = True, nonce_still_valid: bool = True, fees_still_within_cap: bool = True, target_code_still_valid: bool = True, RPC_chain_still_valid: bool = True) -> DreamDexPostApprovalRevalidationResult:
    binding_match = approval.approval_binding_fingerprint == current_preview.approval_binding_fingerprint == original_preview.approval_binding_fingerprint
    current = int(now_monotonic_ms) <= approval.approval_expires_monotonic_ms
    checks = ((binding_match, "execution_approval_binding_mismatch"), (current, "execution_approval_expired"), (not approval.approval_consumed, "execution_approval_replay_detected"), (journal_unchanged, "post_approval_journal_changed"), (intent_unchanged, "post_approval_revalidation_failed"), (reservation_unchanged, "post_approval_revalidation_failed"), (signing_lease_unchanged, "post_approval_revalidation_failed"), (market_evidence_unchanged, "post_approval_market_evidence_changed"), (account_evidence_unchanged, "post_approval_account_evidence_changed"), (risk_evidence_unchanged, "post_approval_revalidation_failed"), (fair_play_evidence_unchanged, "post_approval_revalidation_failed"), (nonce_still_valid, "post_approval_nonce_changed"), (fees_still_within_cap, "post_approval_fees_changed"), (target_code_still_valid, "post_approval_revalidation_failed"), (RPC_chain_still_valid, "post_approval_revalidation_failed"))
    blockers = _tuple(reason for passed, reason in checks if not passed)
    payload = {"binding": current_preview.approval_binding_fingerprint, "checks": [passed for passed, _ in checks]}
    return DreamDexPostApprovalRevalidationResult(SCHEMA_VERSION, "confirmed" if not blockers else "failed", original_preview.approval_binding_fingerprint, current_preview.approval_binding_fingerprint, binding_match, current, not approval.approval_consumed, journal_unchanged, intent_unchanged, reservation_unchanged, signing_lease_unchanged, market_evidence_unchanged, account_evidence_unchanged, risk_evidence_unchanged, fair_play_evidence_unchanged, nonce_still_valid, fees_still_within_cap, target_code_still_valid, RPC_chain_still_valid, _fp(payload, "dreamdex/post-approval-revalidation"), False, blockers)


@dataclass(frozen=True, repr=False)
class DreamDexExecutionCeremonyResult:
    schema_version: str
    ceremony_status: str
    readiness_decision: Any
    approval_preview: DreamDexExecutionApprovalPreview | None
    approval_evidence: DreamDexExecutionApprovalEvidence | None
    revalidation_result: DreamDexPostApprovalRevalidationResult | None
    preview_execution_performed: bool
    approval_prompt_performed: bool
    approval_granted: bool
    post_approval_revalidation_performed: bool
    approval_consumed: bool
    signer_invocation_performed: bool
    submission_call_performed: bool
    production_network_used: bool
    production_secret_used: bool
    ready_for_signer_invocation: bool
    ready_for_real_submission: bool
    ceremony_fingerprint: str
    authoritative: bool = False
    blockers: tuple[str, ...] = ()
    validation_errors: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "authoritative", False); object.__setattr__(self, "signer_invocation_performed", False); object.__setattr__(self, "submission_call_performed", False); object.__setattr__(self, "production_network_used", False); object.__setattr__(self, "production_secret_used", False); object.__setattr__(self, "ready_for_signer_invocation", False); object.__setattr__(self, "ready_for_real_submission", False); object.__setattr__(self, "blockers", _tuple(self.blockers)); object.__setattr__(self, "validation_errors", _tuple(self.validation_errors))

    def safe_dict(self) -> dict[str, Any]:
        return {"schema_version": self.schema_version, "ceremony_status": self.ceremony_status, "approval_preview": self.approval_preview.safe_dict() if self.approval_preview else None, "approval_evidence": self.approval_evidence.safe_dict() if self.approval_evidence else None, "revalidation_result": self.revalidation_result.safe_dict() if self.revalidation_result else None, "preview_execution_performed": self.preview_execution_performed, "approval_prompt_performed": self.approval_prompt_performed, "approval_granted": self.approval_granted, "post_approval_revalidation_performed": self.post_approval_revalidation_performed, "approval_consumed": self.approval_consumed, "signer_invocation_performed": False, "submission_call_performed": False, "production_network_used": False, "production_secret_used": False, "ready_for_signer_invocation": False, "ready_for_real_submission": False, "ceremony_fingerprint": mask_hex_hash(self.ceremony_fingerprint), "authoritative": False, "blockers": self.blockers, "validation_errors": self.validation_errors}

    def __repr__(self) -> str:
        return f"DreamDexExecutionCeremonyResult(status={self.ceremony_status!r}, approved={self.approval_granted!r}, signer=False, submission=False)"


def run_execution_approval_ceremony(*, readiness_decision: Any, preview: DreamDexExecutionApprovalPreview, challenge: DreamDexExecutionApprovalChallenge, provider: DreamDexExecutionApprovalProvider, now_monotonic_ms: int, current_preview: DreamDexExecutionApprovalPreview | None = None, registry: DreamDexExecutionApprovalRegistry | None = None) -> DreamDexExecutionCeremonyResult:
    evidence = evaluate_execution_approval(challenge=challenge, preview=preview, provider=provider, now_monotonic_ms=now_monotonic_ms)
    revalidation = revalidate_execution_approval(approval=evidence, original_preview=preview, current_preview=current_preview or preview, now_monotonic_ms=now_monotonic_ms) if evidence.approved else None
    good = evidence.approved and revalidation is not None and revalidation.revalidation_status == "confirmed"
    if good:
        evidence = consume_execution_approval(evidence, registry or DreamDexExecutionApprovalRegistry())
    blockers = () if good else _tuple((*(evidence.blockers if evidence else ()), *((revalidation.blockers) if revalidation else ())))
    fp = _fp({"approval": evidence.approval_fingerprint, "revalidation": revalidation.revalidation_fingerprint if revalidation else None}, "dreamdex/approval-ceremony")
    return DreamDexExecutionCeremonyResult(SCHEMA_VERSION, "synthetic_approved" if good else "blocked", readiness_decision, preview, evidence, revalidation, True, evidence.provider_invoked, evidence.approved, revalidation is not None, evidence.approval_consumed, False, False, False, False, False, False, fp, False, blockers)


def serialize_execution_approval_diagnostics(value: DreamDexExecutionApprovalPreview | DreamDexExecutionApprovalChallenge | DreamDexExecutionApprovalEvidence | DreamDexPostApprovalRevalidationResult | DreamDexExecutionCeremonyResult | None = None) -> dict[str, Any]:
    if value is None:
        return {"approval_model": "available_offline", "approval_execution_performed": False, "challenge_issued": False, "real_submission_allowed": False}
    if not isinstance(value, (DreamDexExecutionApprovalPreview, DreamDexExecutionApprovalChallenge, DreamDexExecutionApprovalEvidence, DreamDexPostApprovalRevalidationResult, DreamDexExecutionCeremonyResult)):
        raise TypeError("unsupported_execution_approval_diagnostics_type")
    return value.safe_dict()


__all__ = [name for name in globals() if name.startswith("DreamDex") or name.startswith("build_") or name.startswith("create_") or name.startswith("evaluate_") or name.startswith("revalidate_") or name.startswith("run_") or name.startswith("consume_") or name.startswith("serialize_")]
