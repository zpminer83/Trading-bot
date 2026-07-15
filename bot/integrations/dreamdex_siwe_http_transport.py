"""Strict, opt-in HTTP transport for the documented DreamDEX SIWE flow.

This module deliberately stops at authentication and allow-listed GET reads.
It does not contain a signer, key loader, generic request method, or trading
mutation endpoint.  The default factory is disabled and all tests use the
injectable fixture client below.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
import re
from typing import Any, Callable, Mapping, Protocol
from urllib.parse import parse_qsl, urlencode, urlsplit

from bot.integrations.dreamdex_auth_models import (
    AuthTransportResponse,
    DreamDexLoginResponse,
    DreamDexNonceResponse,
    _normalize_auth_address,
)


SIWE_TRANSPORT_ENABLE_ENV = "DREAMDEX_ENABLE_SIWE_AUTH_TRANSPORT"
SIWE_TRANSPORT_BASE_URL_ENV = "DREAMDEX_READ_ONLY_BASE_URL"
# Short compatibility aliases for callers that use the existing transport
# modules' constant naming convention.
ENABLE_ENV = SIWE_TRANSPORT_ENABLE_ENV
BASE_URL_ENV = SIWE_TRANSPORT_BASE_URL_ENV
SIWE_PRODUCTION_HOST = "api.dreamdex.io"
SIWE_BASE_PATH = "/v0"
DEFAULT_CONNECT_TIMEOUT_SECONDS = 5.0
DEFAULT_READ_TIMEOUT_SECONDS = 10.0
MAX_RESPONSE_BODY_BYTES = 1_000_000
MAX_REQUEST_BODY_BYTES = 64_000
MAX_TOKEN_LIFETIME_SECONDS = 24 * 60 * 60

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"", "0", "false", "no", "off"})
_SYMBOL_RE = re.compile(r"^[A-Za-z0-9_.-]{1,32}:[A-Za-z0-9_.-]{1,32}$")
_ORDER_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
_ALLOWED_AUTH_GET_ROOTS = frozenset({"vault", "orders", "order", "trades"})


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_flag(value: Any) -> str:
    normalized = "" if value is None else str(value).strip().lower()
    if normalized in _TRUE_VALUES:
        return "enabled"
    if normalized in _FALSE_VALUES:
        return "disabled"
    return "invalid"


def _safe_error_code(exc: BaseException) -> str:
    name = type(exc).__name__.lower()
    if "timeout" in name:
        return "timeout"
    if "ssl" in name or "certificate" in name:
        return "transport_error"
    return "transport_error"


def _response_content(response: Any) -> tuple[int, Mapping[str, str], bytes, bool]:
    status = int(getattr(response, "status_code", getattr(response, "status", 0)))
    headers_raw = getattr(response, "headers", {}) or {}
    headers = {str(k).lower(): str(v) for k, v in dict(headers_raw).items()}
    content = getattr(response, "content", None)
    if content is None:
        text = getattr(response, "text", "")
        content = text.encode("utf-8") if isinstance(text, str) else b""
    elif isinstance(content, str):
        content = content.encode("utf-8")
    elif not isinstance(content, bytes):
        content = bytes(content)
    redirect = status in {301, 302, 303, 307, 308} or bool(getattr(response, "is_redirect", False))
    return status, headers, content, redirect


def _status_error_code(status: int) -> str | None:
    if status == 400:
        return "bad_request"
    if status == 401:
        return "unauthorized"
    if status == 403:
        return "forbidden"
    if status == 404:
        return "not_found"
    if status == 409:
        return "conflict"
    if status == 429:
        return "rate_limited"
    if 500 <= status <= 599:
        return "upstream_unavailable"
    return None


def _parse_json_body(content: bytes, headers: Mapping[str, str], max_bytes: int) -> tuple[Any, str | None]:
    if not content or not content.strip():
        return None, "empty_response"
    if len(content) > max_bytes:
        return None, "response_too_large"
    content_type = headers.get("content-type", "").lower()
    if content_type and "json" not in content_type:
        return None, "malformed_json"
    try:
        return json.loads(content.decode("utf-8")), None
    except (UnicodeDecodeError, TypeError, ValueError, json.JSONDecodeError):
        return None, "malformed_json"


def _validate_base_url(base_url: str) -> str:
    if not isinstance(base_url, str) or not base_url.strip():
        raise ValueError("base_url_missing")
    parsed = urlsplit(base_url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != SIWE_PRODUCTION_HOST
        or parsed.port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {SIWE_BASE_PATH, SIWE_BASE_PATH + "/"}
        or "//" in parsed.path
    ):
        raise ValueError("base_url_invalid")
    return f"https://{SIWE_PRODUCTION_HOST}{SIWE_BASE_PATH}"


def _validate_query(query: Mapping[str, str] | None, allowed: frozenset[str]) -> str:
    if query is None:
        return ""
    if not isinstance(query, Mapping):
        raise ValueError("query_not_allowed")
    pairs: list[tuple[str, str]] = []
    for key, value in query.items():
        if not isinstance(key, str) or key not in allowed or not isinstance(value, str):
            raise ValueError("query_not_allowed")
        if any(char in key or char in value for char in ("\r", "\n")):
            raise ValueError("query_not_allowed")
        if key == "walletAddress":
            try:
                _normalize_auth_address(value)
            except ValueError:
                raise ValueError("query_not_allowed") from None
        elif any(char in value for char in ("&", "=", "#", "?")):
            raise ValueError("query_not_allowed")
        pairs.append((key, value))
    return urlencode(pairs, doseq=False)


def _validate_read_path(path: str) -> tuple[str, frozenset[str]]:
    if not isinstance(path, str) or not path.startswith("/") or "\\" in path:
        raise ValueError("unsupported_authenticated_path")
    parsed = urlsplit(path)
    if parsed.scheme or parsed.netloc or parsed.fragment or parsed.query or parsed.path != path:
        raise ValueError("unsupported_authenticated_path")
    if "%" in path or "//" in path or any(part in {"", ".", ".."} for part in path.split("/")[1:]):
        raise ValueError("unsupported_authenticated_path")
    parts = path.split("/")
    if len(parts) < 4 or parts[1] != "markets" or not _SYMBOL_RE.fullmatch(parts[2]):
        raise ValueError("unsupported_authenticated_path")
    if len(parts) == 5 and parts[3] == "orders" and _ORDER_ID_RE.fullmatch(parts[4]):
        return path, frozenset()
    if len(parts) == 5 and parts[3] == "vault" and parts[4] == "balance":
        return path, frozenset({"walletAddress"})
    if len(parts) != 4:
        raise ValueError("unsupported_authenticated_path")
    if parts[3] == "vault" or parts[3] == "trades":
        if parts[3] == "vault":
            # The only supported vault read is /vault/balance.
            raise ValueError("unsupported_authenticated_path")
        return path, frozenset({"limit", "cursor"})
    if parts[3] == "orders":
        return path, frozenset({"status", "cursor", "limit"})
    raise ValueError("unsupported_authenticated_path")


class SiweHttpClient(Protocol):
    def request(self, method: str, url: str, **kwargs: Any) -> Any: ...


@dataclass(frozen=True)
class FixtureHttpResponse:
    status_code: int
    body: bytes | str | None = None
    headers: Mapping[str, str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        object.__setattr__(self, "headers", dict(self.headers or {"content-type": "application/json"}))

    @property
    def content(self) -> bytes:
        if self.body is None:
            return b""
        if isinstance(self.body, (Mapping, list, tuple)):
            return json.dumps(self.body, separators=(",", ":")).encode("utf-8")
        return self.body.encode("utf-8") if isinstance(self.body, str) else self.body

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")

    @property
    def is_redirect(self) -> bool:
        return self.status_code in {301, 302, 303, 307, 308}

    def __repr__(self) -> str:
        return f"FixtureHttpResponse(status_code={self.status_code}, body_present={bool(self.content)})"


class FixtureSiweHttpClient:
    """Deterministic injectable client; it never opens a socket."""

    def __init__(self, responses: Any = None) -> None:
        self.responses = responses if responses is not None else {}
        self.calls: list[dict[str, Any]] = []

    def __repr__(self) -> str:
        return f"FixtureSiweHttpClient(call_count={len(self.calls)})"

    @staticmethod
    def _convert(value: Any) -> FixtureHttpResponse:
        if isinstance(value, FixtureHttpResponse):
            return value
        if isinstance(value, Mapping):
            status = int(value.get("status_code", value.get("status", 200)))
            if "body" in value:
                body = value.get("body")
            elif "json" in value:
                body = json.dumps(value.get("json"), separators=(",", ":"))
            else:
                body = None
            return FixtureHttpResponse(status, body, value.get("headers"))
        if isinstance(value, tuple) and len(value) == 2:
            return FixtureHttpResponse(int(value[0]), value[1])
        return FixtureHttpResponse(200, value)

    def request(self, method: str, url: str, **kwargs: Any) -> FixtureHttpResponse:
        self.calls.append({"method": method, "url": url, "kwargs": dict(kwargs)})
        key = (method.upper(), urlsplit(url).path)
        value: Any
        if isinstance(self.responses, list):
            value = self.responses.pop(0) if self.responses else {"status": 500}
        elif isinstance(self.responses, Mapping):
            value = self.responses.get(key, self.responses.get(method.upper(), self.responses.get(url, {"status": 500})))
            if isinstance(value, list):
                value = value.pop(0) if value else {"status": 500}
        else:
            value = {"status": 500}
        return self._convert(value)


# Friendly alias for callers that prefer the longer name.
FixtureDreamDexHttpClient = FixtureSiweHttpClient


class DreamDexSiweHttpTransport:
    """Opt-in transport restricted to SIWE and explicit read-only GETs."""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        http_client: SiweHttpClient | Callable[..., Any] | None = None,
        enabled: bool = True,
        configuration_status: str | None = None,
        connect_timeout_seconds: float = DEFAULT_CONNECT_TIMEOUT_SECONDS,
        read_timeout_seconds: float = DEFAULT_READ_TIMEOUT_SECONDS,
        max_response_body_bytes: int = MAX_RESPONSE_BODY_BYTES,
        max_request_body_bytes: int = MAX_REQUEST_BODY_BYTES,
    ) -> None:
        self._enabled = bool(enabled)
        self._http_client = http_client
        self._connect_timeout_seconds = float(connect_timeout_seconds)
        self._read_timeout_seconds = float(read_timeout_seconds)
        self._max_response_body_bytes = int(max_response_body_bytes)
        self._max_request_body_bytes = int(max_request_body_bytes)
        self._network_attempt_performed = False
        self._nonce_request_performed = False
        self._login_request_performed = False
        self.request_attempt_count = 0
        self._base_url: str | None = None
        if configuration_status is not None:
            self._configuration_status = configuration_status
        elif not self._enabled:
            self._configuration_status = "disabled"
        elif not base_url:
            self._configuration_status = "base_url_missing"
        else:
            self._base_url = _validate_base_url(base_url)
            self._configuration_status = "configured"

    @property
    def configured(self) -> bool:
        return self._enabled and self._configuration_status == "configured" and self._base_url is not None

    @property
    def configuration_status(self) -> str:
        return self._configuration_status

    @property
    def request_execution_enabled(self) -> bool:
        return self.configured

    @property
    def network_attempt_performed(self) -> bool:
        return self._network_attempt_performed

    @property
    def nonce_request_performed(self) -> bool:
        return self._nonce_request_performed

    @property
    def login_request_performed(self) -> bool:
        return self._login_request_performed

    def __repr__(self) -> str:
        return f"DreamDexSiweHttpTransport(configured={self.configured}, status={self.configuration_status!r})"

    def _disabled_nonce(self, code: str) -> DreamDexNonceResponse:
        return DreamDexNonceResponse(None, source_status="unavailable", error_code=code, observed_at=_utc_now())

    def _disabled_login(self, code: str) -> DreamDexLoginResponse:
        return DreamDexLoginResponse(None, None, source_status="unavailable", error_code=code, observed_at=_utc_now())

    def _call(self, method: str, path: str, *, headers: Mapping[str, str], content: bytes | None = None) -> tuple[int, Mapping[str, str], Any, str | None]:
        if not self.configured or self._base_url is None:
            raise RuntimeError(self.configuration_status)
        url = self._base_url + path
        self.request_attempt_count += 1
        self._network_attempt_performed = True
        try:
            import httpx
            timeout: Any = httpx.Timeout(self._read_timeout_seconds, connect=self._connect_timeout_seconds)
        except Exception:
            timeout = self._read_timeout_seconds
        kwargs: dict[str, Any] = {
            "headers": dict(headers),
            "follow_redirects": False,
            "cookies": {},
            "verify": True,
            "timeout": timeout,
        }
        if content is not None:
            kwargs["content"] = content
        try:
            client = self._http_client
            if client is None:
                import httpx
                response = httpx.request(method, url, **kwargs)
            elif callable(client) and not hasattr(client, "request"):
                response = client(method, url, **kwargs)
            else:
                response = client.request(method, url, **kwargs)  # type: ignore[union-attr]
        except Exception as exc:
            return 0, {}, None, _safe_error_code(exc)
        status, response_headers, body, redirect = _response_content(response)
        if redirect:
            return status, response_headers, None, "redirect_blocked"
        error = _status_error_code(status)
        if status != 200:
            return status, response_headers, None, error or "http_error"
        payload, parse_error = _parse_json_body(body, response_headers, self._max_response_body_bytes)
        return status, response_headers, payload, parse_error

    def get_nonce(self, address: str) -> DreamDexNonceResponse:
        try:
            _normalize_auth_address(address)
        except ValueError:
            raise ValueError("invalid address") from None
        if not self.configured:
            return self._disabled_nonce(self.configuration_status)
        # Bot Kit's confirmed core implementation uses GET /auth/nonce with
        # no query parameter.  The address is validated for caller identity,
        # but is not guessed into an undocumented query string.
        self._nonce_request_performed = True
        status, _, payload, error = self._call("GET", "/auth/nonce", headers={"Accept": "application/json"})
        if error:
            return DreamDexNonceResponse(None, source_status=error, error_code=error, observed_at=_utc_now(), http_status=status or None)
        response = DreamDexNonceResponse.from_payload(payload, observed_at=_utc_now())
        if response.source_status != "confirmed":
            return DreamDexNonceResponse(None, source_status="malformed", error_code=response.error_code or "malformed_nonce_response", observed_at=response.observed_at, http_status=status)
        return DreamDexNonceResponse(response.nonce, response.message, response.source_status, response.error_code, response.observed_at, status)

    def login(self, message: str, signature: str) -> DreamDexLoginResponse:
        if not isinstance(message, str) or not message.strip() or not isinstance(signature, str) or not signature.strip():
            return self._disabled_login("invalid_request")
        body = json.dumps({"message": message, "signature": signature}, separators=(",", ":")).encode("utf-8")
        if len(body) > self._max_request_body_bytes:
            return self._disabled_login("request_too_large")
        if not self.configured:
            return self._disabled_login(self.configuration_status)
        self._login_request_performed = True
        status, _, payload, error = self._call(
            "POST", "/auth/login",
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            content=body,
        )
        if error:
            return DreamDexLoginResponse(None, None, source_status=error, error_code=error, observed_at=_utc_now(), http_status=status or None)
        response = DreamDexLoginResponse.from_payload(payload, observed_at=_utc_now())
        if response.source_status != "confirmed" or response.expires_at is None:
            return DreamDexLoginResponse(None, None, source_status="malformed", error_code=response.error_code or "malformed_login_response", observed_at=response.observed_at, http_status=status)
        now = _utc_now()
        remaining = (response.expires_at - now).total_seconds()
        if remaining <= 0:
            return DreamDexLoginResponse(None, None, source_status="expired", error_code="expired", observed_at=response.observed_at, http_status=status)
        if remaining > MAX_TOKEN_LIFETIME_SECONDS:
            return DreamDexLoginResponse(None, None, source_status="malformed", error_code="far_future_expiry", observed_at=response.observed_at, http_status=status)
        return DreamDexLoginResponse(response.token, response.expires_at, response.source_status, response.error_code, response.observed_at, status)

    def authenticated_get(self, path: str, query: Mapping[str, str] | None = None, *, token: str | None = None) -> AuthTransportResponse:
        try:
            clean_path, allowed_query = _validate_read_path(path)
            query_string = _validate_query(query, allowed_query)
        except ValueError:
            raise ValueError("unsupported_authenticated_path") from None
        if not token or not isinstance(token, str):
            return AuthTransportResponse(401, None, "unauthorized")
        suffix = "?" + query_string if query_string else ""
        status, _, payload, error = self._call("GET", clean_path + suffix, headers={"Accept": "application/json", "Authorization": f"Bearer {token}"})
        return AuthTransportResponse(status, payload, error)


def build_siwe_http_transport_from_env(environ: Mapping[str, str] | None = None) -> DreamDexSiweHttpTransport:
    """Build transport from exactly two explicit, non-secret environment keys."""
    source = environ if environ is not None else os.environ
    flag = _parse_flag(source.get(SIWE_TRANSPORT_ENABLE_ENV))
    base_url = source.get(SIWE_TRANSPORT_BASE_URL_ENV)
    if flag == "disabled":
        return DreamDexSiweHttpTransport(base_url=base_url, enabled=False, configuration_status="disabled")
    if flag == "invalid":
        return DreamDexSiweHttpTransport(base_url=base_url, enabled=True, configuration_status="configuration_invalid")
    if not base_url:
        return DreamDexSiweHttpTransport(base_url=None, enabled=True, configuration_status="base_url_missing")
    try:
        return DreamDexSiweHttpTransport(base_url=base_url, enabled=True)
    except ValueError:
        return DreamDexSiweHttpTransport(base_url=None, enabled=True, configuration_status="base_url_invalid")


__all__ = [
    "SIWE_TRANSPORT_ENABLE_ENV", "SIWE_TRANSPORT_BASE_URL_ENV", "ENABLE_ENV", "BASE_URL_ENV", "SIWE_PRODUCTION_HOST", "SIWE_BASE_PATH",
    "MAX_RESPONSE_BODY_BYTES", "MAX_REQUEST_BODY_BYTES", "MAX_TOKEN_LIFETIME_SECONDS",
    "FixtureHttpResponse", "FixtureSiweHttpClient", "FixtureDreamDexHttpClient",
    "DreamDexSiweHttpTransport", "build_siwe_http_transport_from_env",
]
