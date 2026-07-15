from decimal import Decimal

import pytest

from bot.integrations.dreamdex_authenticated_read_only import (
    ENABLE_ENV,
    PRODUCTION_BASE_URL,
    TOKEN_ENV,
    DreamDexAuthenticatedReadOnlyTransport,
    build_authenticated_read_only_transport_from_env,
)
from bot.integrations.dreamdex_auth_models import UnconfiguredAuthenticatedReadOnlyTransport


SYMBOL = "SOMI:USDso"
OWNER = "0x" + "12" * 20
TOKEN = "fixture-bearer-token"


class FakeResponse:
    def __init__(self, status_code=200, payload=None, *, content_type="application/json", content=None, headers=None):
        self.status_code = status_code
        self.headers = {"content-type": content_type, **(headers or {})}
        self._payload = payload
        self.content = content if content is not None else b"{}"

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def _transport(**env):
    values = {ENABLE_ENV: "1", TOKEN_ENV: TOKEN}
    values.update(env)
    return DreamDexAuthenticatedReadOnlyTransport(environ=values)


def test_disabled_by_default_and_token_missing_do_not_use_network():
    disabled = DreamDexAuthenticatedReadOnlyTransport(environ={})
    assert not disabled.configured
    assert not disabled.request_execution_enabled
    assert disabled.fetch_vault_balance(SYMBOL, OWNER).source_status.error_code == "authenticated_transport_unconfigured"
    missing = DreamDexAuthenticatedReadOnlyTransport(environ={ENABLE_ENV: "true"})
    assert not missing.configured
    assert missing.fetch_vault_balance(SYMBOL, OWNER).source_status.error_code == "authenticated_token_missing"
    for transport in (disabled, missing):
        assert not hasattr(transport, "request")
        assert not hasattr(transport, "get")
        assert not hasattr(transport, "post")
        assert not hasattr(transport, "login")
        assert not hasattr(transport, "sign")
    assert TOKEN not in repr(transport)


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
def test_env_factory_accepts_true_values_without_exposing_token(value):
    transport = build_authenticated_read_only_transport_from_env({ENABLE_ENV: value, TOKEN_ENV: TOKEN})
    assert isinstance(transport, DreamDexAuthenticatedReadOnlyTransport)
    assert transport.configuration_status == "configured"
    assert transport.configured
    assert TOKEN not in repr(transport)


@pytest.mark.parametrize("value", [None, "", "0", "false", "no", "off"])
def test_env_factory_disabled_values_return_unconfigured_without_network(value):
    env = {TOKEN_ENV: TOKEN}
    if value is not None:
        env[ENABLE_ENV] = value
    transport = build_authenticated_read_only_transport_from_env(env)
    assert isinstance(transport, UnconfiguredAuthenticatedReadOnlyTransport)


def test_env_factory_invalid_flag_and_missing_token_are_explicit_and_offline(monkeypatch):
    invalid = build_authenticated_read_only_transport_from_env({ENABLE_ENV: "maybe", TOKEN_ENV: TOKEN})
    assert isinstance(invalid, DreamDexAuthenticatedReadOnlyTransport)
    assert invalid.configuration_status == "authenticated_configuration_invalid"
    assert invalid.fetch_vault_balance(SYMBOL, OWNER).source_status.error_code == "authenticated_configuration_invalid"
    missing = build_authenticated_read_only_transport_from_env({ENABLE_ENV: "1"})
    assert missing.configuration_status == "token_missing"
    assert not missing.request_execution_enabled
    assert missing.fetch_vault_balance(SYMBOL, OWNER).source_status.error_code == "authenticated_token_missing"


def test_host_pinning_and_safe_path_validation():
    with pytest.raises(ValueError):
        DreamDexAuthenticatedReadOnlyTransport(base_url="http://api.dreamdex.io/v0", environ={})
    with pytest.raises(ValueError):
        DreamDexAuthenticatedReadOnlyTransport(base_url="https://evil.example/v0", environ={})
    transport = _transport()
    with pytest.raises(ValueError):
        transport.fetch_order_by_id("SOMI/../USDso", "7")
    with pytest.raises(ValueError):
        transport.fetch_order_by_id(SYMBOL, "../7")
    with pytest.raises(ValueError):
        transport.fetch_orders_page(SYMBOL, status="unknown")
    with pytest.raises(ValueError):
        transport._url("unknown", SYMBOL)
    with pytest.raises(ValueError):
        transport.fetch_vault_balance(SYMBOL + "\r\nX", OWNER)
    assert PRODUCTION_BASE_URL == "https://api.dreamdex.io/v0"


def test_valid_vault_response_uses_only_confirmed_shape_and_fingerprint(monkeypatch):
    calls = []
    payload = {"balances": [{"currency": "SOMI", "amount": "1.25"}, {"currency": "USDso", "amount": "20"}]}

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        return FakeResponse(payload=payload, content=b'{"balances":[]}', headers={"content-type": "application/json"})

    monkeypatch.setattr("httpx.get", fake_get)
    result = _transport().fetch_vault_balance(SYMBOL, OWNER)
    assert result.source_status.status == "available"
    assert {item.asset: item.total for item in result.balances} == {"SOMI": Decimal("1.25"), "USDso": Decimal("20")}
    assert result.schema_fingerprint is not None
    assert "balances" in result.schema_fingerprint.field_names
    assert "1.25" not in repr(result.schema_fingerprint)
    url, kwargs = calls[0]
    assert url == "https://api.dreamdex.io/v0/markets/SOMI:USDso/vault/balance"
    assert kwargs["params"] == {"walletAddress": OWNER}
    assert kwargs["follow_redirects"] is False
    assert kwargs["cookies"] == {}
    assert kwargs["headers"]["Accept"] == "application/json"
    assert kwargs["headers"]["Authorization"] == f"Bearer {TOKEN}"


def test_unknown_vault_schema_is_not_authoritative_and_missing_values_are_not_zero(monkeypatch):
    monkeypatch.setattr("httpx.get", lambda *args, **kwargs: FakeResponse(payload={"data": {"SOMI": "1"}}, content=b"{}"))
    result = _transport().fetch_vault_balance(SYMBOL, OWNER)
    assert result.source_status.status == "available_but_unverified_schema"
    assert result.source_status.error_code == "unverified_schema"
    assert result.balances == ()


def test_order_by_id_and_order_list_are_get_only_and_pagination_stays_incomplete(monkeypatch):
    responses = [
        FakeResponse(payload={"id": "7", "symbol": SYMBOL, "price": "10.5", "amount": "2", "remaining": "1", "status": "open", "owner": OWNER}, content=b"{}"),
        FakeResponse(payload={"orders": [{"id": "7", "symbol": SYMBOL, "amount": "2"}]}, content=b"{}"),
    ]
    calls = []

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        return responses.pop(0)

    monkeypatch.setattr("httpx.get", fake_get)
    transport = _transport()
    order = transport.fetch_order_by_id(SYMBOL, "7")
    assert order.status == "available"
    assert order.metadata is not None
    assert order.metadata.price == Decimal("10.5")
    assert order.metadata.quantity == Decimal("2")
    records, status = transport.fetch_orders_page(SYMBOL, status="open")
    assert len(records) == 1
    assert status.status == "available"
    assert status.pagination_complete is False
    assert status.error_code == "pagination_not_confirmed"
    assert all(call[1]["headers"]["Accept"] == "application/json" for call in calls)
    assert all(call[0].startswith(PRODUCTION_BASE_URL + "/markets/") for call in calls)


@pytest.mark.parametrize("status_code,error_code", [(401, "unauthorized"), (403, "forbidden"), (404, "not_found"), (429, "rate_limited"), (500, "upstream_unavailable")])
def test_http_status_mapping_does_not_expose_body(monkeypatch, status_code, error_code):
    secret_body = f"Authorization: Bearer {TOKEN} private-key=hidden"
    monkeypatch.setattr("httpx.get", lambda *args, **kwargs: FakeResponse(status_code=status_code, payload={"secret": secret_body}, content=secret_body.encode()))
    result = _transport().fetch_order_by_id(SYMBOL, "7")
    assert result.source_status.error_code == error_code
    assert TOKEN not in repr(result)
    assert "Authorization" not in repr(result)


def test_redirect_malformed_json_and_oversized_body_are_blocked(monkeypatch):
    monkeypatch.setattr("httpx.get", lambda *args, **kwargs: FakeResponse(status_code=302, payload={"location": "https://evil.example"}, headers={"location": "https://evil.example"}))
    redirect = _transport().fetch_vault_balance(SYMBOL, OWNER)
    assert redirect.source_status.error_code == "redirect_blocked"
    monkeypatch.setattr("httpx.get", lambda *args, **kwargs: FakeResponse(status_code=200, payload=ValueError("bad-json"), content=b"not-json"))
    malformed = _transport().fetch_vault_balance(SYMBOL, OWNER)
    assert malformed.source_status.error_code == "malformed_response"
    monkeypatch.setattr("httpx.get", lambda *args, **kwargs: FakeResponse(status_code=200, payload={}, content=b"x" * 1_000_001))
    oversized = _transport().fetch_vault_balance(SYMBOL, OWNER)
    assert oversized.source_status.error_code == "response_too_large"


def test_exception_redacts_token_and_sensitive_names(monkeypatch):
    def fake_get(*args, **kwargs):
        raise RuntimeError(f"Authorization: Bearer {TOKEN}; nonce=abc signature=def")

    monkeypatch.setattr("httpx.get", fake_get)
    result = _transport().fetch_vault_balance(SYMBOL, OWNER)
    text = repr(result)
    assert TOKEN not in text
    assert "Bearer " not in text or "Bearer <redacted>" in text
    assert "nonce=abc" not in text
    assert "signature=def" not in text


def test_private_key_environment_is_not_read_and_invalid_address_is_unavailable(monkeypatch):
    transport = DreamDexAuthenticatedReadOnlyTransport(environ={ENABLE_ENV: "1", TOKEN_ENV: TOKEN, "PRIVATE_KEY": "should-not-be-read"})
    result = transport.fetch_vault_balance(SYMBOL, "not-an-address")
    assert result.source_status.error_code == "invalid_target"
    assert "PRIVATE_KEY" not in repr(transport)
