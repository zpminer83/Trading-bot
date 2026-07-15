from datetime import datetime, timedelta, timezone
import json

import pytest

from bot.integrations.dreamdex_auth_models import DreamDexAuthManager, FixtureMessageSigner
from bot.integrations.dreamdex_siwe_http_transport import (
    DreamDexSiweHttpTransport,
    FixtureDreamDexHttpClient,
    FixtureHttpResponse,
    build_siwe_http_transport_from_env,
)


ADDRESS = "0x1234567890abcdef1234567890abcdef12345678"
BASE_URL = "https://api.dreamdex.io/v0"


def response(payload, status=200, headers=None):
    return FixtureHttpResponse(status, json.dumps(payload) if payload is not None else None, headers or {"content-type": "application/json"})


def transport(responses):
    return DreamDexSiweHttpTransport(base_url=BASE_URL, http_client=FixtureDreamDexHttpClient(responses))


def test_factory_is_disabled_by_default_and_reads_only_explicit_settings():
    value = build_siwe_http_transport_from_env({})
    assert not value.configured
    assert value.configuration_status == "disabled"
    assert value.request_attempt_count == 0
    enabled = build_siwe_http_transport_from_env({"DREAMDEX_ENABLE_SIWE_AUTH_TRANSPORT": "true", "DREAMDEX_READ_ONLY_BASE_URL": BASE_URL})
    assert enabled.configured and enabled.configuration_status == "configured"


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
def test_factory_accepts_true_values(value):
    result = build_siwe_http_transport_from_env({"DREAMDEX_ENABLE_SIWE_AUTH_TRANSPORT": value, "DREAMDEX_READ_ONLY_BASE_URL": BASE_URL})
    assert result.configured


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off"])
def test_factory_accepts_false_values(value):
    result = build_siwe_http_transport_from_env({"DREAMDEX_ENABLE_SIWE_AUTH_TRANSPORT": value, "DREAMDEX_READ_ONLY_BASE_URL": BASE_URL})
    assert result.configuration_status == "disabled"


def test_factory_invalid_and_base_url_states_are_fail_closed():
    assert build_siwe_http_transport_from_env({"DREAMDEX_ENABLE_SIWE_AUTH_TRANSPORT": "maybe"}).configuration_status == "configuration_invalid"
    assert build_siwe_http_transport_from_env({"DREAMDEX_ENABLE_SIWE_AUTH_TRANSPORT": "true"}).configuration_status == "base_url_missing"
    assert build_siwe_http_transport_from_env({"DREAMDEX_ENABLE_SIWE_AUTH_TRANSPORT": "true", "DREAMDEX_READ_ONLY_BASE_URL": "http://api.dreamdex.io/v0"}).configuration_status == "base_url_invalid"
    for bad in (
        "https://evil.example/v0", "https://api.dreamdex.io/", "https://api.dreamdex.io/v1",
        "https://user:pass@api.dreamdex.io/v0", "https://api.dreamdex.io:443/v0",
        "https://api.dreamdex.io/v0?x=1", "https://api.dreamdex.io/v0#fragment",
        "https://api.dreamdex.io/v0//",
    ):
        with pytest.raises(ValueError, match="base_url_invalid"):
            DreamDexSiweHttpTransport(base_url=bad)


def test_exact_nonce_get_path_and_no_undocumented_query():
    client = FixtureDreamDexHttpClient({("GET", "/v0/auth/nonce"): response({"nonce": "secret-nonce", "message": "secret-message"})})
    value = DreamDexSiweHttpTransport(base_url=BASE_URL, http_client=client).get_nonce(ADDRESS)
    assert value.nonce == "secret-nonce" and value.http_status == 200
    call = client.calls[0]
    assert call["method"] == "GET"
    assert call["url"] == f"{BASE_URL}/auth/nonce"
    assert "address=" not in call["url"]
    assert call["kwargs"]["follow_redirects"] is False
    assert call["kwargs"]["cookies"] == {}


def test_invalid_address_is_rejected_without_network():
    client = FixtureDreamDexHttpClient([])
    value = DreamDexSiweHttpTransport(base_url=BASE_URL, http_client=client)
    with pytest.raises(ValueError, match="invalid address"):
        value.get_nonce("not-an-address")
    assert not client.calls


def test_nonce_statuses_and_malformed_shapes_are_safe():
    for fixture, expected in [
        ({"status": 400}, "bad_request"), ({"status": 401}, "unauthorized"),
        ({"status": 403}, "forbidden"), ({"status": 404}, "not_found"),
        ({"status": 409}, "conflict"), ({"status": 429}, "rate_limited"),
        ({"status": 503}, "upstream_unavailable"),
    ]:
        result = transport({("GET", "/v0/auth/nonce"): fixture}).get_nonce(ADDRESS)
        assert result.error_code == expected and result.http_status == fixture["status"]
    for payload, code in [({}, "missing_nonce"), ({"nonce": ""}, "missing_nonce"), ({"nonce": 3}, "missing_nonce")]:
        result = transport({("GET", "/v0/auth/nonce"): response(payload)}).get_nonce(ADDRESS)
        assert result.error_code == code
    assert transport({("GET", "/v0/auth/nonce"): FixtureHttpResponse(200, b"not-json")}).get_nonce(ADDRESS).error_code == "malformed_json"
    assert transport({("GET", "/v0/auth/nonce"): FixtureHttpResponse(200, b"")}).get_nonce(ADDRESS).error_code == "empty_response"


def test_login_request_is_exact_and_never_adds_cookie_or_browser_headers():
    expires = int((datetime.now(timezone.utc) + timedelta(minutes=30)).timestamp() * 1000)
    client = FixtureDreamDexHttpClient({("POST", "/v0/auth/login"): response({"token": "secret-jwt", "expiresAt": expires})})
    value = DreamDexSiweHttpTransport(base_url=BASE_URL, http_client=client).login("secret-message", "secret-signature")
    assert value.token == "secret-jwt" and value.http_status == 200
    call = client.calls[0]
    assert call["method"] == "POST" and call["url"] == f"{BASE_URL}/auth/login"
    assert json.loads(call["kwargs"]["content"]) == {"message": "secret-message", "signature": "secret-signature"}
    assert call["kwargs"]["cookies"] == {}
    assert "Authorization" not in call["kwargs"]["headers"]
    assert "User-Agent" not in call["kwargs"]["headers"]


@pytest.mark.parametrize("payload,code", [
    ({"jwt": "secret", "expiresAt": 1}, "missing_token"),
    ({"token": "", "expiresAt": 1}, "missing_token"),
    ({"token": "x", "expiresAt": True}, "malformed_expiresAt"),
    ({"token": "x", "expiresAt": "bad"}, "malformed_expiresAt"),
])
def test_login_schema_is_strict(payload, code):
    result = transport({("POST", "/v0/auth/login"): response(payload)}).login("message", "signature")
    assert result.error_code == code


def test_expired_and_far_future_tokens_are_rejected():
    expired = int((datetime.now(timezone.utc) - timedelta(seconds=1)).timestamp() * 1000)
    future = int((datetime.now(timezone.utc) + timedelta(days=2)).timestamp() * 1000)
    assert transport({("POST", "/v0/auth/login"): response({"token": "x", "expiresAt": expired})}).login("m", "s").error_code == "expired"
    assert transport({("POST", "/v0/auth/login"): response({"token": "x", "expiresAt": future})}).login("m", "s").error_code == "far_future_expiry"


def test_authenticated_get_only_allows_confirmed_paths_and_authorization():
    client = FixtureDreamDexHttpClient({("GET", "/v0/markets/SOMI:USDso/orders"): response({"orders": []})})
    value = DreamDexSiweHttpTransport(base_url=BASE_URL, http_client=client)
    result = value.authenticated_get("/markets/SOMI:USDso/orders", {"status": "open"}, token="secret-jwt")
    assert result.status == 200
    call = client.calls[0]
    assert call["url"].endswith("/orders?status=open")
    assert call["kwargs"]["headers"]["Authorization"] == "Bearer secret-jwt"
    assert value.authenticated_get("/markets/SOMI:USDso/orders", token=None).error_code == "unauthorized"
    with pytest.raises(ValueError):
        value.authenticated_get("https://evil.example/submit", token="x")
    with pytest.raises(ValueError):
        value.authenticated_get("/markets/SOMI:USDso/orders", {"evil": "1"}, token="x")
    with pytest.raises(ValueError):
        value.authenticated_get("/markets/SOMI:USDso/orders/../x", token="x")


def test_vault_and_trades_paths_are_explicitly_allowlisted():
    client = FixtureDreamDexHttpClient({
        ("GET", "/v0/markets/SOMI:USDso/vault/balance"): response({"balances": []}),
        ("GET", "/v0/markets/SOMI:USDso/trades"): response([]),
    })
    value = DreamDexSiweHttpTransport(base_url=BASE_URL, http_client=client)
    assert value.authenticated_get("/markets/SOMI:USDso/vault/balance", {"walletAddress": ADDRESS}, token="x").status == 200
    assert value.authenticated_get("/markets/SOMI:USDso/trades", {"limit": "1"}, token="x").status == 200


def test_redirect_oversized_and_transport_errors_are_mapped_without_body():
    redirect = transport({("GET", "/v0/auth/nonce"): FixtureHttpResponse(302, "secret-body", {"location": "https://evil.example"})}).get_nonce(ADDRESS)
    assert redirect.error_code == "redirect_blocked"
    oversized = DreamDexSiweHttpTransport(base_url=BASE_URL, http_client=FixtureDreamDexHttpClient({("GET", "/v0/auth/nonce"): FixtureHttpResponse(200, b"x" * 20)}), max_response_body_bytes=10).get_nonce(ADDRESS)
    assert oversized.error_code == "response_too_large"
    def timeout(*args, **kwargs):
        raise TimeoutError("secret nonce JWT Authorization")
    timed = DreamDexSiweHttpTransport(base_url=BASE_URL, http_client=timeout).get_nonce(ADDRESS)
    assert timed.error_code == "timeout"
    assert "secret" not in repr(timed)


def test_configured_transport_without_signer_does_not_start_auth_flow():
    client = FixtureDreamDexHttpClient({("GET", "/v0/auth/nonce"): response({"nonce": "n"})})
    transport_instance = DreamDexSiweHttpTransport(base_url=BASE_URL, http_client=client)
    manager = DreamDexAuthManager(transport=transport_instance)
    snapshot = manager.snapshot().safe_dict()
    assert transport_instance.configured
    assert snapshot["manager_configured"] is False
    assert snapshot["transport_configured"] is True
    assert snapshot["auth_network_attempt_performed"] is False
    assert not client.calls


def test_fixture_transport_repr_and_errors_do_not_expose_secrets():
    client = FixtureDreamDexHttpClient({("GET", "/v0/auth/nonce"): response({"nonce": "nonce-secret"})})
    value = DreamDexSiweHttpTransport(base_url=BASE_URL, http_client=client)
    result = value.get_nonce(ADDRESS)
    assert "nonce-secret" not in repr(value)
    assert "nonce-secret" not in repr(result)
    assert "Authorization" not in repr(value)
