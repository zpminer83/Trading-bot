from dataclasses import FrozenInstanceError, replace
from pathlib import Path
import sqlite3
import threading

import pytest

from bot.execution.dreamdex_execution_journal import (
    DreamDexExecutionJournalPolicy,
    DreamDexExecutionIntent,
    JournalState,
    initialize_journal,
    open_journal,
)
from bot.execution.dreamdex_execution_primitives import build_execution_capability_matrix

OWNER = "0x1111111111111111111111111111111111111111"
TARGET = "0x2222222222222222222222222222222222222222"
WRITE_POLICY = DreamDexExecutionJournalPolicy(maximum_active_intents=100, maximum_active_reservations=100)


def _intent(journal, request="a" * 64, envelope="b" * 64):
    return journal.create_or_get_execution_intent(
        operation="place_order", chain_id=5031, signer_address=OWNER,
        target_address=TARGET, request_fingerprint=request,
        original_envelope_fingerprint=envelope, finalized_envelope_fingerprint="c" * 64,
        preflight_fingerprint="d" * 64, created_source="test_fixture",
    )


def test_schema_pragmas_and_models_are_frozen(tmp_path):
    path = tmp_path / "journal.sqlite"
    journal = initialize_journal(path, WRITE_POLICY)
    snapshot = journal.build_execution_journal_snapshot()
    assert snapshot.schema_status == "compatible"
    assert snapshot.integrity_status == "passed"
    conn = journal._require_conn()  # repository-internal check; raw connection is not public
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2
    with pytest.raises(FrozenInstanceError):
        snapshot.intent_count = 4
    journal.__exit__(None, None, None)


def test_deterministic_idempotent_intent_and_no_duplicate_event(tmp_path):
    journal = initialize_journal(tmp_path / "j.sqlite", WRITE_POLICY)
    first = _intent(journal)
    second = _intent(journal)
    assert first.intent and first.intent.intent_id == second.intent.intent_id
    assert second.existing_intent_reused is True
    assert len(journal.get_events(first.intent.intent_id)) == 1
    conflict = _intent(journal, envelope="e" * 64)
    assert conflict.conflict_detected is True
    assert "execution_intent_conflict" in conflict.blockers
    journal.__exit__(None, None, None)


def test_transitions_validate_order_and_are_atomic(tmp_path):
    journal = initialize_journal(tmp_path / "j.sqlite", WRITE_POLICY)
    intent = _intent(journal).intent
    assert intent
    assert journal.transition_execution_intent(intent.intent_id, "submitted").validation_errors
    assert journal.get_execution_intent(intent.intent_id).state == "created"
    assert journal.transition_execution_intent(intent.intent_id, "preflight_validated").intent.state == "preflight_validated"
    assert journal.transition_execution_intent(intent.intent_id, "nonce_reserved").intent.state == "nonce_reserved"
    assert journal.transition_execution_intent(intent.intent_id, "signing_started").validation_errors
    assert len(journal.get_events(intent.intent_id)) == 3
    journal.__exit__(None, None, None)


def test_nonce_reservation_uniqueness_snapshot_and_release(tmp_path):
    path = tmp_path / "j.sqlite"
    journal = initialize_journal(path, WRITE_POLICY)
    a = _intent(journal, request="1" * 64).intent
    b = _intent(journal, request="2" * 64).intent
    assert a and b
    journal.transition_execution_intent(a.intent_id, "preflight_validated")
    journal.transition_execution_intent(b.intent_id, "preflight_validated")
    first = journal.reserve_nonce(intent_id=a.intent_id, nonce=5, finalized_envelope_fingerprint=a.finalized_envelope_fingerprint)
    assert first.reservation_created and first.snapshot_only_nonce
    assert first.nonce_revalidation_required
    second = journal.reserve_nonce(intent_id=b.intent_id, nonce=5, finalized_envelope_fingerprint=b.finalized_envelope_fingerprint)
    assert second.conflict_detected
    assert journal.get_nonce_reservation(intent_id=a.intent_id).nonce == 5
    released = journal.release_nonce_before_signing(intent_id=a.intent_id, reason="operator_cancelled")
    assert released.status == "released"
    # Historical rows are retained; the same nonce cannot be silently reused.
    again = journal.reserve_nonce(intent_id=b.intent_id, nonce=5, finalized_envelope_fingerprint=b.finalized_envelope_fingerprint)
    assert again.conflict_detected
    assert journal.get_execution_intent(a.intent_id).state == "cancelled_before_signing"
    journal.__exit__(None, None, None)


def test_recovery_unknown_state_and_event_gap_block_writes(tmp_path):
    path = tmp_path / "j.sqlite"
    journal = initialize_journal(path, WRITE_POLICY)
    intent = _intent(journal).intent
    assert intent
    conn = journal._require_conn()
    conn.execute("UPDATE execution_intents SET state='submission_unknown' WHERE intent_id=?", (intent.intent_id,))
    journal.__exit__(None, None, None)
    reopened = open_journal(path, WRITE_POLICY)
    snapshot = reopened.build_execution_journal_snapshot()
    assert snapshot.recovery_required is True
    assert snapshot.safe_to_create_intent is False
    blocked = _intent(reopened, request="f" * 64)
    assert blocked.intent is None
    assert blocked.blockers
    reopened.__exit__(None, None, None)


def test_unknown_schema_and_missing_column_are_incompatible(tmp_path):
    path = tmp_path / "bad.sqlite"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE journal_schema(schema_version INTEGER, created_at INTEGER, application_id TEXT)")
    conn.execute("INSERT INTO journal_schema VALUES(999, 1, 'x')")
    conn.commit(); conn.close()
    journal = open_journal(path, WRITE_POLICY)
    assert journal.build_execution_journal_snapshot().schema_status == "incompatible"
    journal.__exit__(None, None, None)


def test_two_connections_race_for_same_nonce(tmp_path):
    path = tmp_path / "race.sqlite"
    seed = initialize_journal(path, WRITE_POLICY)
    ia = _intent(seed, request="1" * 64).intent
    ib = _intent(seed, request="2" * 64).intent
    seed.transition_execution_intent(ia.intent_id, "preflight_validated")
    seed.transition_execution_intent(ib.intent_id, "preflight_validated")
    seed.__exit__(None, None, None)
    barrier = threading.Barrier(2)
    results = []

    def worker(intent_id):
        j = open_journal(path, WRITE_POLICY)
        barrier.wait()
        fp = "c" * 64
        results.append(j.reserve_nonce(intent_id=intent_id, nonce=9, finalized_envelope_fingerprint=fp).status)
        j.__exit__(None, None, None)

    t1 = threading.Thread(target=worker, args=(ia.intent_id,)); t2 = threading.Thread(target=worker, args=(ib.intent_id,))
    t1.start(); t2.start(); t1.join(); t2.join()
    assert sorted(results) == ["conflict", "reserved"]
    check = open_journal(path, WRITE_POLICY)
    assert check._require_conn().execute("SELECT COUNT(*) FROM nonce_reservations").fetchone()[0] == 1
    check.__exit__(None, None, None)


def test_safe_diagnostics_never_include_path_addresses_or_payload(tmp_path):
    journal = initialize_journal(tmp_path / "private.sqlite", WRITE_POLICY)
    result = _intent(journal)
    diagnostics = journal.serialize_execution_journal_diagnostics()
    text = str(diagnostics)
    assert str(tmp_path) not in text
    assert OWNER not in text
    assert "private.sqlite" not in text
    assert result.intent and "a" * 64 not in repr(result.intent)
    journal.__exit__(None, None, None)


def test_policy_requires_bounded_limits_and_rejects_nonce_reuse():
    with pytest.raises(ValueError):
        DreamDexExecutionJournalPolicy(allow_nonce_reuse_after_release=True)
    assert DreamDexExecutionJournalPolicy(maximum_active_intents=None).maximum_active_intents is None


def test_vendor_path_is_rejected_and_missing_limits_block_writes(tmp_path):
    with pytest.raises(ValueError, match="vendor"):
        initialize_journal(tmp_path / "vendor" / "journal.sqlite", WRITE_POLICY)
    journal = initialize_journal(tmp_path / "bounded.sqlite")
    assert journal.build_execution_journal_snapshot().safe_to_create_intent is False
    blocked = _intent(journal)
    assert blocked.intent is None
    journal.close()


def test_capability_matrix_exposes_local_journal_without_live_execution():
    matrix = build_execution_capability_matrix()
    assert matrix.by_name("execution_journal_model").status == "available_offline"
    assert matrix.by_name("reserve_nonce_locally").status == "available_offline"
    assert matrix.by_name("recover_execution_state").status == "partial"
    assert matrix.by_name("revalidate_nonce_live").status == "unavailable"
    assert matrix.by_name("submit_transaction").status == "unavailable"
