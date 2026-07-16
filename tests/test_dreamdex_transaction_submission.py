from pathlib import Path
import sqlite3

import pytest

from bot.execution.dreamdex_execution_journal import (
    DreamDexExecutionJournalPolicy,
    JournalState,
    initialize_journal,
    migrate_journal_v1_to_v2,
    open_journal,
)
from bot.execution.dreamdex_readonly_rpc import DreamDexRpcError
from bot.execution.dreamdex_signed_transaction import run_transaction_signing_session
from bot.execution.dreamdex_transaction_submission import (
    DreamDexRawTransactionSubmissionResponse,
    DreamDexTransactionSubmissionPolicy,
    DreamDexRawTransactionHttpSubmitter,
    build_transaction_submission_preview,
    recover_transaction_submission,
    run_transaction_submission_session,
)
from test_dreamdex_signed_transaction import _TestOnlySigner, _prepared


WRITE_POLICY = DreamDexExecutionJournalPolicy(maximum_active_intents=20, maximum_active_reservations=20)


class _Submitter:
    def __init__(self, response=None, error=None, on_call=None):
        self.response = response
        self.error = error
        self.calls = 0
        self.on_call = on_call

    def submit_raw_transaction(self, ephemeral):
        self.calls += 1
        if self.on_call:
            self.on_call()
        if self.error:
            raise self.error
        return self.response


def _signed(path: Path):
    journal, material, envelope, request, policy = _prepared(path)
    signing = run_transaction_signing_session(journal=journal, material=material, signer=_TestOnlySigner())
    assert signing.artifact is not None
    ephemeral = _TestOnlySigner().sign_finalized_transaction(material)
    return journal, material, signing.artifact, ephemeral


def test_exact_hash_is_persisted_before_one_send_and_repeated_call_is_idempotent(tmp_path):
    journal, material, artifact, ephemeral = _signed(tmp_path / "success.sqlite")
    submitter = _Submitter()
    submitter.response = DreamDexRawTransactionSubmissionResponse("accepted", None, artifact.signed_transaction_hash, None, "dispatched", True, "response_received", "fixture", False)
    observed = []
    submitter.on_call = lambda: observed.append(journal.get_transaction_submission(intent_id=material.intent_id))
    result = run_transaction_submission_session(journal=journal, material=material, artifact=artifact, ephemeral_signed_transaction=ephemeral, submitter=submitter, policy=DreamDexTransactionSubmissionPolicy())
    assert result.status == "submitted"
    assert submitter.calls == 1
    assert observed and observed[0]["signed_transaction_hash"] == artifact.signed_transaction_hash
    again = run_transaction_submission_session(journal=journal, material=material, artifact=artifact, ephemeral_signed_transaction=ephemeral, submitter=submitter, policy=DreamDexTransactionSubmissionPolicy())
    assert again.status == "existing"
    assert submitter.calls == 1
    assert journal.get_execution_intent(material.intent_id).state == JournalState.SUBMITTED.value
    journal.close()


@pytest.mark.parametrize("error", [DreamDexRpcError("timeout"), ConnectionError("provider unavailable")])
def test_timeout_or_connection_loss_is_unknown_and_never_retried(tmp_path, error):
    journal, material, artifact, ephemeral = _signed(tmp_path / ("unknown-" + type(error).__name__ + ".sqlite"))
    submitter = _Submitter(error=error)
    result = run_transaction_submission_session(journal=journal, material=material, artifact=artifact, ephemeral_signed_transaction=ephemeral, submitter=submitter)
    assert result.submission_unknown is True
    assert result.ready_for_resubmission is False
    assert journal.get_execution_intent(material.intent_id).state == JournalState.SUBMISSION_UNKNOWN.value
    again = run_transaction_submission_session(journal=journal, material=material, artifact=artifact, ephemeral_signed_transaction=ephemeral, submitter=submitter)
    assert again.status == "existing"
    assert submitter.calls == 1
    journal.close()


def test_hash_mismatch_requires_recovery(tmp_path):
    journal, material, artifact, ephemeral = _signed(tmp_path / "mismatch.sqlite")
    other_hash = "0x" + "2" * 64
    submitter = _Submitter(DreamDexRawTransactionSubmissionResponse("accepted", None, other_hash, False, "dispatched", True, "response_received", "fixture", False))
    result = run_transaction_submission_session(journal=journal, material=material, artifact=artifact, ephemeral_signed_transaction=ephemeral, submitter=submitter)
    assert result.status == "submission_hash_conflict"
    assert journal.get_execution_intent(material.intent_id).state == JournalState.RECOVERY_REQUIRED.value
    assert submitter.calls == 1
    journal.close()


def test_recovery_exact_match_and_not_found_do_not_resend(tmp_path):
    journal, material, artifact, ephemeral = _signed(tmp_path / "recovery.sqlite")
    submitter = _Submitter(error=DreamDexRpcError("timeout"))
    first = run_transaction_submission_session(journal=journal, material=material, artifact=artifact, ephemeral_signed_transaction=ephemeral, submitter=submitter)
    assert first.submission_unknown

    class Reader:
        def __init__(self, value): self.value, self.calls = value, 0
        def get_transaction_by_hash(self, tx_hash): self.calls += 1; return self.value

    reader = Reader({"hash": artifact.signed_transaction_hash, "chainId": hex(material.finalized_envelope.chain_id), "from": artifact.signer_address, "nonce": hex(artifact.nonce), "to": artifact.target_address, "value": hex(material.finalized_envelope.value_wei), "gas": hex(material.finalized_envelope.gas_limit), "type": "0x0", "gasPrice": hex(material.finalized_envelope.gas_price_wei), "input": "0x" + material.finalized_envelope.calldata.hex()})
    recovered, evidence = recover_transaction_submission(journal=journal, intent_id=material.intent_id, reader=reader, artifact=artifact, material=material)
    assert recovered.status == "submitted_recovered"
    assert evidence.exact_expected_fields_match is True
    assert reader.calls == 1
    journal.close()


def test_null_recovery_is_not_found_and_second_lookup_is_blocked(tmp_path):
    journal, material, artifact, ephemeral = _signed(tmp_path / "not-found.sqlite")
    run_transaction_submission_session(journal=journal, material=material, artifact=artifact, ephemeral_signed_transaction=ephemeral, submitter=_Submitter(error=DreamDexRpcError("timeout")))

    class Reader:
        calls = 0
        def get_transaction_by_hash(self, tx_hash): self.calls += 1; return None

    reader = Reader()
    result, evidence = recover_transaction_submission(journal=journal, intent_id=material.intent_id, reader=reader, artifact=artifact, material=material)
    assert result.submission_unknown and evidence.transaction_found is False
    again, evidence_again = recover_transaction_submission(journal=journal, intent_id=material.intent_id, reader=reader, artifact=artifact, material=material)
    assert reader.calls == 1
    assert evidence_again.lookup_status == "already_checked"
    assert again.ready_for_resubmission is False
    journal.close()


def test_schema_contains_hash_only_submission_record_and_explicit_migration(tmp_path):
    path = tmp_path / "v1.sqlite"
    legacy_policy = DreamDexExecutionJournalPolicy(schema_version=1, maximum_active_intents=20, maximum_active_reservations=20)
    legacy = initialize_journal(path, legacy_policy)
    assert legacy.build_execution_journal_snapshot().schema_status == "legacy_read_only"
    legacy.close()
    ro = open_journal(path, legacy_policy, mode="ro")
    assert ro.build_execution_journal_snapshot().schema_status == "legacy_read_only"
    ro.close()
    migrated = migrate_journal_v1_to_v2(path, legacy_policy)
    assert migrated.build_execution_journal_snapshot().schema_status == "compatible"
    columns = {row[1] for row in migrated._require_conn().execute("PRAGMA table_info(transaction_submissions)")}
    assert "signed_transaction_hash" in columns
    assert "raw_signed_transaction" not in columns
    assert "calldata" not in columns
    migrated.close()


def test_submission_preview_is_fail_closed_and_transport_has_no_generic_call():
    preview = build_transaction_submission_preview()
    assert preview.submission_execution_performed is False
    assert preview.raw_payload_persisted is False
    assert preview.ready_for_resubmission is False
    assert preview.automatic_retry_allowed is False
    assert not hasattr(DreamDexRawTransactionHttpSubmitter, "call")
