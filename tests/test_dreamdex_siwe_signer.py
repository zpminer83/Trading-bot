from datetime import datetime, timedelta, timezone

import pytest

from bot.integrations.dreamdex_auth_models import DreamDexAuthManager, FixtureDreamDexAuthTransport
from bot.integrations.dreamdex_siwe_signer import (
    FixtureSiweMessageSigner,
    SIWE_LOGIN_CAPABILITY,
    build_production_siwe_signer_from_env,
    resolve_auth_mode,
)


ADDRESS = "0x1234567890abcdef1234567890abcdef12345678"
OTHER = "0xabcdefabcdefabcdefabcdefabcdefabcdefabcd"
NOW = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)


def fixture():
    return {
        "nonce_response": {"nonce": "offline-nonce"},
        "login_response": {"token": "offline-token", "expiresAt": int((NOW + timedelta(hours=1)).timestamp() * 1000)},
    }


def test_production_factory_is_unavailable_and_does_not_read_environment():
    class SecretTrap(dict):
        def __getitem__(self, key):
            raise AssertionError(f"secret read: {key}")
        def get(self, key, default=None):
            if key not in {"DREAMDEX_READ_ONLY_OWNER_ADDRESS"}:
                raise AssertionError(f"unexpected read: {key}")
            return default

    signer = build_production_siwe_signer_from_env(SecretTrap())
    assert signer.configured is False
    assert signer.status == "unavailable"
    assert signer.capabilities == frozenset()
    with pytest.raises(RuntimeError, match="signer_unavailable"):
        signer.sign_message("x")


def test_auth_modes_are_explicit_and_conflicts_do_not_choose_a_transport():
    assert resolve_auth_mode(manual_bearer_configured=False, managed_siwe_configured=False) == "none"
    assert resolve_auth_mode(manual_bearer_configured=True, managed_siwe_configured=False) == "manual_bearer_read_only"
    assert resolve_auth_mode(manual_bearer_configured=False, managed_siwe_configured=True) == "managed_siwe"
    assert resolve_auth_mode(manual_bearer_configured=True, managed_siwe_configured=True) == "conflicting_configuration"


def test_fixture_signer_exact_message_call_count_and_capability():
    signer = FixtureSiweMessageSigner(ADDRESS, expected_message="exact")
    assert signer.get_address() == ADDRESS
    assert signer.capabilities == frozenset({SIWE_LOGIN_CAPABILITY})
    assert signer.sign_message("exact").startswith("0x")
    assert signer.call_count == 1
    with pytest.raises(ValueError, match="unexpected_siwe_message"):
        signer.sign_message("different")


def test_managed_flow_binds_address_and_passes_identical_message():
    transport = FixtureDreamDexAuthTransport(fixture(), now=NOW)
    signer = FixtureSiweMessageSigner(ADDRESS)
    manager = DreamDexAuthManager(signer=signer, transport=transport, owner_address=ADDRESS, clock=lambda: NOW)
    message = manager.get_nonce()
    assert message.state == "nonce_available"
    built = manager.build_message()
    exact = built.siwe_message.message
    result = manager.authenticate()
    assert result.state == "authenticated"
    assert signer.call_count == 1
    assert signer.last_message_fingerprint
    assert manager.snapshot().nonce_response is None
    assert manager.snapshot().siwe_message is None

    mismatched = DreamDexAuthManager(
        signer=FixtureSiweMessageSigner(OTHER), transport=FixtureDreamDexAuthTransport(fixture(), now=NOW),
        owner_address=ADDRESS, clock=lambda: NOW,
    )
    blocked = mismatched.authenticate()
    assert blocked.state == "signer_address_mismatch"
    assert blocked.signer_address_match == "conflicting"
    assert mismatched.transport.nonce_calls == 0


@pytest.mark.parametrize("kwargs", [{"malformed": True}, {"fixture_signature": "0x11"}, {"fixture_signature": "11" + "11" * 65}])
def test_signature_shape_is_checked_before_login(kwargs):
    transport = FixtureDreamDexAuthTransport(fixture(), now=NOW)
    signer = FixtureSiweMessageSigner(ADDRESS, **kwargs)
    result = DreamDexAuthManager(signer=signer, transport=transport, owner_address=ADDRESS, clock=lambda: NOW).authenticate()
    assert result.state == "failed_closed"
    assert transport.login_calls == 0
    assert not result.token_present


def test_rejecting_fixture_clears_temporary_state():
    transport = FixtureDreamDexAuthTransport(fixture(), now=NOW)
    signer = FixtureSiweMessageSigner(ADDRESS, reject=True)
    result = DreamDexAuthManager(signer=signer, transport=transport, owner_address=ADDRESS, clock=lambda: NOW).authenticate()
    assert result.state == "failed_closed"
    assert result.nonce_response is None and result.siwe_message is None
    assert transport.login_calls == 0
    assert result.signer_invocation_performed
