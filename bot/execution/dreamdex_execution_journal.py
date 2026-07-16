"""Durable, local-only execution journal and nonce reservation store.

The journal is deliberately independent from signing, submission and RPC.  It
stores only hashes, normalized identifiers and state transitions; payloads,
calldata, credentials and transaction bytes are never persisted.  Callers must
opt in explicitly by opening a SQLite path.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import json
from pathlib import Path
import re
import sqlite3
import time
from typing import Any, Mapping, Sequence

from bot.execution.dreamdex_execution_primitives import (
    DreamDexExecutionBlockers,
    deterministic_fingerprint,
    mask_evm_address,
    mask_hex_hash,
    validate_evm_address,
    validate_uint,
)

SCHEMA_VERSION = 1
APPLICATION_ID = "dreamdex-paper-trading"
DEFAULT_BUSY_TIMEOUT_MS = 2500
MAX_INTENT_LIMIT = 10000
MAX_RESERVATION_LIMIT = 10000


class JournalState(str, Enum):
    CREATED = "created"
    PREFLIGHT_VALIDATED = "preflight_validated"
    NONCE_RESERVED = "nonce_reserved"
    SIGNING_REVIEW_READY = "signing_review_ready"
    SIGNING_LEASE_ACQUIRED = "signing_lease_acquired"
    SIGNING_STARTED = "signing_started"
    SIGNED = "signed"
    SUBMISSION_STARTED = "submission_started"
    SUBMITTED = "submitted"
    PENDING = "pending"
    CONFIRMED_SUCCESS = "confirmed_success"
    CONFIRMED_REVERTED = "confirmed_reverted"
    CANCELLED_BEFORE_SIGNING = "cancelled_before_signing"
    FAILED_PRE_SUBMISSION = "failed_pre_submission"
    SUBMISSION_UNKNOWN = "submission_unknown"
    RECOVERY_REQUIRED = "recovery_required"
    ABANDONED_MANUAL = "abandoned_manual"


INTENT_STATES = frozenset(item.value for item in JournalState)
PRODUCTION_WRITABLE_STATES = frozenset({
    JournalState.CREATED.value,
    JournalState.PREFLIGHT_VALIDATED.value,
    JournalState.NONCE_RESERVED.value,
    JournalState.SIGNING_REVIEW_READY.value,
    JournalState.SIGNING_LEASE_ACQUIRED.value,
    JournalState.CANCELLED_BEFORE_SIGNING.value,
    JournalState.FAILED_PRE_SUBMISSION.value,
    JournalState.RECOVERY_REQUIRED.value,
})

TRANSITIONS: dict[str, frozenset[str]] = {
    JournalState.CREATED.value: frozenset({"preflight_validated", "cancelled_before_signing", "failed_pre_submission", "recovery_required"}),
    JournalState.PREFLIGHT_VALIDATED.value: frozenset({"nonce_reserved", "cancelled_before_signing", "failed_pre_submission", "recovery_required"}),
    JournalState.NONCE_RESERVED.value: frozenset({"signing_review_ready", "cancelled_before_signing", "recovery_required"}),
    JournalState.SIGNING_REVIEW_READY.value: frozenset({"cancelled_before_signing", "signing_lease_acquired", "recovery_required"}),
    JournalState.SIGNING_LEASE_ACQUIRED.value: frozenset({"cancelled_before_signing", "recovery_required"}),
    JournalState.SIGNING_STARTED.value: frozenset({"signed", "failed_pre_submission", "recovery_required"}),
    JournalState.SIGNED.value: frozenset({"submission_started", "recovery_required"}),
    JournalState.SUBMISSION_STARTED.value: frozenset({"submitted", "submission_unknown", "failed_pre_submission", "recovery_required"}),
    JournalState.SUBMITTED.value: frozenset({"pending", "confirmed_success", "confirmed_reverted", "submission_unknown", "recovery_required"}),
    JournalState.PENDING.value: frozenset({"confirmed_success", "confirmed_reverted", "submission_unknown", "recovery_required"}),
    JournalState.SUBMISSION_UNKNOWN.value: frozenset({"recovery_required", "abandoned_manual"}),
    JournalState.RECOVERY_REQUIRED.value: frozenset({"abandoned_manual"}),
    JournalState.CONFIRMED_SUCCESS.value: frozenset(),
    JournalState.CONFIRMED_REVERTED.value: frozenset(),
    JournalState.CANCELLED_BEFORE_SIGNING.value: frozenset(),
    JournalState.FAILED_PRE_SUBMISSION.value: frozenset(),
    JournalState.ABANDONED_MANUAL.value: frozenset(),
}

RELEASE_REASONS = frozenset({"operator_cancelled", "preflight_invalidated", "shutdown", "manual_review"})


def _now_ms() -> int:
    return int(time.time() * 1000)


def _tuple(values: Sequence[str] | None) -> tuple[str, ...]:
    return tuple(dict.fromkeys(str(value) for value in (values or ()) if str(value)))


def _norm_address(value: str, field: str) -> str:
    return validate_evm_address(value, field=field)  # type: ignore[return-value]


def _safe_repr(value: Any) -> str:
    return f"<{type(value).__name__}>"


def _intent_semantics(*, operation: str, chain_id: int, signer_address: str, target_address: str,
                      request_fingerprint: str, original_envelope_fingerprint: str | None,
                      finalized_envelope_fingerprint: str | None) -> dict[str, Any]:
    return {
        "operation": operation,
        "chain_id": chain_id,
        "signer_address": signer_address,
        "target_address": target_address,
        "request_fingerprint": request_fingerprint,
        "original_envelope_fingerprint": original_envelope_fingerprint,
        "finalized_envelope_fingerprint": finalized_envelope_fingerprint,
    }


@dataclass(frozen=True, repr=False)
class DreamDexExecutionJournalPolicy:
    schema_version: int = SCHEMA_VERSION
    application_id: str = APPLICATION_ID
    busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS
    require_absolute_path: bool = True
    require_existing_parent_directory: bool = True
    enable_wal: bool = True
    synchronous_mode: str = "FULL"
    quick_check_on_open: bool = True
    block_writes_on_recovery_required: bool = True
    allow_nonce_reuse_after_release: bool = False
    maximum_active_intents: int | None = None
    maximum_active_reservations: int | None = None
    authoritative: bool = False
    unresolved_reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError("unsupported_journal_schema_version")
        if not isinstance(self.busy_timeout_ms, int) or isinstance(self.busy_timeout_ms, bool) or self.busy_timeout_ms <= 0 or self.busy_timeout_ms > 60000:
            raise ValueError("busy_timeout_ms_invalid")
        if self.synchronous_mode.upper() not in {"FULL", "NORMAL", "OFF", "EXTRA"}:
            raise ValueError("synchronous_mode_invalid")
        for name in ("maximum_active_intents", "maximum_active_reservations"):
            value = getattr(self, name)
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 1):
                raise ValueError(f"{name}_invalid")
        object.__setattr__(self, "synchronous_mode", self.synchronous_mode.upper())
        object.__setattr__(self, "unresolved_reasons", _tuple(self.unresolved_reasons))
        if self.allow_nonce_reuse_after_release:
            raise ValueError("nonce_reuse_after_release_not_supported")

    def __repr__(self) -> str:
        return f"DreamDexExecutionJournalPolicy(schema_version={self.schema_version!r}, synchronous_mode={self.synchronous_mode!r}, authoritative=False)"

    def safe_dict(self) -> dict[str, Any]:
        return {"schema_version": self.schema_version, "application_id": self.application_id, "busy_timeout_ms": self.busy_timeout_ms, "require_absolute_path": self.require_absolute_path, "require_existing_parent_directory": self.require_existing_parent_directory, "enable_wal": self.enable_wal, "synchronous_mode": self.synchronous_mode, "quick_check_on_open": self.quick_check_on_open, "block_writes_on_recovery_required": self.block_writes_on_recovery_required, "allow_nonce_reuse_after_release": False, "maximum_active_intents": self.maximum_active_intents, "maximum_active_reservations": self.maximum_active_reservations, "authoritative": False, "unresolved_reasons": self.unresolved_reasons}


@dataclass(frozen=True, repr=False)
class DreamDexExecutionIntent:
    schema_version: int
    intent_id: str
    operation: str
    chain_id: int
    signer_address: str
    target_address: str
    request_fingerprint: str
    original_envelope_fingerprint: str | None
    finalized_envelope_fingerprint: str | None
    preflight_fingerprint: str | None
    signing_request_fingerprint: str | None
    order_identity_status: str
    created_source: str
    intent_fingerprint: str
    authoritative: bool
    blockers: tuple[str, ...] = ()
    unresolved_reasons: tuple[str, ...] = ()
    state: str = JournalState.CREATED.value
    created_at_unix_ms: int = 0
    updated_at_unix_ms: int = 0

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError("unsupported_journal_schema_version")
        if not self.intent_id or not self.intent_fingerprint:
            raise ValueError("intent_fingerprint_required")
        if self.chain_id < 0 or self.chain_id > (1 << 256) - 1:
            raise ValueError("chain_id_invalid")
        object.__setattr__(self, "signer_address", _norm_address(self.signer_address, "signer_address"))
        object.__setattr__(self, "target_address", _norm_address(self.target_address, "target_address"))
        if self.state not in INTENT_STATES:
            raise ValueError("intent_state_invalid")
        object.__setattr__(self, "blockers", _tuple(self.blockers))
        object.__setattr__(self, "unresolved_reasons", _tuple(self.unresolved_reasons))

    @classmethod
    def build(cls, *, operation: str, chain_id: int, signer_address: str, target_address: str,
              request_fingerprint: str, original_envelope_fingerprint: str | None = None,
              finalized_envelope_fingerprint: str | None = None, preflight_fingerprint: str | None = None,
              signing_request_fingerprint: str | None = None, order_identity_status: str = "unavailable",
              created_source: str = "test_fixture", authoritative: bool = False,
              blockers: Sequence[str] = (), unresolved_reasons: Sequence[str] = ()) -> "DreamDexExecutionIntent":
        signer_n = _norm_address(signer_address, "signer_address")
        target_n = _norm_address(target_address, "target_address")
        semantics = _intent_semantics(operation=operation, chain_id=chain_id, signer_address=signer_n, target_address=target_n, request_fingerprint=request_fingerprint, original_envelope_fingerprint=original_envelope_fingerprint, finalized_envelope_fingerprint=finalized_envelope_fingerprint)
        intent_id = deterministic_fingerprint(semantics, domain="dreamdex_execution_intent_id")
        intent_fp = deterministic_fingerprint({**semantics, "preflight_fingerprint": preflight_fingerprint, "signing_request_fingerprint": signing_request_fingerprint, "order_identity_status": order_identity_status}, domain="dreamdex_execution_intent")
        return cls(SCHEMA_VERSION, intent_id, operation, chain_id, signer_n, target_n, request_fingerprint, original_envelope_fingerprint, finalized_envelope_fingerprint, preflight_fingerprint, signing_request_fingerprint, order_identity_status, created_source, intent_fp, authoritative, _tuple(blockers), _tuple(unresolved_reasons))

    def __repr__(self) -> str:
        return f"DreamDexExecutionIntent(intent_id={mask_hex_hash(self.intent_id)!r}, operation={self.operation!r}, signer={mask_evm_address(self.signer_address)!r}, target={mask_evm_address(self.target_address)!r}, state={self.state!r})"

    def safe_dict(self) -> dict[str, Any]:
        return {"schema_version": self.schema_version, "intent_id": mask_hex_hash(self.intent_id), "operation": self.operation, "chain_id": self.chain_id, "signer_address_masked": mask_evm_address(self.signer_address), "target_address_masked": mask_evm_address(self.target_address), "request_fingerprint": mask_hex_hash(self.request_fingerprint), "original_envelope_fingerprint": mask_hex_hash(self.original_envelope_fingerprint), "finalized_envelope_fingerprint": mask_hex_hash(self.finalized_envelope_fingerprint), "preflight_fingerprint": mask_hex_hash(self.preflight_fingerprint), "signing_request_fingerprint": mask_hex_hash(self.signing_request_fingerprint), "order_identity_status": self.order_identity_status, "created_source": self.created_source, "intent_fingerprint": mask_hex_hash(self.intent_fingerprint), "state": self.state, "authoritative": False, "blockers": self.blockers, "unresolved_reasons": self.unresolved_reasons}


@dataclass(frozen=True, repr=False)
class DreamDexNonceReservation:
    schema_version: int
    reservation_id: str
    intent_id: str
    chain_id: int
    signer_address: str
    nonce: int
    reservation_status: str
    reserved_at_unix_ms: int
    released_at_unix_ms: int | None
    release_reason: str | None
    reservation_fingerprint: str
    authoritative: bool
    blockers: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        validate_uint(self.nonce, field="nonce")
        object.__setattr__(self, "signer_address", _norm_address(self.signer_address, "signer_address"))
        object.__setattr__(self, "blockers", _tuple(self.blockers))

    def __repr__(self) -> str:
        return f"DreamDexNonceReservation(reservation_id={mask_hex_hash(self.reservation_id)!r}, intent_id={mask_hex_hash(self.intent_id)!r}, signer={mask_evm_address(self.signer_address)!r}, nonce={self.nonce!r}, status={self.reservation_status!r})"

    def safe_dict(self) -> dict[str, Any]:
        return {"schema_version": self.schema_version, "reservation_id": mask_hex_hash(self.reservation_id), "intent_id": mask_hex_hash(self.intent_id), "chain_id": self.chain_id, "signer_address_masked": mask_evm_address(self.signer_address), "nonce": self.nonce, "reservation_status": self.reservation_status, "released_at_present": self.released_at_unix_ms is not None, "release_reason": self.release_reason, "reservation_fingerprint": mask_hex_hash(self.reservation_fingerprint), "authoritative": False, "blockers": self.blockers}


@dataclass(frozen=True, repr=False)
class DreamDexExecutionJournalEvent:
    schema_version: int
    event_id: str
    intent_id: str
    reservation_id: str | None
    event_type: str
    previous_state: str | None
    new_state: str
    event_sequence: int
    event_fingerprint: str
    created_at_unix_ms: int
    source_type: str
    authoritative: bool
    details_status: str
    blockers: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.new_state not in INTENT_STATES:
            raise ValueError("event_state_invalid")
        if self.event_sequence < 1:
            raise ValueError("event_sequence_invalid")
        object.__setattr__(self, "blockers", _tuple(self.blockers))

    def __repr__(self) -> str:
        return f"DreamDexExecutionJournalEvent(event_id={mask_hex_hash(self.event_id)!r}, intent_id={mask_hex_hash(self.intent_id)!r}, event_type={self.event_type!r}, sequence={self.event_sequence!r})"

    def safe_dict(self) -> dict[str, Any]:
        return {"schema_version": self.schema_version, "event_id": mask_hex_hash(self.event_id), "intent_id": mask_hex_hash(self.intent_id), "reservation_id": mask_hex_hash(self.reservation_id), "event_type": self.event_type, "previous_state": self.previous_state, "new_state": self.new_state, "event_sequence": self.event_sequence, "event_fingerprint": mask_hex_hash(self.event_fingerprint), "source_type": self.source_type, "authoritative": False, "details_status": self.details_status, "blockers": self.blockers}


@dataclass(frozen=True, repr=False)
class DreamDexExecutionJournalSnapshot:
    schema_version: int | None
    journal_status: str
    schema_status: str
    integrity_status: str
    intent_count: int
    active_intent_count: int
    reservation_count: int
    active_reservation_count: int
    unknown_state_count: int
    conflicted_intent_count: int
    recovery_required: bool
    safe_to_create_intent: bool
    safe_to_reserve_nonce: bool
    snapshot_fingerprint: str
    blockers: tuple[str, ...] = ()
    unresolved_reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "blockers", _tuple(self.blockers))
        object.__setattr__(self, "unresolved_reasons", _tuple(self.unresolved_reasons))

    def __repr__(self) -> str:
        return f"DreamDexExecutionJournalSnapshot(status={self.journal_status!r}, schema={self.schema_status!r}, integrity={self.integrity_status!r}, recovery_required={self.recovery_required!r})"

    def safe_dict(self) -> dict[str, Any]:
        return {"schema_version": self.schema_version, "journal_status": self.journal_status, "schema_status": self.schema_status, "integrity_status": self.integrity_status, "intent_count": self.intent_count, "active_intent_count": self.active_intent_count, "reservation_count": self.reservation_count, "active_reservation_count": self.active_reservation_count, "unknown_state_count": self.unknown_state_count, "conflicted_intent_count": self.conflicted_intent_count, "recovery_required": self.recovery_required, "safe_to_create_intent": self.safe_to_create_intent, "safe_to_reserve_nonce": self.safe_to_reserve_nonce, "snapshot_fingerprint": mask_hex_hash(self.snapshot_fingerprint), "blockers": self.blockers, "unresolved_reasons": self.unresolved_reasons}


@dataclass(frozen=True, repr=False)
class DreamDexExecutionIntentResult:
    intent: DreamDexExecutionIntent | None
    existing_intent_reused: bool
    conflict_detected: bool
    blockers: tuple[str, ...] = ()
    validation_errors: tuple[str, ...] = ()

    def __repr__(self) -> str:
        return f"DreamDexExecutionIntentResult(intent_id={mask_hex_hash(self.intent.intent_id) if self.intent else '<missing>'!r}, existing_intent_reused={self.existing_intent_reused!r}, conflict_detected={self.conflict_detected!r})"

    def safe_dict(self) -> dict[str, Any]:
        return {"intent": self.intent.safe_dict() if self.intent else None, "existing_intent_reused": self.existing_intent_reused, "conflict_detected": self.conflict_detected, "blockers": self.blockers, "validation_errors": self.validation_errors}


@dataclass(frozen=True, repr=False)
class DreamDexNonceReservationResult:
    status: str
    intent_id: str
    reservation_id: str | None
    requested_nonce: int | None
    reservation_created: bool
    existing_reservation_reused: bool
    conflict_detected: bool
    snapshot_only_nonce: bool
    nonce_revalidation_required: bool
    reservation_fingerprint: str | None
    blockers: tuple[str, ...] = ()
    validation_errors: tuple[str, ...] = ()

    def __repr__(self) -> str:
        return f"DreamDexNonceReservationResult(status={self.status!r}, intent_id={mask_hex_hash(self.intent_id)!r}, reservation_created={self.reservation_created!r}, conflict_detected={self.conflict_detected!r})"

    def safe_dict(self) -> dict[str, Any]:
        return {"status": self.status, "intent_id": mask_hex_hash(self.intent_id), "reservation_id": mask_hex_hash(self.reservation_id), "requested_nonce": self.requested_nonce, "reservation_created": self.reservation_created, "existing_reservation_reused": self.existing_reservation_reused, "conflict_detected": self.conflict_detected, "snapshot_only_nonce": self.snapshot_only_nonce, "nonce_revalidation_required": self.nonce_revalidation_required, "reservation_fingerprint": mask_hex_hash(self.reservation_fingerprint), "blockers": self.blockers, "validation_errors": self.validation_errors}


@dataclass(frozen=True)
class _JournalConfig:
    path: Path
    policy: DreamDexExecutionJournalPolicy


SCHEMA_SQL = (
    "CREATE TABLE IF NOT EXISTS journal_schema (schema_version INTEGER NOT NULL, created_at INTEGER NOT NULL, application_id TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS execution_intents (intent_id TEXT PRIMARY KEY, operation TEXT NOT NULL, chain_id INTEGER NOT NULL, signer_address_normalized TEXT NOT NULL, target_address_normalized TEXT NOT NULL, request_fingerprint TEXT NOT NULL, original_envelope_fingerprint TEXT, finalized_envelope_fingerprint TEXT, preflight_fingerprint TEXT, signing_request_fingerprint TEXT, order_identity_status TEXT NOT NULL, created_source TEXT NOT NULL, state TEXT NOT NULL, intent_fingerprint TEXT NOT NULL UNIQUE, authoritative INTEGER NOT NULL, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, blockers_json TEXT NOT NULL, unresolved_reasons_json TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS nonce_reservations (reservation_id TEXT PRIMARY KEY, intent_id TEXT NOT NULL UNIQUE, chain_id INTEGER NOT NULL, signer_address_normalized TEXT NOT NULL, nonce INTEGER NOT NULL, status TEXT NOT NULL, reservation_fingerprint TEXT NOT NULL UNIQUE, authoritative INTEGER NOT NULL, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, released_at INTEGER, release_reason TEXT, blockers_json TEXT NOT NULL, FOREIGN KEY(intent_id) REFERENCES execution_intents(intent_id))",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_nonce_reservation_chain_signer_nonce ON nonce_reservations(chain_id, signer_address_normalized, nonce)",
    "CREATE TABLE IF NOT EXISTS journal_events (event_id TEXT PRIMARY KEY, intent_id TEXT NOT NULL, reservation_id TEXT, event_sequence INTEGER NOT NULL, event_type TEXT NOT NULL, previous_state TEXT, new_state TEXT NOT NULL, event_fingerprint TEXT NOT NULL UNIQUE, created_at INTEGER NOT NULL, source_type TEXT NOT NULL, authoritative INTEGER NOT NULL, details_status TEXT NOT NULL, blockers_json TEXT NOT NULL, UNIQUE(intent_id, event_sequence), FOREIGN KEY(intent_id) REFERENCES execution_intents(intent_id), FOREIGN KEY(reservation_id) REFERENCES nonce_reservations(reservation_id))",
    "CREATE INDEX IF NOT EXISTS idx_intents_state ON execution_intents(state)",
    "CREATE INDEX IF NOT EXISTS idx_events_intent ON journal_events(intent_id, event_sequence)",
)

REQUIRED_COLUMNS = {
    "journal_schema": {"schema_version", "created_at", "application_id"},
    "execution_intents": {"intent_id", "operation", "chain_id", "signer_address_normalized", "target_address_normalized", "request_fingerprint", "created_source", "state", "intent_fingerprint", "authoritative", "created_at", "updated_at"},
    "nonce_reservations": {"reservation_id", "intent_id", "chain_id", "signer_address_normalized", "nonce", "status", "reservation_fingerprint", "created_at", "updated_at"},
    "journal_events": {"event_id", "intent_id", "event_sequence", "event_type", "previous_state", "new_state", "event_fingerprint", "created_at"},
}
REQUIRED_INDEXES = {
    "uq_nonce_reservation_chain_signer_nonce",
    "idx_intents_state",
    "idx_events_intent",
}


class DreamDexExecutionJournal:
    """Context-managed SQLite repository; raw connections are never exposed."""

    def __init__(self, path: str | Path, policy: DreamDexExecutionJournalPolicy | None = None, *, mode: str = "rw") -> None:
        self.config = _JournalConfig(Path(path), policy or DreamDexExecutionJournalPolicy())
        self.mode = mode
        self._conn: sqlite3.Connection | None = None
        self._schema_status = "unavailable"
        self._integrity_status = "unavailable"
        self._recovery_reasons: tuple[str, ...] = ()

    @property
    def path(self) -> Path:
        return self.config.path

    @property
    def policy(self) -> DreamDexExecutionJournalPolicy:
        return self.config.policy

    def initialize(self) -> "DreamDexExecutionJournal":
        return self.__enter__()

    def __enter__(self) -> "DreamDexExecutionJournal":
        if self._conn is not None:
            return self
        path = self.path
        if self.policy.require_absolute_path and not path.is_absolute():
            raise ValueError("journal_path_must_be_absolute")
        if any(part.lower() == "vendor" for part in path.parts):
            raise ValueError("journal_path_vendor_forbidden")
        if not path.parent.exists():
            if self.policy.require_existing_parent_directory:
                raise FileNotFoundError("journal_parent_directory_missing")
            path.parent.mkdir(parents=True, exist_ok=True)
        if self.mode not in {"rw", "ro"}:
            raise ValueError("journal_mode_invalid")
        uri = False
        target = str(path)
        if self.mode == "ro":
            target = f"file:{path.as_posix()}?mode=ro"
            uri = True
        self._conn = sqlite3.connect(target, timeout=self.policy.busy_timeout_ms / 1000, isolation_level=None, uri=uri)
        self._conn.row_factory = sqlite3.Row
        self._configure_connection()
        self._inspect_schema(create=self.mode == "rw")
        self._integrity_status, self._recovery_reasons = self._check_integrity()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._conn is not None:
            self._conn.close()
        self._conn = None

    def close(self) -> None:
        self.__exit__(None, None, None)

    def _require_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("journal_not_open")
        return self._conn

    def _configure_connection(self) -> None:
        conn = self._require_conn()
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(f"PRAGMA busy_timeout = {self.policy.busy_timeout_ms}")
        if self.mode == "rw":
            if self.policy.enable_wal:
                conn.execute("PRAGMA journal_mode = WAL")
            conn.execute(f"PRAGMA synchronous = {self.policy.synchronous_mode}")

    def _inspect_schema(self, *, create: bool) -> None:
        conn = self._require_conn()
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not tables and create:
            conn.execute("BEGIN IMMEDIATE")
            try:
                for sql in SCHEMA_SQL:
                    conn.execute(sql)
                conn.execute("INSERT INTO journal_schema(schema_version, created_at, application_id) VALUES (?, ?, ?)", (SCHEMA_VERSION, _now_ms(), self.policy.application_id))
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not REQUIRED_COLUMNS.keys() <= tables:
            self._schema_status = "incompatible"
            return
        version_rows = conn.execute("SELECT schema_version, application_id FROM journal_schema ORDER BY rowid LIMIT 1").fetchall()
        if not version_rows or int(version_rows[0][0]) != SCHEMA_VERSION or str(version_rows[0][1]) != self.policy.application_id:
            self._schema_status = "incompatible"
            return
        for table, required in REQUIRED_COLUMNS.items():
            cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            if not required <= cols:
                self._schema_status = "incompatible"
                return
        self._schema_status = "compatible"

    def _check_integrity(self) -> tuple[str, tuple[str, ...]]:
        if self._schema_status != "compatible":
            return "unavailable", ("execution_journal_schema_incompatible",)
        conn = self._require_conn()
        reasons: list[str] = []
        if self.policy.quick_check_on_open:
            try:
                row = conn.execute("PRAGMA quick_check").fetchone()
                if not row or str(row[0]).lower() != "ok":
                    reasons.append("execution_journal_integrity_failed")
            except sqlite3.DatabaseError:
                reasons.append("execution_journal_integrity_failed")
        try:
            indexes = {str(row[1]) for row in conn.execute("PRAGMA index_list(nonce_reservations)")}
            indexes |= {str(row[1]) for row in conn.execute("PRAGMA index_list(execution_intents)")}
            indexes |= {str(row[1]) for row in conn.execute("PRAGMA index_list(journal_events)")}
            if not REQUIRED_INDEXES <= indexes:
                reasons.append("execution_journal_integrity_failed")
            intent_states = {str(row[0]) for row in conn.execute("SELECT state FROM execution_intents")}
            reasons.extend("unknown_execution_state_blocks_progress" for state in intent_states if state not in INTENT_STATES)
            for row in conn.execute("SELECT intent_id, state FROM execution_intents"):
                latest = conn.execute("SELECT new_state FROM journal_events WHERE intent_id=? ORDER BY event_sequence DESC LIMIT 1", (row[0],)).fetchone()
                if latest is None or str(latest[0]) != str(row[1]):
                    reasons.append("execution_journal_state_event_mismatch")
                if row[1] in {"signing_started", "signed", "submission_started", "submission_unknown", "submitted", "pending"}:
                    reasons.append("execution_journal_recovery_required")
            orphan = conn.execute("SELECT COUNT(*) FROM nonce_reservations r LEFT JOIN execution_intents i ON i.intent_id=r.intent_id WHERE i.intent_id IS NULL").fetchone()[0]
            if orphan:
                reasons.append("execution_journal_recovery_required")
            missing = conn.execute("SELECT COUNT(*) FROM execution_intents i LEFT JOIN nonce_reservations r ON r.intent_id=i.intent_id WHERE i.state='nonce_reserved' AND r.intent_id IS NULL").fetchone()[0]
            if missing:
                reasons.append("execution_journal_recovery_required")
            gap = conn.execute("SELECT COUNT(*) FROM (SELECT intent_id, MAX(event_sequence) AS m, COUNT(*) AS c FROM journal_events GROUP BY intent_id HAVING m != c)").fetchone()[0]
            if gap:
                reasons.append("execution_journal_recovery_required")
        except sqlite3.DatabaseError:
            reasons.append("execution_journal_integrity_failed")
        unique = tuple(dict.fromkeys(reasons))
        return ("passed" if not unique else "failed"), unique

    def _writes_allowed(self) -> tuple[bool, tuple[str, ...]]:
        if self.mode != "rw":
            return False, ("journal_read_only",)
        if self._schema_status != "compatible":
            return False, ("execution_journal_schema_incompatible",)
        if self._integrity_status == "failed":
            return False, self._recovery_reasons or ("execution_journal_integrity_failed",)
        if self.policy.block_writes_on_recovery_required and self._recovery_reasons:
            return False, self._recovery_reasons
        if self.policy.unresolved_reasons:
            return False, self.policy.unresolved_reasons
        if self.policy.maximum_active_intents is None or self.policy.maximum_active_reservations is None:
            return False, ("execution_journal_limits_unresolved",)
        return True, ()

    def _begin(self) -> sqlite3.Connection:
        conn = self._require_conn()
        conn.execute("BEGIN IMMEDIATE")
        return conn

    @staticmethod
    def _json_tuple(values: Sequence[str]) -> str:
        return json.dumps(list(_tuple(values)), separators=(",", ":"))

    @staticmethod
    def _parse_tuple(value: str | None) -> tuple[str, ...]:
        try:
            parsed = json.loads(value or "[]")
            return _tuple(parsed if isinstance(parsed, list) else ())
        except (TypeError, ValueError, json.JSONDecodeError):
            return ()

    def _intent_from_row(self, row: sqlite3.Row) -> DreamDexExecutionIntent:
        return DreamDexExecutionIntent(schema_version=SCHEMA_VERSION, intent_id=row["intent_id"], operation=row["operation"], chain_id=int(row["chain_id"]), signer_address=row["signer_address_normalized"], target_address=row["target_address_normalized"], request_fingerprint=row["request_fingerprint"], original_envelope_fingerprint=row["original_envelope_fingerprint"], finalized_envelope_fingerprint=row["finalized_envelope_fingerprint"], preflight_fingerprint=row["preflight_fingerprint"], signing_request_fingerprint=row["signing_request_fingerprint"], order_identity_status=row["order_identity_status"], created_source=row["created_source"] if "created_source" in row.keys() else "unavailable", intent_fingerprint=row["intent_fingerprint"], authoritative=bool(row["authoritative"]), blockers=self._parse_tuple(row["blockers_json"]), unresolved_reasons=self._parse_tuple(row["unresolved_reasons_json"]), state=row["state"], created_at_unix_ms=int(row["created_at"]), updated_at_unix_ms=int(row["updated_at"]))

    def _reservation_from_row(self, row: sqlite3.Row) -> DreamDexNonceReservation:
        return DreamDexNonceReservation(schema_version=SCHEMA_VERSION, reservation_id=row["reservation_id"], intent_id=row["intent_id"], chain_id=int(row["chain_id"]), signer_address=row["signer_address_normalized"], nonce=int(row["nonce"]), reservation_status=row["status"], reserved_at_unix_ms=int(row["created_at"]), released_at_unix_ms=row["released_at"], release_reason=row["release_reason"], reservation_fingerprint=row["reservation_fingerprint"], authoritative=bool(row["authoritative"]), blockers=self._parse_tuple(row["blockers_json"]))

    def _insert_event(self, conn: sqlite3.Connection, *, intent_id: str, reservation_id: str | None, event_type: str, previous_state: str | None, new_state: str, source_type: str, authoritative: bool, details_status: str, blockers: Sequence[str] = ()) -> DreamDexExecutionJournalEvent:
        seq = int(conn.execute("SELECT COALESCE(MAX(event_sequence), 0) + 1 FROM journal_events WHERE intent_id=?", (intent_id,)).fetchone()[0])
        fp = deterministic_fingerprint({"intent_id": intent_id, "reservation_id": reservation_id, "event_type": event_type, "previous_state": previous_state, "new_state": new_state, "event_sequence": seq, "source_type": source_type}, domain="dreamdex_execution_journal_event")
        event_id = deterministic_fingerprint({"event_fingerprint": fp}, domain="dreamdex_execution_journal_event_id")
        created = _now_ms()
        conn.execute("INSERT INTO journal_events(event_id,intent_id,reservation_id,event_sequence,event_type,previous_state,new_state,event_fingerprint,created_at,source_type,authoritative,details_status,blockers_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (event_id, intent_id, reservation_id, seq, event_type, previous_state, new_state, fp, created, source_type, int(bool(authoritative)), details_status, self._json_tuple(blockers)))
        return DreamDexExecutionJournalEvent(SCHEMA_VERSION, event_id, intent_id, reservation_id, event_type, previous_state, new_state, seq, fp, created, source_type, bool(authoritative), details_status, _tuple(blockers))

    def create_or_get_execution_intent(self, *, operation: str, chain_id: int, signer_address: str, target_address: str, request_fingerprint: str, original_envelope_fingerprint: str | None = None, finalized_envelope_fingerprint: str | None = None, preflight_fingerprint: str | None = None, signing_request_fingerprint: str | None = None, order_identity_status: str = "unavailable", created_source: str = "test_fixture", authoritative: bool = False, blockers: Sequence[str] = (), unresolved_reasons: Sequence[str] = ()) -> DreamDexExecutionIntentResult:
        allowed, reasons = self._writes_allowed()
        if not allowed:
            return DreamDexExecutionIntentResult(None, False, False, reasons, reasons)
        signer = _norm_address(signer_address, "signer_address")
        target = _norm_address(target_address, "target_address")
        if isinstance(chain_id, bool) or not isinstance(chain_id, int) or chain_id < 0:
            raise ValueError("chain_id_invalid")
        if not request_fingerprint:
            raise ValueError("request_fingerprint_required")
        semantics = _intent_semantics(operation=operation, chain_id=chain_id, signer_address=signer, target_address=target, request_fingerprint=request_fingerprint, original_envelope_fingerprint=original_envelope_fingerprint, finalized_envelope_fingerprint=finalized_envelope_fingerprint)
        intent_id = deterministic_fingerprint(semantics, domain="dreamdex_execution_intent_id")
        intent_fp = deterministic_fingerprint({**semantics, "preflight_fingerprint": preflight_fingerprint, "signing_request_fingerprint": signing_request_fingerprint, "order_identity_status": order_identity_status}, domain="dreamdex_execution_intent")
        try:
            conn = self._begin()
        except sqlite3.OperationalError:
            return DreamDexExecutionIntentResult(None, False, False, ("execution_journal_unavailable",), ("journal_database_locked",))
        now = _now_ms()
        try:
            existing = conn.execute("SELECT * FROM execution_intents WHERE intent_id=?", (intent_id,)).fetchone()
            if existing is not None:
                if existing["intent_fingerprint"] != intent_fp:
                    conn.rollback()
                    return DreamDexExecutionIntentResult(None, False, True, ("execution_intent_conflict",), ("intent_fingerprint_conflict",))
                conn.commit()
                return DreamDexExecutionIntentResult(self._intent_from_row(existing), True, False)
            # A request fingerprint is the semantic idempotency boundary.  A
            # changed finalized envelope for the same request is ambiguous and
            # must not silently become a replacement intent.
            request_conflict = conn.execute(
                "SELECT * FROM execution_intents WHERE operation=? AND chain_id=? AND signer_address_normalized=? AND target_address_normalized=? AND request_fingerprint=? LIMIT 1",
                (operation, chain_id, signer, target, request_fingerprint),
            ).fetchone()
            if request_conflict is not None:
                conn.rollback()
                return DreamDexExecutionIntentResult(None, False, True, ("execution_intent_conflict",), ("request_finalized_envelope_conflict",))
            active_count = int(conn.execute("SELECT COUNT(*) FROM execution_intents WHERE state NOT IN ('cancelled_before_signing','failed_pre_submission','confirmed_success','confirmed_reverted','abandoned_manual')").fetchone()[0])
            if self.policy.maximum_active_intents is not None and active_count >= self.policy.maximum_active_intents:
                conn.rollback()
                return DreamDexExecutionIntentResult(None, False, False, ("execution_intent_limit_exceeded",), ("execution_intent_limit_exceeded",))
            intent = DreamDexExecutionIntent(SCHEMA_VERSION, intent_id, operation, chain_id, signer, target, request_fingerprint, original_envelope_fingerprint, finalized_envelope_fingerprint, preflight_fingerprint, signing_request_fingerprint, order_identity_status, created_source, intent_fp, authoritative, _tuple(blockers), _tuple(unresolved_reasons), JournalState.CREATED.value, now, now)
            conn.execute("INSERT INTO execution_intents(intent_id,operation,chain_id,signer_address_normalized,target_address_normalized,request_fingerprint,original_envelope_fingerprint,finalized_envelope_fingerprint,preflight_fingerprint,signing_request_fingerprint,order_identity_status,created_source,state,intent_fingerprint,authoritative,created_at,updated_at,blockers_json,unresolved_reasons_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (intent.intent_id, intent.operation, intent.chain_id, intent.signer_address, intent.target_address, intent.request_fingerprint, intent.original_envelope_fingerprint, intent.finalized_envelope_fingerprint, intent.preflight_fingerprint, intent.signing_request_fingerprint, intent.order_identity_status, intent.created_source, intent.state, intent.intent_fingerprint, int(intent.authoritative), now, now, self._json_tuple(intent.blockers), self._json_tuple(intent.unresolved_reasons)))
            self._insert_event(conn, intent_id=intent.intent_id, reservation_id=None, event_type="intent_created", previous_state=None, new_state=JournalState.CREATED.value, source_type=created_source, authoritative=authoritative, details_status="not_stored", blockers=intent.blockers)
            conn.commit()
            return DreamDexExecutionIntentResult(intent, False, False)
        except Exception:
            conn.rollback()
            raise

    def get_execution_intent(self, intent_id: str) -> DreamDexExecutionIntent | None:
        row = self._require_conn().execute("SELECT * FROM execution_intents WHERE intent_id=?", (intent_id,)).fetchone()
        return self._intent_from_row(row) if row else None

    def create_or_get_intent(self, intent: DreamDexExecutionIntent) -> DreamDexExecutionIntentResult:
        """Compatibility helper accepting the immutable domain model."""
        if not isinstance(intent, DreamDexExecutionIntent):
            raise TypeError("intent must be DreamDexExecutionIntent")
        expected_id = deterministic_fingerprint(
            _intent_semantics(operation=intent.operation, chain_id=intent.chain_id,
                              signer_address=intent.signer_address, target_address=intent.target_address,
                              request_fingerprint=intent.request_fingerprint,
                              original_envelope_fingerprint=intent.original_envelope_fingerprint,
                              finalized_envelope_fingerprint=intent.finalized_envelope_fingerprint),
            domain="dreamdex_execution_intent_id",
        )
        if intent.intent_id != expected_id:
            return DreamDexExecutionIntentResult(None, False, True, ("execution_intent_conflict",), ("intent_id_not_deterministic",))
        return self.create_or_get_execution_intent(
            operation=intent.operation, chain_id=intent.chain_id,
            signer_address=intent.signer_address, target_address=intent.target_address,
            request_fingerprint=intent.request_fingerprint,
            original_envelope_fingerprint=intent.original_envelope_fingerprint,
            finalized_envelope_fingerprint=intent.finalized_envelope_fingerprint,
            preflight_fingerprint=intent.preflight_fingerprint,
            signing_request_fingerprint=intent.signing_request_fingerprint,
            order_identity_status=intent.order_identity_status,
            created_source=intent.created_source, authoritative=intent.authoritative,
            blockers=intent.blockers, unresolved_reasons=intent.unresolved_reasons,
        )

    def get_events(self, intent_id: str) -> tuple[DreamDexExecutionJournalEvent, ...]:
        rows = self._require_conn().execute("SELECT * FROM journal_events WHERE intent_id=? ORDER BY event_sequence", (intent_id,)).fetchall()
        return tuple(DreamDexExecutionJournalEvent(SCHEMA_VERSION, row["event_id"], row["intent_id"], row["reservation_id"], row["event_type"], row["previous_state"], row["new_state"], int(row["event_sequence"]), row["event_fingerprint"], int(row["created_at"]), row["source_type"], bool(row["authoritative"]), row["details_status"], self._parse_tuple(row["blockers_json"])) for row in rows)

    def transition_execution_intent(self, intent_id: str, new_state: str, *, event_type: str | None = None, source_type: str = "test_fixture", details_status: str = "not_stored", blockers: Sequence[str] = ()) -> DreamDexExecutionIntentResult:
        if new_state not in INTENT_STATES:
            return DreamDexExecutionIntentResult(None, False, False, ("invalid_state_transition",), ("unknown_state",))
        # Post-signing states remain in the schema for future lifecycle
        # integration, but this local journal has no signer/submission path.
        if new_state not in PRODUCTION_WRITABLE_STATES:
            return DreamDexExecutionIntentResult(None, False, False, ("execution_journal_post_signing_unavailable",), ("post_signing_transition_unavailable",))
        allowed, reasons = self._writes_allowed()
        if not allowed:
            return DreamDexExecutionIntentResult(None, False, False, reasons, reasons)
        try:
            conn = self._begin()
        except sqlite3.OperationalError:
            return DreamDexExecutionIntentResult(None, False, False, ("execution_journal_unavailable",), ("journal_database_locked",))
        try:
            row = conn.execute("SELECT * FROM execution_intents WHERE intent_id=?", (intent_id,)).fetchone()
            if row is None:
                conn.rollback()
                return DreamDexExecutionIntentResult(None, False, False, ("execution_intent_not_found",), ("execution_intent_not_found",))
            old = row["state"]
            if new_state not in TRANSITIONS.get(old, frozenset()):
                conn.rollback()
                return DreamDexExecutionIntentResult(self._intent_from_row(row), False, False, ("invalid_state_transition",), (f"transition_{old}_to_{new_state}_not_allowed",))
            now = _now_ms()
            conn.execute("UPDATE execution_intents SET state=?, updated_at=? WHERE intent_id=?", (new_state, now, intent_id))
            self._insert_event(conn, intent_id=intent_id, reservation_id=None, event_type=event_type or f"state_{new_state}", previous_state=old, new_state=new_state, source_type=source_type, authoritative=bool(row["authoritative"]), details_status=details_status, blockers=blockers)
            conn.commit()
            return DreamDexExecutionIntentResult(self.get_execution_intent(intent_id), False, False)
        except Exception:
            conn.rollback()
            raise

    def reserve_nonce(self, *, intent_id: str, nonce: int, chain_id: int | None = None, signer_address: str | None = None, finalized_envelope_fingerprint: str | None = None, preflight_policy_compliant: bool = True, ready_for_signing_policy_review: bool = True, nonce_revalidation_required: bool = True, pending_nonce_source_status: str = "source_confirmed", authoritative: bool = False) -> DreamDexNonceReservationResult:
        if isinstance(nonce, bool) or not isinstance(nonce, int) or nonce < 0 or nonce > (1 << 256) - 1:
            return DreamDexNonceReservationResult("rejected", intent_id, None, None, False, False, False, True, nonce_revalidation_required, None, ("nonce_invalid",), ("nonce_invalid",))
        if not nonce_revalidation_required:
            return DreamDexNonceReservationResult("rejected", intent_id, None, nonce, False, False, False, True, False, None, ("pending_nonce_snapshot_requires_revalidation",), ("nonce_revalidation_required",))
        if pending_nonce_source_status != "source_confirmed":
            return DreamDexNonceReservationResult("rejected", intent_id, None, nonce, False, False, False, True, True, None, ("pending_nonce_unavailable",), ("pending_nonce_source_unconfirmed",))
        allowed, reasons = self._writes_allowed()
        if not allowed:
            return DreamDexNonceReservationResult("blocked", intent_id, None, nonce, False, False, False, True, nonce_revalidation_required, None, reasons, reasons)
        try:
            conn = self._begin()
        except sqlite3.OperationalError:
            return DreamDexNonceReservationResult("blocked", intent_id, None, nonce, False, False, False, True, nonce_revalidation_required, None, ("execution_journal_unavailable",), ("journal_database_locked",))
        try:
            row = conn.execute("SELECT * FROM execution_intents WHERE intent_id=?", (intent_id,)).fetchone()
            if row is None:
                conn.rollback(); return DreamDexNonceReservationResult("rejected", intent_id, None, nonce, False, False, False, True, nonce_revalidation_required, None, ("execution_intent_not_found",), ("execution_intent_not_found",))
            if row["state"] != JournalState.PREFLIGHT_VALIDATED.value:
                conn.rollback(); return DreamDexNonceReservationResult("rejected", intent_id, None, nonce, False, False, False, True, nonce_revalidation_required, None, ("nonce_reservation_requires_preflight_validated",), ("intent_state_invalid",))
            if chain_id is not None and int(chain_id) != int(row["chain_id"]):
                conn.rollback(); return DreamDexNonceReservationResult("rejected", intent_id, None, nonce, False, False, False, True, nonce_revalidation_required, None, ("nonce_chain_mismatch",), ("chain_mismatch",))
            if signer_address is not None and _norm_address(signer_address, "signer_address") != row["signer_address_normalized"]:
                conn.rollback(); return DreamDexNonceReservationResult("rejected", intent_id, None, nonce, False, False, False, True, nonce_revalidation_required, None, ("nonce_signer_mismatch",), ("signer_mismatch",))
            if not row["finalized_envelope_fingerprint"] or finalized_envelope_fingerprint is None or finalized_envelope_fingerprint != row["finalized_envelope_fingerprint"]:
                conn.rollback(); return DreamDexNonceReservationResult("rejected", intent_id, None, nonce, False, False, False, True, nonce_revalidation_required, None, ("nonce_envelope_mismatch",), ("envelope_mismatch",))
            if not preflight_policy_compliant or not ready_for_signing_policy_review:
                conn.rollback(); return DreamDexNonceReservationResult("rejected", intent_id, None, nonce, False, False, False, True, nonce_revalidation_required, None, ("transaction_signing_policy_rejected",), ("preflight_not_compliant",))
            existing_for_intent = conn.execute("SELECT * FROM nonce_reservations WHERE intent_id=?", (intent_id,)).fetchone()
            if existing_for_intent:
                conn.commit(); return DreamDexNonceReservationResult("existing", intent_id, existing_for_intent["reservation_id"], nonce, False, True, False, True, True, existing_for_intent["reservation_fingerprint"], self._parse_tuple(existing_for_intent["blockers_json"]), ())
            conflict = conn.execute("SELECT * FROM nonce_reservations WHERE chain_id=? AND signer_address_normalized=? AND nonce=?", (row["chain_id"], row["signer_address_normalized"], nonce)).fetchone()
            if conflict:
                conn.rollback(); return DreamDexNonceReservationResult("conflict", intent_id, None, nonce, False, False, True, True, nonce_revalidation_required, None, ("nonce_reservation_conflict",), ("nonce_already_reserved",))
            active = int(conn.execute("SELECT COUNT(*) FROM nonce_reservations WHERE status='reserved'").fetchone()[0])
            if self.policy.maximum_active_reservations is not None and active >= self.policy.maximum_active_reservations:
                conn.rollback(); return DreamDexNonceReservationResult("rejected", intent_id, None, nonce, False, False, False, True, nonce_revalidation_required, None, ("nonce_reservation_limit_exceeded",), ("nonce_reservation_limit_exceeded",))
            reservation_id = deterministic_fingerprint({"intent_id": intent_id, "nonce": nonce}, domain="dreamdex_nonce_reservation_id")
            reservation_fp = deterministic_fingerprint({"intent_id": intent_id, "chain_id": int(row["chain_id"]), "signer_address": row["signer_address_normalized"], "nonce": nonce}, domain="dreamdex_nonce_reservation")
            now = _now_ms()
            blockers = ("pending_nonce_snapshot_requires_revalidation",) if nonce_revalidation_required else ()
            conn.execute("INSERT INTO nonce_reservations(reservation_id,intent_id,chain_id,signer_address_normalized,nonce,status,reservation_fingerprint,authoritative,created_at,updated_at,released_at,release_reason,blockers_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (reservation_id, intent_id, row["chain_id"], row["signer_address_normalized"], nonce, "reserved", reservation_fp, int(bool(authoritative)), now, now, None, None, self._json_tuple(blockers)))
            self._insert_event(conn, intent_id=intent_id, reservation_id=reservation_id, event_type="nonce_reserved", previous_state=row["state"], new_state=JournalState.NONCE_RESERVED.value, source_type="local_journal", authoritative=authoritative, details_status="not_stored", blockers=blockers)
            conn.execute("UPDATE execution_intents SET state=?, updated_at=? WHERE intent_id=?", (JournalState.NONCE_RESERVED.value, now, intent_id))
            conn.commit()
            return DreamDexNonceReservationResult("reserved", intent_id, reservation_id, nonce, True, False, False, True, nonce_revalidation_required, reservation_fp, blockers, ())
        except sqlite3.IntegrityError:
            conn.rollback(); return DreamDexNonceReservationResult("conflict", intent_id, None, nonce, False, False, True, True, nonce_revalidation_required, None, ("nonce_reservation_conflict",), ("nonce_already_reserved",))
        except Exception:
            conn.rollback(); raise

    def acquire_signing_lease(self, *, intent_id: str, reservation_id: str, signer_address: str, chain_id: int, finalized_envelope_fingerprint: str, signing_request_fingerprint: str, maximum_active_leases_per_signer: int = 1) -> DreamDexExecutionJournalEvent | None:
        """Atomically transition a reviewed intent and persist the lease event.

        The event id is the lease id.  No separate lease table is needed: the
        intent state and event are committed under the same writer lock.
        """
        allowed, reasons = self._writes_allowed()
        if not allowed:
            return None
        if isinstance(maximum_active_leases_per_signer, bool) or not isinstance(maximum_active_leases_per_signer, int) or maximum_active_leases_per_signer < 1:
            return None
        try:
            chain = validate_uint(chain_id, field="chain_id")
            signer = _norm_address(signer_address, "signer_address")
        except ValueError:
            return None
        try:
            conn = self._begin()
        except sqlite3.OperationalError:
            return None
        try:
            intent = conn.execute("SELECT * FROM execution_intents WHERE intent_id=?", (intent_id,)).fetchone()
            reservation = conn.execute("SELECT * FROM nonce_reservations WHERE reservation_id=?", (reservation_id,)).fetchone()
            if intent is None or reservation is None or intent["state"] != JournalState.SIGNING_REVIEW_READY.value:
                conn.rollback(); return None
            if int(intent["chain_id"]) != int(chain) or intent["signer_address_normalized"] != signer or reservation["intent_id"] != intent_id or reservation["status"] != "reserved":
                conn.rollback(); return None
            if intent["finalized_envelope_fingerprint"] != finalized_envelope_fingerprint:
                conn.rollback(); return None
            active = conn.execute("SELECT COUNT(*) FROM execution_intents WHERE signer_address_normalized=? AND state=?", (signer, JournalState.SIGNING_LEASE_ACQUIRED.value)).fetchone()[0]
            if int(active) >= maximum_active_leases_per_signer:
                conn.rollback(); return None
            now = _now_ms()
            event = self._insert_event(conn, intent_id=intent_id, reservation_id=reservation_id, event_type="signing_lease_acquired", previous_state=JournalState.SIGNING_REVIEW_READY.value, new_state=JournalState.SIGNING_LEASE_ACQUIRED.value, source_type="local_journal", authoritative=False, details_status="fingerprints_only", blockers=("pending_nonce_snapshot_requires_revalidation",))
            conn.execute("UPDATE execution_intents SET state=?, updated_at=? WHERE intent_id=?", (JournalState.SIGNING_LEASE_ACQUIRED.value, now, intent_id))
            conn.commit()
            return event
        except Exception:
            conn.rollback(); raise

    def get_nonce_reservation(self, reservation_id: str | None = None, *, intent_id: str | None = None) -> DreamDexNonceReservation | None:
        if reservation_id is None and intent_id is None:
            raise ValueError("reservation_identifier_required")
        if reservation_id is not None:
            row = self._require_conn().execute("SELECT * FROM nonce_reservations WHERE reservation_id=?", (reservation_id,)).fetchone()
        else:
            row = self._require_conn().execute("SELECT * FROM nonce_reservations WHERE intent_id=?", (intent_id,)).fetchone()
        return self._reservation_from_row(row) if row else None

    def release_nonce_before_signing(self, *, intent_id: str, reason: str) -> DreamDexNonceReservationResult:
        if reason not in RELEASE_REASONS:
            return DreamDexNonceReservationResult("rejected", intent_id, None, None, False, False, False, True, True, None, ("release_reason_invalid",), ("release_reason_invalid",))
        allowed, reasons = self._writes_allowed()
        if not allowed:
            return DreamDexNonceReservationResult("blocked", intent_id, None, None, False, False, False, True, True, None, reasons, reasons)
        try:
            conn = self._begin()
        except sqlite3.OperationalError:
            return DreamDexNonceReservationResult("blocked", intent_id, None, None, False, False, False, True, True, None, ("execution_journal_unavailable",), ("journal_database_locked",))
        try:
            intent = conn.execute("SELECT * FROM execution_intents WHERE intent_id=?", (intent_id,)).fetchone()
            reservation = conn.execute("SELECT * FROM nonce_reservations WHERE intent_id=?", (intent_id,)).fetchone()
            if intent is None or reservation is None:
                conn.rollback(); return DreamDexNonceReservationResult("rejected", intent_id, None, None, False, False, False, True, True, None, ("nonce_reservation_not_found",), ("nonce_reservation_not_found",))
            if intent["state"] not in {JournalState.PREFLIGHT_VALIDATED.value, JournalState.NONCE_RESERVED.value, JournalState.SIGNING_REVIEW_READY.value, JournalState.SIGNING_LEASE_ACQUIRED.value} or reservation["status"] != "reserved":
                conn.rollback(); return DreamDexNonceReservationResult("rejected", intent_id, reservation["reservation_id"], reservation["nonce"], False, False, False, True, True, reservation["reservation_fingerprint"], ("nonce_release_after_signing_forbidden",), ("release_state_invalid",))
            now = _now_ms()
            conn.execute("UPDATE nonce_reservations SET status='released_before_signing', updated_at=?, released_at=?, release_reason=? WHERE reservation_id=?", (now, now, reason, reservation["reservation_id"]))
            conn.execute("UPDATE execution_intents SET state='cancelled_before_signing', updated_at=? WHERE intent_id=?", (now, intent_id))
            self._insert_event(conn, intent_id=intent_id, reservation_id=reservation["reservation_id"], event_type="nonce_released", previous_state=intent["state"], new_state="cancelled_before_signing", source_type="local_journal", authoritative=False, details_status="reason_enum_only")
            conn.commit()
            return DreamDexNonceReservationResult("released", intent_id, reservation["reservation_id"], reservation["nonce"], False, False, False, True, True, reservation["reservation_fingerprint"], (), ())
        except Exception:
            conn.rollback(); raise

    def build_execution_journal_snapshot(self) -> DreamDexExecutionJournalSnapshot:
        if self._schema_status != "compatible":
            blockers = ("execution_journal_schema_incompatible",)
            return DreamDexExecutionJournalSnapshot(None, "incompatible", self._schema_status, self._integrity_status, 0, 0, 0, 0, 0, 0, True, False, False, deterministic_fingerprint({"status": "incompatible", "path": str(self.path.name)}, domain="journal_snapshot"), blockers, blockers)
        conn = self._require_conn()
        intent_count = int(conn.execute("SELECT COUNT(*) FROM execution_intents").fetchone()[0])
        active_states = ("created", "preflight_validated", "nonce_reserved", "signing_review_ready", "signing_lease_acquired", "signing_started", "signed", "submission_started", "submitted", "pending", "submission_unknown", "recovery_required")
        active_count = int(conn.execute("SELECT COUNT(*) FROM execution_intents WHERE state IN (%s)" % ",".join("?" * len(active_states)), active_states).fetchone()[0])
        reservation_count = int(conn.execute("SELECT COUNT(*) FROM nonce_reservations").fetchone()[0])
        active_reservation_count = int(conn.execute("SELECT COUNT(*) FROM nonce_reservations WHERE status='reserved'").fetchone()[0])
        unknown = int(conn.execute("SELECT COUNT(*) FROM execution_intents WHERE state IN ('submission_unknown','recovery_required') OR state NOT IN (%s)" % ",".join("?" * len(INTENT_STATES)), tuple(INTENT_STATES)).fetchone()[0])
        conflicts = 0
        recovery = bool(self._recovery_reasons)
        blockers = list(self._recovery_reasons)
        if self.policy.maximum_active_intents is None or self.policy.maximum_active_reservations is None:
            blockers.append("execution_journal_limits_unresolved")
        if self.policy.unresolved_reasons:
            blockers.extend(self.policy.unresolved_reasons)
        if not self.policy.authoritative:
            # local journaling is useful for safety, but never account/execution authority
            pass
        blockers = tuple(dict.fromkeys(blockers))
        safe_create = self._integrity_status == "passed" and not recovery and self.mode == "rw" and not self.policy.unresolved_reasons and self.policy.maximum_active_intents is not None and self.policy.maximum_active_reservations is not None and active_count < self.policy.maximum_active_intents
        safe_reserve = safe_create and self.mode == "rw" and self.policy.maximum_active_reservations is not None and active_reservation_count < self.policy.maximum_active_reservations
        payload = {"schema_version": SCHEMA_VERSION, "journal_status": "open", "schema_status": self._schema_status, "integrity_status": self._integrity_status, "intent_count": intent_count, "active_intent_count": active_count, "reservation_count": reservation_count, "active_reservation_count": active_reservation_count, "unknown_state_count": unknown, "recovery_required": recovery, "blockers": blockers}
        return DreamDexExecutionJournalSnapshot(SCHEMA_VERSION, "open", self._schema_status, self._integrity_status, intent_count, active_count, reservation_count, active_reservation_count, unknown, conflicts, recovery, safe_create, safe_reserve, deterministic_fingerprint(payload, domain="journal_snapshot"), blockers, self.policy.unresolved_reasons)

    def verify_execution_journal_integrity(self) -> DreamDexExecutionJournalSnapshot:
        return self.build_execution_journal_snapshot()

    def serialize_execution_journal_diagnostics(self) -> dict[str, Any]:
        snap = self.build_execution_journal_snapshot()
        return {"schema_version": snap.schema_version, "journal_status": snap.journal_status, "schema_status": snap.schema_status, "integrity_status": snap.integrity_status, "intent_count": snap.intent_count, "active_intent_count": snap.active_intent_count, "reservation_count": snap.reservation_count, "active_reservation_count": snap.active_reservation_count, "unknown_state_count": snap.unknown_state_count, "conflicted_intent_count": snap.conflicted_intent_count, "recovery_required": snap.recovery_required, "safe_to_create_intent": snap.safe_to_create_intent, "safe_to_reserve_nonce": snap.safe_to_reserve_nonce, "snapshot_fingerprint": mask_hex_hash(snap.snapshot_fingerprint), "journal_path_output_allowed": False, "addresses_output_allowed": False, "raw_payload_output_allowed": False, "blockers": snap.blockers, "unresolved_reasons": snap.unresolved_reasons}


def initialize_journal(path: str | Path, policy: DreamDexExecutionJournalPolicy | None = None) -> DreamDexExecutionJournal:
    """Initialize and return an opened journal.  Parent creation is policy-controlled."""
    journal = DreamDexExecutionJournal(path, policy or DreamDexExecutionJournalPolicy(), mode="rw")
    journal.__enter__()
    return journal


def open_journal(path: str | Path, policy: DreamDexExecutionJournalPolicy | None = None, *, mode: str = "rw") -> DreamDexExecutionJournal:
    journal = DreamDexExecutionJournal(path, policy or DreamDexExecutionJournalPolicy(), mode=mode)
    journal.__enter__()
    return journal


def build_execution_journal_snapshot(journal: DreamDexExecutionJournal) -> DreamDexExecutionJournalSnapshot:
    return journal.build_execution_journal_snapshot()


def create_or_get_execution_intent(journal: DreamDexExecutionJournal, **kwargs: Any) -> DreamDexExecutionIntentResult:
    return journal.create_or_get_execution_intent(**kwargs)


def transition_execution_intent(journal: DreamDexExecutionJournal, intent_id: str, new_state: str, **kwargs: Any) -> DreamDexExecutionIntentResult:
    return journal.transition_execution_intent(intent_id, new_state, **kwargs)


def reserve_nonce(journal: DreamDexExecutionJournal, **kwargs: Any) -> DreamDexNonceReservationResult:
    return journal.reserve_nonce(**kwargs)


def release_nonce_before_signing(journal: DreamDexExecutionJournal, **kwargs: Any) -> DreamDexNonceReservationResult:
    return journal.release_nonce_before_signing(**kwargs)


def verify_execution_journal_integrity(journal: DreamDexExecutionJournal) -> DreamDexExecutionJournalSnapshot:
    return journal.verify_execution_journal_integrity()


def serialize_execution_journal_diagnostics(journal: DreamDexExecutionJournal) -> dict[str, Any]:
    return journal.serialize_execution_journal_diagnostics()


DreamDexExecutionJournalRepository = DreamDexExecutionJournal


__all__ = [
    "SCHEMA_VERSION", "JournalState", "DreamDexExecutionJournalPolicy", "DreamDexExecutionIntent", "DreamDexNonceReservation", "DreamDexExecutionJournalEvent", "DreamDexExecutionJournalSnapshot", "DreamDexExecutionIntentResult", "DreamDexNonceReservationResult", "DreamDexExecutionJournal", "DreamDexExecutionJournalRepository", "initialize_journal", "open_journal", "create_or_get_execution_intent", "transition_execution_intent", "reserve_nonce", "release_nonce_before_signing", "build_execution_journal_snapshot", "verify_execution_journal_integrity", "serialize_execution_journal_diagnostics", "TRANSITIONS", "RELEASE_REASONS",
]
