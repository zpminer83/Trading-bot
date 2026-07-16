import json
from pathlib import Path
import shutil
import tempfile

import pytest
from eth_account import Account

from bot.execution.dreamdex_encrypted_keystore_signer import (
    EncryptedKeystoreDreamDexTransactionSigner,
    DreamDexEncryptedKeystorePolicy,
    inspect_encrypted_keystore,
)
from bot.execution.dreamdex_secret_provider import (
    InteractiveDreamDexKeystoreSecretProvider,
    UnavailableDreamDexKeystoreSecretProvider,
    WindowsCredentialManagerDreamDexKeystoreSecretProvider,
)


KEY = bytes.fromhex("11" * 32)
ADDRESS = Account.from_key(KEY).address


@pytest.fixture
def external_tmp():
    path = Path(tempfile.mkdtemp(prefix="dreamdex-keystore-test-"))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _write_keystore(path: Path, password: str = "test-passphrase", **changes):
    value = Account.encrypt(KEY, password)
    value.update(changes)
    path.write_text(json.dumps(value), encoding="utf-8")
    return value


def test_valid_v3_metadata_is_structural_and_redacted(external_tmp):
    path = external_tmp / "wallet.json"
    _write_keystore(path)
    metadata = inspect_encrypted_keystore(path, DreamDexEncryptedKeystorePolicy(expected_signer_address=ADDRESS))
    assert metadata.keystore_status == "valid"
    assert metadata.keystore_version == 3
    assert metadata.public_address_match is True
    safe = metadata.safe_dict()
    assert "test-passphrase" not in repr(metadata)
    assert "ciphertext" not in repr(safe)
    assert str(path) not in repr(safe)
    assert safe["raw_keystore_output_allowed"] is False


def test_malformed_version_unknown_and_duplicate_fields_fail_closed(external_tmp):
    version = external_tmp / "version.json"
    _write_keystore(version, version=2)
    result = inspect_encrypted_keystore(version, DreamDexEncryptedKeystorePolicy(expected_signer_address=ADDRESS))
    assert result.keystore_status == "malformed"
    assert "encrypted_keystore_version_unsupported" in result.blockers

    unknown = external_tmp / "unknown.json"
    _write_keystore(unknown, extraSecret="nope")
    assert "encrypted_keystore_malformed" in inspect_encrypted_keystore(unknown, DreamDexEncryptedKeystorePolicy(expected_signer_address=ADDRESS)).blockers

    duplicate = external_tmp / "duplicate.json"
    duplicate.write_text('{"address":"' + ADDRESS[2:] + '","ADDRESS":"' + ADDRESS[2:] + '","version":3}', encoding="utf-8")
    assert "encrypted_keystore_malformed" in inspect_encrypted_keystore(duplicate, DreamDexEncryptedKeystorePolicy(expected_signer_address=ADDRESS)).blockers


def test_address_mismatch_and_path_safety_are_blocked(external_tmp):
    path = external_tmp / "wallet.json"
    _write_keystore(path)
    mismatch = inspect_encrypted_keystore(path, DreamDexEncryptedKeystorePolicy(expected_signer_address=Account.create().address))
    assert mismatch.public_address_match is False
    assert "encrypted_keystore_public_address_mismatch" in mismatch.blockers
    relative = inspect_encrypted_keystore("wallet.json", DreamDexEncryptedKeystorePolicy(expected_signer_address=ADDRESS))
    assert "encrypted_keystore_path_unsafe" in relative.blockers
    repo_file = inspect_encrypted_keystore(Path(__file__).parents[1] / "bot" / "execution" / "dreamdex_encrypted_keystore_signer.py", DreamDexEncryptedKeystorePolicy(expected_signer_address=ADDRESS))
    assert "encrypted_keystore_path_unsafe" in repo_file.blockers


def test_interactive_unlock_is_single_attempt_and_never_persists_secret(external_tmp):
    path = external_tmp / "wallet.json"
    _write_keystore(path)
    provider = InteractiveDreamDexKeystoreSecretProvider(prompt_fn=lambda _: "test-passphrase")
    signer = EncryptedKeystoreDreamDexTransactionSigner(keystore_path=path, expected_signer_address=ADDRESS, secret_provider=provider)
    result = signer.unlock_check()
    assert result.unlock_status == "verified"
    assert result.derived_address_match is True
    assert result.key_material_received is True
    assert result.key_material_reference_released is True
    assert result.secure_memory_zeroization_guaranteed is False
    assert provider.invocation_count == 1
    assert "test-passphrase" not in repr(result)
    second = signer.unlock_check()
    assert second.unlock_status == "failed"
    assert provider.invocation_count == 1


def test_unavailable_provider_fails_closed_without_invocation(external_tmp):
    path = external_tmp / "wallet.json"
    _write_keystore(path)
    signer = EncryptedKeystoreDreamDexTransactionSigner(keystore_path=path, expected_signer_address=ADDRESS, secret_provider=UnavailableDreamDexKeystoreSecretProvider())
    result = signer.unlock_check()
    assert result.unlock_status == "failed"
    assert result.secret_provider_invoked is False
    assert "keystore_secret_provider_unavailable" in result.blockers


def test_optional_windows_provider_has_no_network_surface():
    provider = WindowsCredentialManagerDreamDexKeystoreSecretProvider()
    assert provider.describe_capabilities().provider_type == "windows_credential_manager"
    assert not hasattr(provider, "send")
    assert not hasattr(provider, "request")


def test_signer_supports_legacy_and_eip1559_through_existing_session(external_tmp):
    # Reuse the existing journal/material fixture; the production signer only
    # supplies the bound signer callback and does not submit or use RPC.
    from test_dreamdex_signed_transaction import _prepared
    from bot.execution.dreamdex_signed_transaction import run_transaction_signing_session

    for tx_type in ("legacy", "eip1559"):
        key_path = external_tmp / f"{tx_type}.json"
        _write_keystore(key_path)
        journal, material, *_ = _prepared(external_tmp / f"{tx_type}.sqlite", transaction_type=tx_type)
        signer = EncryptedKeystoreDreamDexTransactionSigner(
            keystore_path=key_path,
            expected_signer_address=ADDRESS,
            secret_provider=InteractiveDreamDexKeystoreSecretProvider(prompt_fn=lambda _: "test-passphrase"),
        )
        result = run_transaction_signing_session(journal=journal, material=material, signer=signer)
        assert result.status == "signed"
        assert result.verification and result.verification.verified
        assert result.artifact and result.artifact.ready_for_submission is False
        journal.close()
