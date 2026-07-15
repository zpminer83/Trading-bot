from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest

from bot.integrations.dreamdex_auth_models import DreamDexAuthManager, FixtureDreamDexAuthTransport
from bot.integrations.dreamdex_external_signer_client import (
    PROTOCOL,
    DreamDexExternalSiweSignerClient,
    ExternalSignerProcessLauncher,
    build_production_external_siwe_signer_from_env,
)


FIXTURE = Path(__file__).parent / "fixtures" / "fake_external_siwe_signer.py"
OWNER = "0x1a642f0e3c3af545e7acbd38b07251b3990914f1"
NOW = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)


def launcher(mode="valid", **kwargs):
    return ExternalSignerProcessLauncher([sys.executable, str(FIXTURE), mode], **kwargs)


def auth_fixture():
    return {"nonce_response": {"nonce": "ipc-nonce"}, "login_response": {"token": "ipc-token", "expiresAt": int((NOW + timedelta(hours=1)).timestamp() * 1000)}}


def test_production_external_factory_is_unavailable_and_does_not_read_path_or_secrets():
    class Trap(dict):
        def __getitem__(self, key):
            raise AssertionError(key)
        def get(self, key, default=None):
            raise AssertionError(key)

    signer = build_production_external_siwe_signer_from_env(Trap())
    assert not signer.configured
    assert signer.status == "unavailable"
    assert signer.executable == "<unresolved>"
    assert signer.protocol_status == "unavailable"


def test_describe_then_exact_sign_uses_only_allowed_capability_and_closes_process():
    client = DreamDexExternalSiweSignerClient(launcher(), owner_address=OWNER)
    assert client.get_address() == OWNER
    assert client.capabilities == frozenset({"siwe_login_message"})
    signature = client.sign_message("exact message")
    assert signature.startswith("0x")
    assert client.diagnostics.describe_performed and client.diagnostics.sign_performed
    assert client.diagnostics.exit_status == "clean"
    assert client.diagnostics.address_match == "confirmed"
    assert client.diagnostics.environment_isolated == "confirmed"
    assert client.diagnostics.message_integrity == "confirmed"
    assert client.diagnostics.signature_verification == "unavailable"


@pytest.mark.parametrize("mode", ["wrong_protocol", "wrong_capability", "address_mismatch", "wrong_request_id", "malformed_json", "extra_stdout"])
def test_protocol_or_signature_faults_fail_closed(mode):
    client = DreamDexExternalSiweSignerClient(launcher(mode), owner_address=OWNER)
    with pytest.raises(RuntimeError):
        client.get_address() if mode in {"wrong_protocol", "wrong_capability", "address_mismatch", "malformed_json", "extra_stdout"} else (client.get_address(), client.sign_message("x"))


def test_timeout_is_bounded_and_child_is_killed():
    client = DreamDexExternalSiweSignerClient(launcher("timeout", request_timeout_seconds=0.1, startup_timeout_seconds=0.1), owner_address=OWNER)
    with pytest.raises(RuntimeError, match="describe_failed"):
        client.get_address()
    assert client.launcher.process is None
    assert client.diagnostics.exit_status == "killed_timeout"


def test_nonzero_exit_status_is_safe_diagnostic_only():
    client = DreamDexExternalSiweSignerClient(launcher("nonzero_exit"), owner_address=OWNER)
    with pytest.raises(RuntimeError):
        client.get_address()
    assert client.diagnostics.exit_status == "nonzero_exit"


def test_address_mismatch_does_not_confirm_identity():
    client = DreamDexExternalSiweSignerClient(launcher("address_mismatch"), owner_address=OWNER)
    with pytest.raises(RuntimeError):
        client.get_address()
    assert client.diagnostics.address_match == "mismatch"
    assert client.diagnostics.signature_verification == "unavailable"


def test_minimal_environment_does_not_forward_parent_secret():
    l = launcher("secret_environment_probe", environment={"DREAMDEX_READ_ONLY_BEARER_TOKEN": "fake-secret"})
    client = DreamDexExternalSiweSignerClient(l, owner_address=OWNER)
    assert client.get_address() == OWNER
    client.close()
    assert "DREAMDEX_READ_ONLY_BEARER_TOKEN" not in l.environment


def test_external_client_integrates_with_auth_manager_and_invalid_signature_blocks_login():
    transport = FixtureDreamDexAuthTransport(auth_fixture(), now=NOW)
    client = DreamDexExternalSiweSignerClient(launcher(), owner_address=OWNER)
    manager = DreamDexAuthManager(signer=client, transport=transport, owner_address=OWNER, clock=lambda: NOW)
    result = manager.authenticate()
    assert result.state == "authenticated"
    assert transport.nonce_calls == 1 and transport.login_calls == 1
    assert result.signature_verification.status == "valid"
    safe = result.safe_dict()
    assert safe["external_signer_exit_status"] == "clean"
    assert safe["external_signer_address_match"] == "confirmed"
    assert safe["external_signer_environment_isolated"] == "confirmed"
    assert safe["external_signer_message_integrity"] == "confirmed"
    assert safe["external_signer_signature_verification"] == "valid"
    assert not result.signature_verification.authoritative_for_dreamdex_wallet_binding

    bad_transport = FixtureDreamDexAuthTransport(auth_fixture(), now=NOW)
    bad_client = DreamDexExternalSiweSignerClient(launcher("invalid_signature"), owner_address=OWNER)
    bad = DreamDexAuthManager(signer=bad_client, transport=bad_transport, owner_address=OWNER, clock=lambda: NOW).authenticate()
    assert bad.state == "failed_closed"
    assert bad_transport.login_calls == 0
    assert bad.signature_verification.status == "invalid_format"
    assert bad_client.diagnostics.signature_verification == "invalid"


def test_message_mutation_is_not_confirmed():
    transport = FixtureDreamDexAuthTransport(auth_fixture(), now=NOW)
    client = DreamDexExternalSiweSignerClient(launcher("message_mutation"), owner_address=OWNER)
    result = DreamDexAuthManager(signer=client, transport=transport, owner_address=OWNER, clock=lambda: NOW).authenticate()
    assert result.state == "failed_closed"
    assert transport.login_calls == 0
    assert client.diagnostics.message_integrity == "mismatch"
    assert client.diagnostics.signature_verification == "invalid"


def test_external_managed_flow_preserves_single_flight():
    transport = FixtureDreamDexAuthTransport(auth_fixture(), now=NOW)
    client = DreamDexExternalSiweSignerClient(launcher(), owner_address=OWNER)
    manager = DreamDexAuthManager(signer=client, transport=transport, owner_address=OWNER, clock=lambda: NOW)
    with ThreadPoolExecutor(max_workers=4) as pool:
        states = list(pool.map(lambda _: manager.ensure_authenticated().state, range(4)))
    assert states == ["authenticated"] * 4
    assert transport.nonce_calls == 1 and transport.login_calls == 1
