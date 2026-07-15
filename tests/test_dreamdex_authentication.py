from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from bot.integrations.dreamdex_auth_models import (
    AuthState,
    DreamDexAuthIdentity,
    DreamDexAuthManager,
    DreamDexLoginResponse,
    DreamDexNonceResponse,
    DreamDexSiweMessage,
    DreamDexTokenState,
    FixtureDreamDexAuthTransport,
    FixtureMessageSigner,
    RejectingUnconfiguredSigner,
)


ADDRESS = "0x1234567890abcdef1234567890abcdef12345678"
NOW = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)


def fixture(*, expires_at=None, sequence=None):
    expires_at = expires_at or int((NOW + timedelta(hours=1)).timestamp() * 1000)
    value = {
        "nonce_response": {"nonce": "fixture-nonce"},
        "login_response": {"token": "fixture-jwt", "expiresAt": expires_at},
        "authenticated_get": {"status": 200, "payload": {"ok": True}},
    }
    if sequence is not None:
        value["authenticated_get_sequence"] = list(sequence)
    return value


def manager(data=None, *, clock=lambda: NOW):
    transport = FixtureDreamDexAuthTransport(data or fixture(), now=NOW)
    signer = FixtureMessageSigner(ADDRESS)
    return DreamDexAuthManager(signer=signer, transport=transport, clock=clock), transport


def test_official_nonce_and_login_shapes_are_strict_and_safe():
    nonce = DreamDexNonceResponse.from_payload({"nonce": "abc", "message": "template"}, observed_at=NOW)
    assert nonce.nonce == "abc" and nonce.message == "template"
    login = DreamDexLoginResponse.from_payload({"token": "secret", "expiresAt": int((NOW + timedelta(minutes=5)).timestamp() * 1000)}, observed_at=NOW)
    assert login.expires_at == NOW + timedelta(minutes=5)
    assert "secret" not in repr(login)
    assert DreamDexLoginResponse.from_payload({"jwt": "secret", "expiresAt": 1}).source_status == "malformed"
    assert DreamDexLoginResponse.from_payload({"token": "x", "expiresAt": "bad"}).error_code == "malformed_expiresAt"


def test_siwe_template_matches_bot_kit():
    message = DreamDexSiweMessage(ADDRESS, "api.dreamdex.io", "https://api.dreamdex.io", 5031, "abc", NOW)
    assert message.message == (
        "api.dreamdex.io wants you to sign in with your Ethereum account:\n"
        f"{ADDRESS}\n\nSign in to dreamDEX\n\n"
        "URI: https://api.dreamdex.io\nVersion: 1\nChain ID: 5031\nNonce: abc\n"
        "Issued At: 2026-07-15T12:00:00.000Z"
    )
    assert "abc" not in repr(message)


def test_fixture_state_transitions_and_no_default_auth():
    manager_instance, transport = manager()
    assert manager_instance.snapshot().state == AuthState.nonce_required.value
    assert manager_instance.get_nonce().state == AuthState.nonce_available.value
    assert manager_instance.build_message().state == AuthState.message_built.value
    snapshot = manager_instance.authenticate()
    assert snapshot.state == AuthState.authenticated.value
    assert snapshot.token_present
    assert transport.nonce_calls == 1 and transport.login_calls == 1
    unconfigured = DreamDexAuthManager()
    assert unconfigured.snapshot().safe_dict()["state"] == "unconfigured"
    assert unconfigured.authenticate().state == "unconfigured"


def test_cached_token_refresh_window_and_expiry():
    data = fixture(expires_at=int((NOW + timedelta(seconds=60)).timestamp() * 1000))
    manager_instance, transport = manager(data)
    manager_instance.authenticate()
    manager_instance.ensure_authenticated()
    assert transport.login_calls == 2
    expired = DreamDexTokenState("x", NOW - timedelta(seconds=1), Decimal("60"), "confirmed")
    assert expired.expiry_status(NOW) == "expired"


def test_failed_refresh_clears_expired_token():
    class FailingRefresh(FixtureDreamDexAuthTransport):
        def login(self, message, signature):
            if self.login_calls:
                self.login_calls += 1
                return DreamDexLoginResponse(None, None, source_status="malformed", error_code="missing_token", observed_at=NOW)
            return super().login(message, signature)

    transport = FailingRefresh(fixture(expires_at=int((NOW + timedelta(seconds=60)).timestamp() * 1000)), now=NOW)
    manager_instance = DreamDexAuthManager(signer=FixtureMessageSigner(ADDRESS), transport=transport, clock=lambda: NOW)
    assert manager_instance.authenticate().token_present
    refreshed = manager_instance.ensure_authenticated()
    assert refreshed.state == AuthState.failed_closed.value
    assert not refreshed.token_present


def test_bounded_401_retry_and_second_401_unauthorized():
    data = fixture(sequence=[{"status": 401}, {"status": 200, "payload": {"ok": True}}])
    manager_instance, transport = manager(data)
    result = manager_instance.authenticated_get("/private")
    assert result.status == 200 and transport.login_calls == 2 and transport.authenticated_get_calls == 2
    failing, _ = manager(fixture(sequence=[{"status": 401}, {"status": 401}]))
    assert failing.authenticated_get("/private").status == 401
    assert failing.snapshot().state == AuthState.unauthorized.value


def test_single_flight_concurrent_callers_and_secret_redaction():
    manager_instance, transport = manager()
    with ThreadPoolExecutor(max_workers=8) as pool:
        states = list(pool.map(lambda _: manager_instance.ensure_authenticated().state, range(8)))
    assert all(state == AuthState.authenticated.value for state in states)
    assert transport.login_calls == 1 and transport.nonce_calls == 1
    assert "fixture-jwt" not in repr(manager_instance.snapshot())


def test_clock_regression_fail_closed_and_reset():
    times = [NOW, NOW - timedelta(seconds=1)]
    manager_instance, _ = manager(clock=lambda: times.pop(0) if times else NOW - timedelta(seconds=1))
    manager_instance.snapshot()
    with pytest.raises(RuntimeError, match="clock_regression"):
        manager_instance.ensure_authenticated()
    assert manager_instance.snapshot().state == AuthState.failed_closed.value
    assert manager_instance.reset().state == AuthState.nonce_required.value


def test_identity_never_auto_resolves_address_semantics():
    identity = DreamDexAuthIdentity(login_address=ADDRESS, trading_address="0xabcdefabcdefabcdefabcdefabcdefabcdefabcd")
    assert not identity.authoritative
    assert identity.address_semantics_status == "unresolved"
    assert identity.login_address != identity.trading_address
    with pytest.raises(ValueError):
        DreamDexAuthIdentity(login_address="not-an-address")


def test_fixture_signer_and_rejecting_signer_have_no_key_loader():
    signer = FixtureMessageSigner(ADDRESS)
    assert signer.sign_message("hello").startswith("0xfixture")
    with pytest.raises(RuntimeError, match="unconfigured_signer"):
        RejectingUnconfiguredSigner().sign_message("hello")
    assert not hasattr(RejectingUnconfiguredSigner(), "private_key")
