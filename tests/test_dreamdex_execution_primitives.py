from dataclasses import FrozenInstanceError

import pytest

from bot.execution.dreamdex_execution_primitives import (
    AUDIT_FINDINGS,
    DreamDexExecutionBlockers,
    DreamDexExecutionCapability,
    ExecutionAvailability,
    build_execution_architecture_audit_report,
    build_execution_capability_matrix,
    canonical_json_bytes,
    deterministic_fingerprint,
    ensure_no_raw_sensitive_fields,
    mask_evm_address,
    mask_hex_hash,
    sha256_hex,
    validate_evm_address,
    validate_tx_hash,
    validate_uint,
)


def test_status_vocabulary_and_matrix_are_deterministic():
    assert ExecutionAvailability.AVAILABLE_OFFLINE.value == "available_offline"
    first = build_execution_capability_matrix(
        blockers=("transaction_signer_unavailable", "incomplete_account_state")
    )
    second = build_execution_capability_matrix(
        blockers=("incomplete_account_state", "transaction_signer_unavailable")
    )
    assert first.fingerprint == second.fingerprint
    assert first.by_name("sign_transaction").status == "unavailable"
    assert first.by_name("build_unsigned_place").status == "available_offline"
    assert first.by_name("readonly_rpc_protocol").status == "available_offline"
    assert first.by_name("finalize_transaction_envelope").status == "available_offline"
    assert first.by_name("resolve_pending_nonce").status == "partial"
    assert first.by_name("estimate_transaction_gas").status == "partial"
    assert first.by_name("submit_transaction").status == "unavailable"
    assert first.blockers == ("incomplete_account_state", "transaction_signer_unavailable")


def test_blocker_registry_rejects_unknown_and_deduplicates():
    assert DreamDexExecutionBlockers.normalize(
        ("transaction_signer_unavailable", "transaction_signer_unavailable")
    ) == ("transaction_signer_unavailable",)
    with pytest.raises(ValueError):
        DreamDexExecutionBlockers.normalize(("not_a_real_blocker",))


def test_common_validation_and_masking_helpers():
    address = "0x" + "a" * 40
    tx_hash = "0x" + "b" * 64
    assert validate_evm_address(address) == address
    assert validate_tx_hash(tx_hash) == tx_hash
    assert validate_uint(3) == 3
    assert mask_evm_address(address) == "0xaa...aaaa"
    assert mask_hex_hash(tx_hash) == "0xbbbb...bbbb"
    assert canonical_json_bytes({"b": 1, "a": 2}) == b'{"a":2,"b":1}'
    assert sha256_hex(b"x") == sha256_hex(b"x")
    assert deterministic_fingerprint({"x": 1}) == deterministic_fingerprint({"x": 1})
    with pytest.raises(ValueError):
        validate_evm_address("0x123")
    with pytest.raises(ValueError):
        validate_tx_hash("0x" + "0" * 64)
    with pytest.raises(ValueError):
        validate_uint(True)


def test_diagnostics_reject_raw_sensitive_values_but_allow_safe_metadata():
    with pytest.raises(ValueError):
        ensure_no_raw_sensitive_fields({"private_key": "secret"})
    with pytest.raises(ValueError):
        ensure_no_raw_sensitive_fields({"owner_address": "0x" + "1" * 40})
    with pytest.raises(ValueError):
        ensure_no_raw_sensitive_fields({"transaction_hash": "0x" + "2" * 64})
    assert ensure_no_raw_sensitive_fields({"status": "unavailable", "count": 2})["count"] == 2


def test_capability_is_immutable_and_audit_report_is_available():
    capability = DreamDexExecutionCapability("x", "available_offline", "layer")
    with pytest.raises(FrozenInstanceError):
        capability.name = "changed"
    assert build_execution_architecture_audit_report() == AUDIT_FINDINGS
    assert len(AUDIT_FINDINGS) >= 3
