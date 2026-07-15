"""Strict, opt-in authenticated read-only DreamDEX transport.

This module intentionally does not implement SIWE, login, JWT creation, or
any mutation endpoint.  It only uses a pre-issued bearer token supplied by an
explicit environment gate and a fixed production host.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from decimal import Decimal
import json
import os
import re
from typing import Any, Mapping
from urllib.parse import quote, urlparse

from bot.integrations.dreamdex_auth_models import (
    AuthenticatedBalanceSnapshot,
    AuthenticatedSourceCollection,
    AuthenticatedSourceStatus,
)
from bot.integrations.dreamdex_order_metadata import (
    OrderMetadataKey,
    OrderMetadataLookupResult,
    OrderMetadataSourceStatus,
    RawOrderMetadata,
    normalize_order_metadata,
)


PRODUCTION_BASE_URL = "https://api.dreamdex.io/v0"
ENABLE_ENV = "DREAMDEX_ENABLE_AUTHENTICATED_READ_ONLY"
TOKEN_ENV = "DREAMDEX_READ_ONLY_BEARER_TOKEN"
MAX_RESPONSE_BODY_BYTES = 1_000_000
DEFAULT_CONNECT_TIMEOUT_SECONDS = 5.0
DEFAULT_READ_TIMEOUT_SECONDS = 10.0
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"", "0", "false", "no", "off"})
_SYMBOL_RE = re.compile(r"^[A-Za-z0-9_.-]{1,32}:[A-Za-z0-9_.-]{1,32}$")
_ORDER_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
_STATUS_VALUES = frozenset({"open", "filled", "cancelled", "canceled", "all", "pending", "expired"})


def _parse_enable_flag(value: Any) -> str:
    normalized = "" if value is None else str(value).strip().lower()
    if normalized in _TRUE_VALUES:
        return "enabled"
    if normalized in _FALSE_VALUES:
        return "disabled"
    return "invalid"


def _utc(value: Any = None) -> datetime:
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, (int, float)):
        result = datetime.fromtimestamp(float(value) / (1000 if value > 10_000_000_000 else 1), tz=timezone.utc)
    elif value:
        try:
            result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            result = datetime.now(timezone.utc)
    else:
        result = datetime.now(timezone.utc)
    return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result.astimezone(timezone.utc)


def _mask(value: Any) -> str:
    if not value:
        return "<missing>"
    text = str(value)
    return text if text.startswith("<") else ("***" if len(text) <= 8 else f"{text[:4]}...{text[-4:]}")


def _address(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.lower()
    if len(text) != 42 or not text.startswith("0x") or any(char not in "0123456789abcdef" for char in text[2:]):
        return None
    return text


def _sanitize(value: Any, *, secret: str | None = None) -> str:
    text = str(value)
    if secret:
        text = text.replace(secret, "<redacted-token>")
    text = re.sub(r"(?i)authorization\s*[:=]\s*[^,;\s]+", "Authorization=<redacted>", text)
    text = re.sub(r"(?i)bearer\s+[^,;\s]+", "Bearer <redacted>", text)
    text = re.sub(r"(?i)(cookie|set-cookie|nonce|signature|jwt)\s*[:=]\s*[^,;\s]+", r"\1=<redacted>", text)
    text = re.sub(r"\beyJ[a-zA-Z0-9_-]{12,}\.[a-zA-Z0-9_-]{4,}\.[a-zA-Z0-9_-]{4,}\b", "<redacted-jwt>", text)
    text = re.sub(r"0x[0-9a-fA-F]{40,}", "<redacted-hex>", text)
    return text[:240]


def _safe_json_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float, Decimal)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, Mapping):
        return "object"
    return type(value).__name__


_PAGINATION_NAME_PARTS = frozenset({"page", "pages", "cursor", "next", "offset", "limit", "total", "pagination", "continuation", "hasmore", "hasnext"})


def _is_pagination_name(name: str) -> bool:
    parts = {part for part in re.split(r"[^a-z0-9]+", name.lower()) if part}
    compact = "".join(parts)
    return bool(parts & _PAGINATION_NAME_PARTS) or any(token in compact for token in _PAGINATION_NAME_PARTS)


def _schema_structure(value: Any, *, max_depth: int = 3) -> tuple[tuple[str, ...], tuple[tuple[str, str], ...], tuple[str, ...], tuple[tuple[str, str], ...], tuple[tuple[str, int], ...], tuple[str, ...]]:
    """Return structure-only metadata; never retain payload values.

    Arrays contribute their length and are traversed through their first item
    only.  Nested traversal is bounded to three object/array levels.
    """
    top_names: list[str] = []
    top_types: list[tuple[str, str]] = []
    nested_names: list[str] = []
    nested_types: list[tuple[str, str]] = []
    list_lengths: list[tuple[str, int]] = []
    pagination_names: list[str] = []

    def walk(node: Any, path: str, depth: int, *, top: bool = False) -> None:
        if isinstance(node, list):
            list_lengths.append((path or "$", len(node)))
            if node and depth < max_depth:
                walk(node[0], path + "[]", depth + 1)
            return
        if not isinstance(node, Mapping) or depth > max_depth:
            return
        for raw_key in sorted(node.keys(), key=lambda item: str(item)):
            key = str(raw_key)
            item = node[raw_key]
            field_path = key if not path else f"{path}.{key}"
            kind = _safe_json_type(item)
            if top:
                top_names.append(key)
                top_types.append((key, kind))
            else:
                nested_names.append(field_path)
                nested_types.append((field_path, kind))
            if _is_pagination_name(key) or _is_pagination_name(field_path):
                pagination_names.append(field_path)
            if depth >= max_depth:
                continue
            if isinstance(item, (Mapping, list)):
                walk(item, field_path, depth + 1)

    if isinstance(value, list):
        list_lengths.append(("$", len(value)))
        if value:
            # The first array element is the only sample used for structure;
            # its fields are presented as the array's item schema.
            walk(value[0], "", 1, top=True)
    elif isinstance(value, Mapping):
        walk(value, "", 0, top=True)
    else:
        # Scalar values are represented only by their type in the caller.
        pass
    return (
        tuple(top_names), tuple(top_types), tuple(nested_names),
        tuple(nested_types), tuple(list_lengths), tuple(sorted(set(pagination_names))),
    )


@dataclass(frozen=True)
class AuthenticatedResponseSchemaFingerprint:
    endpoint_name: str
    top_level_type: str
    top_level_field_names: tuple[str, ...] = ()
    field_types: tuple[tuple[str, str], ...] = ()
    nested_field_names: tuple[str, ...] = ()
    nested_field_types: tuple[tuple[str, str], ...] = ()
    list_lengths: tuple[tuple[str, int], ...] = ()
    pagination_field_names: tuple[str, ...] = ()
    observed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    http_status: int | None = None

    @classmethod
    def from_payload(cls, endpoint_name: str, payload: Any, *, observed_at: datetime, http_status: int | None) -> "AuthenticatedResponseSchemaFingerprint":
        top_names, top_types, nested_names, nested_types, list_lengths, pagination_names = _schema_structure(payload)
        return cls(endpoint_name, _safe_json_type(payload), top_names, top_types, nested_names, nested_types, list_lengths, pagination_names, observed_at, http_status)

    # Compatibility aliases used by the first fingerprint implementation.
    @property
    def field_names(self) -> tuple[str, ...]:
        return self.top_level_field_names

    @property
    def nested_fields(self) -> tuple[tuple[str, str], ...]:
        return self.nested_field_types

    @property
    def list_length(self) -> int | None:
        return next((length for path, length in self.list_lengths if path == "$"), None)


@dataclass(frozen=True)
class AuthenticatedVaultBalanceResult:
    balances: tuple[AuthenticatedBalanceSnapshot, ...]
    source_status: AuthenticatedSourceStatus
    schema_fingerprint: AuthenticatedResponseSchemaFingerprint | None = None


class DreamDexAuthenticatedReadOnlyTransport:
    """Explicitly gated GET-only production transport.

    The class has no public generic request/get/post API.  It cannot be
    configured from an arbitrary URL and never performs authentication.
    """

    def __init__(self, *, base_url: str = PRODUCTION_BASE_URL, environ: Mapping[str, str] | None = None, connect_timeout_seconds: float = DEFAULT_CONNECT_TIMEOUT_SECONDS, read_timeout_seconds: float = DEFAULT_READ_TIMEOUT_SECONDS, max_response_body_bytes: int = MAX_RESPONSE_BODY_BYTES) -> None:
        self._validate_base_url(base_url)
        self._base_url = PRODUCTION_BASE_URL
        # Read only the two explicitly documented settings.  In particular,
        # do not enumerate or copy the process environment, which could
        # contain unrelated private-key or seed material.
        source = environ if environ is not None else os.environ
        raw_token = source.get(TOKEN_ENV, "")
        self._token = "" if raw_token is None else str(raw_token)
        flag_state = _parse_enable_flag(source.get(ENABLE_ENV))
        self._enabled = flag_state == "enabled"
        self._configuration_status = (
            "authenticated_configuration_invalid" if flag_state == "invalid"
            else "token_missing" if self._enabled and not self._token.strip()
            else "configured" if self._enabled
            else "unconfigured"
        )
        self._connect_timeout_seconds = connect_timeout_seconds
        self._read_timeout_seconds = read_timeout_seconds
        self._max_response_body_bytes = max_response_body_bytes

    @staticmethod
    def _validate_base_url(base_url: str) -> None:
        parsed = urlparse(str(base_url))
        expected = urlparse(PRODUCTION_BASE_URL)
        if parsed.scheme != "https" or parsed.hostname != expected.hostname or parsed.port is not None or parsed.username or parsed.password or parsed.path.rstrip("/") != expected.path.rstrip("/") or parsed.query or parsed.fragment:
            raise ValueError("authenticated read-only transport requires the pinned DreamDEX HTTPS host")

    @property
    def configured(self) -> bool:
        return self._enabled and bool(self._token.strip())

    @property
    def configuration_status(self) -> str:
        return self._configuration_status

    @property
    def request_execution_enabled(self) -> bool:
        return self.configured

    def __repr__(self) -> str:
        return f"{type(self).__name__}(configured={self.configured}, base_url={PRODUCTION_BASE_URL!r})"

    def _unconfigured_status(self, endpoint: str) -> AuthenticatedSourceStatus:
        if self._configuration_status == "authenticated_configuration_invalid":
            reason = code = "authenticated_configuration_invalid"
        elif self._configuration_status == "token_missing":
            reason = code = "authenticated_token_missing"
        else:
            reason = code = "authenticated_transport_unconfigured"
        return AuthenticatedSourceStatus("unavailable", endpoint, reason=reason, error_code=code, pagination_complete=False)

    @staticmethod
    def _validate_symbol(symbol: str) -> str:
        if not isinstance(symbol, str) or not _SYMBOL_RE.fullmatch(symbol) or any(char in symbol for char in ("/", "\\", "%", "?", "#", "&", "\r", "\n")):
            raise ValueError("invalid market symbol")
        return symbol

    @staticmethod
    def _validate_order_id(order_id: str) -> str:
        if not isinstance(order_id, str) or not _ORDER_ID_RE.fullmatch(order_id) or any(char in order_id for char in ("/", "\\", "%", "?", "#", "&", "\r", "\n")):
            raise ValueError("invalid order id")
        return order_id

    @staticmethod
    def _validate_status(status: str | None) -> str | None:
        if status is None:
            return None
        if status not in _STATUS_VALUES:
            raise ValueError("invalid order status")
        return status

    def _url(self, endpoint: str, symbol: str, order_id: str | None = None) -> str:
        if endpoint not in {"vault_balance", "order_by_id", "orders_page"}:
            raise ValueError("unsupported authenticated read-only endpoint")
        symbol = self._validate_symbol(symbol)
        path = f"/markets/{quote(symbol, safe=":")}/orders"
        if endpoint == "vault_balance":
            path = f"/markets/{quote(symbol, safe=":")}/vault/balance"
        elif endpoint == "order_by_id":
            path = f"{path}/{quote(self._validate_order_id(order_id or ""), safe="._-")}"
        return self._base_url + path

    def _perform_get(self, endpoint: str, symbol: str, *, order_id: str | None = None, params: Mapping[str, str] | None = None) -> tuple[int, Mapping[str, str], Any, str | None]:
        if not self.configured:
            raise RuntimeError("authenticated_transport_unconfigured" if not self._enabled else "authenticated_token_missing")
        import httpx
        response = httpx.get(
            self._url(endpoint, symbol, order_id),
            params=dict(params or {}),
            headers={"Accept": "application/json", "Authorization": f"Bearer {self._token}", "User-Agent": "DreamDEX-read-only/1.0"},
            cookies={},
            timeout=httpx.Timeout(self._read_timeout_seconds, connect=self._connect_timeout_seconds),
            follow_redirects=False,
        )
        content = response.content
        if response.status_code == 200 and (not content or not content.strip()):
            return response.status_code, response.headers, None, "empty_response"
        if len(content) > self._max_response_body_bytes:
            return response.status_code, response.headers, None, "response_too_large"
        content_type = str(response.headers.get("content-type", "")).lower()
        if response.status_code == 200 and "json" not in content_type:
            return response.status_code, response.headers, None, "malformed_json"
        if response.status_code in {301, 302, 303, 307, 308}:
            return response.status_code, response.headers, None, "redirect_blocked"
        if response.status_code != 200:
            return response.status_code, response.headers, None, None
        try:
            return response.status_code, response.headers, response.json(), None
        except (TypeError, ValueError, json.JSONDecodeError):
            return response.status_code, response.headers, None, "malformed_json"

    def fetch_vault_balance(self, symbol: str, wallet_address: str) -> AuthenticatedVaultBalanceResult:
        endpoint = "account_vault_balances"
        self._validate_symbol(symbol)
        if not _address(wallet_address):
            status = AuthenticatedSourceStatus("unavailable", endpoint, reason="invalid_wallet_address", error_code="invalid_target", pagination_complete=False)
            return AuthenticatedVaultBalanceResult((), status)
        if not self.configured:
            return AuthenticatedVaultBalanceResult((), self._unconfigured_status(endpoint))
        observed = datetime.now(timezone.utc)
        try:
            http_status, _, payload, error = self._perform_get("vault_balance", symbol, params={"walletAddress": wallet_address})
        except Exception as exc:
            status = AuthenticatedSourceStatus("unavailable", endpoint, observed, reason=_sanitize(exc, secret=self._token), error_code="transport_error", pagination_complete=False)
            return AuthenticatedVaultBalanceResult((), status)
        fingerprint = AuthenticatedResponseSchemaFingerprint.from_payload(endpoint, payload, observed_at=observed, http_status=http_status)
        if error:
            return AuthenticatedVaultBalanceResult((), AuthenticatedSourceStatus(error, endpoint, observed, reason=error, error_code=error, pagination_complete=False, response_body_status="absent" if error == "empty_response" else "present", schema_status=error, records_status="unavailable"), fingerprint)
        if http_status in {401, 403, 404, 429} or http_status >= 500:
            mapping = {401: ("unauthorized", "unauthorized"), 403: ("forbidden", "forbidden"), 404: ("not_found", "not_found"), 429: ("rate_limited", "rate_limited")}
            status_name, code = mapping.get(http_status, ("upstream_unavailable", "upstream_unavailable"))
            return AuthenticatedVaultBalanceResult((), AuthenticatedSourceStatus(status_name, endpoint, observed, error_code=code, pagination_complete=False), fingerprint)
        if not isinstance(payload, (Mapping, list)):
            status = AuthenticatedSourceStatus("unsupported_top_level_type", endpoint, observed, reason="unsupported_top_level_type", error_code="unsupported_top_level_type", pagination_complete=False, response_body_status="present", schema_status="unsupported_top_level_type", records_status="unavailable")
            return AuthenticatedVaultBalanceResult((), status, fingerprint)
        if not isinstance(payload, Mapping) or not isinstance(payload.get("balances"), list):
            status = AuthenticatedSourceStatus("available_but_unverified_schema", endpoint, observed, reason="unverified_vault_schema", error_code="unverified_schema", pagination_complete=False, response_body_status="present", schema_status="available_but_unverified_schema", records_status="unavailable")
            return AuthenticatedVaultBalanceResult((), status, fingerprint)
        balances: list[AuthenticatedBalanceSnapshot] = []
        malformed = 0
        conflicting_duplicates = False
        seen_amounts: dict[str, Decimal] = {}
        for row in payload["balances"]:
            if not isinstance(row, Mapping) or not isinstance(row.get("currency"), str) or not row.get("currency") or not isinstance(row.get("amount"), str):
                malformed += 1
                continue
            try:
                amount = Decimal(str(row["amount"]))
            except (InvalidOperation, TypeError, ValueError):
                malformed += 1
                continue
            currency = row["currency"]
            previous = seen_amounts.get(currency)
            if previous is not None and previous != amount:
                conflicting_duplicates = True
            seen_amounts[currency] = amount
            source = AuthenticatedSourceStatus("available", endpoint, observed, raw_status_name="200", pagination_complete=True)
            balances.append(AuthenticatedBalanceSnapshot(currency, amount, amount, None, _mask(wallet_address), source))
        # Preserve the existing vault ``available`` status for compatibility;
        # the stricter schema labels below are used for order-list diagnostics.
        status_name = "malformed_confirmed_field" if malformed or conflicting_duplicates else "available"
        status = AuthenticatedSourceStatus(status_name, endpoint, observed, raw_status_name="200", pagination_complete=True, malformed_count=malformed + (1 if conflicting_duplicates else 0), reason="conflicting_duplicate_currency" if conflicting_duplicates else "malformed_balance_record" if malformed else None, error_code="conflicting_duplicate_currency" if conflicting_duplicates else "malformed_record" if malformed else None, response_body_status="present", schema_status="valid_confirmed_schema", records_status="available" if balances else "available_empty", pagination_status="not_applicable", authority_status="source_available")
        if conflicting_duplicates:
            balances = []
        balances = [replace(item, source_status=status) for item in balances]
        return AuthenticatedVaultBalanceResult(tuple(balances), status, fingerprint)

    def fetch_order_by_id(self, symbol: str, order_id: str) -> OrderMetadataLookupResult:
        key = OrderMetadataKey(symbol, str(order_id))
        endpoint = "order_by_id"
        self._validate_symbol(symbol)
        self._validate_order_id(order_id)
        if not self.configured:
            reason = "authenticated_configuration_invalid" if self.configuration_status == "authenticated_configuration_invalid" else "authenticated_token_missing" if self.configuration_status == "token_missing" else "authenticated_transport_unconfigured"
            return OrderMetadataLookupResult(key, None, OrderMetadataSourceStatus("unconfigured", endpoint, reason=reason, error_code=reason, pagination_complete=False))
        observed = datetime.now(timezone.utc)
        try:
            http_status, _, payload, error = self._perform_get(endpoint, symbol, order_id=order_id)
        except Exception as exc:
            return OrderMetadataLookupResult(key, None, OrderMetadataSourceStatus("unavailable", endpoint, observed, reason=_sanitize(exc, secret=self._token), error_code="transport_error", pagination_complete=False))
        fingerprint = AuthenticatedResponseSchemaFingerprint.from_payload(endpoint, payload, observed_at=observed, http_status=http_status)
        if error:
            return OrderMetadataLookupResult(key, None, OrderMetadataSourceStatus(error, endpoint, observed, reason=error, error_code=error, pagination_complete=False, schema_fingerprint=fingerprint))
        if http_status == 401:
            return OrderMetadataLookupResult(key, None, OrderMetadataSourceStatus("unauthorized", endpoint, observed, error_code="unauthorized", pagination_complete=False, schema_fingerprint=fingerprint))
        if http_status == 403:
            return OrderMetadataLookupResult(key, None, OrderMetadataSourceStatus("forbidden", endpoint, observed, error_code="forbidden", pagination_complete=False, schema_fingerprint=fingerprint))
        if http_status == 404:
            return OrderMetadataLookupResult(key, None, OrderMetadataSourceStatus("not_found", endpoint, observed, error_code="not_found", pagination_complete=False, schema_fingerprint=fingerprint))
        if http_status == 429:
            return OrderMetadataLookupResult(key, None, OrderMetadataSourceStatus("rate_limited", endpoint, observed, error_code="rate_limited", pagination_complete=False, schema_fingerprint=fingerprint))
        if http_status >= 500:
            return OrderMetadataLookupResult(key, None, OrderMetadataSourceStatus("upstream_unavailable", endpoint, observed, error_code="upstream_unavailable", pagination_complete=False, schema_fingerprint=fingerprint))
        if not isinstance(payload, Mapping):
            return OrderMetadataLookupResult(key, None, OrderMetadataSourceStatus("unsupported_top_level_type", endpoint, observed, reason="unsupported_top_level_type", error_code="unsupported_top_level_type", pagination_complete=False, schema_fingerprint=fingerprint))
        raw = RawOrderMetadata(key, payload, "GET /markets/{symbol}/orders/{orderId}", observed)
        normalized = normalize_order_metadata(raw)
        if normalized.order_id != str(order_id):
            status = OrderMetadataSourceStatus("available_but_unverified_schema", endpoint, observed, reason="unverified_order_schema", error_code="unverified_schema", pagination_complete=False, malformed_count=1, schema_fingerprint=fingerprint)
            return OrderMetadataLookupResult(key, replace(normalized, source_available=False), status)
        status = OrderMetadataSourceStatus("malformed_confirmed_field" if normalized.malformed else "valid_confirmed_schema", endpoint, observed, raw_status_name=str(payload.get("status")) if payload.get("status") is not None else None, pagination_complete=True, malformed_count=len(normalized.malformed_fields), reason="malformed_order_record" if normalized.malformed else None, error_code="malformed_record" if normalized.malformed else None, schema_fingerprint=fingerprint)
        return OrderMetadataLookupResult(key, replace(normalized, source_available=status.available), status)

    def fetch_orders_page(self, symbol: str, status: str | None = None, cursor: str | None = None) -> tuple[tuple[RawOrderMetadata, ...], OrderMetadataSourceStatus]:
        endpoint = "orders_page"
        self._validate_symbol(symbol)
        self._validate_status(status)
        if cursor is not None:
            return (), OrderMetadataSourceStatus("unavailable", endpoint, reason="pagination_not_confirmed", error_code="pagination_not_confirmed", pagination_complete=False)
        if not self.configured:
            reason = "authenticated_configuration_invalid" if self.configuration_status == "authenticated_configuration_invalid" else "authenticated_token_missing" if self.configuration_status == "token_missing" else "authenticated_transport_unconfigured"
            return (), OrderMetadataSourceStatus("unconfigured", endpoint, reason=reason, error_code=reason, pagination_complete=False)
        observed = datetime.now(timezone.utc)
        try:
            http_status, _, payload, error = self._perform_get("orders_page", symbol, params={"status": status} if status else None)
        except Exception as exc:
            return (), OrderMetadataSourceStatus("unavailable", endpoint, observed, reason=_sanitize(exc, secret=self._token), error_code="transport_error", pagination_complete=False)
        fingerprint = AuthenticatedResponseSchemaFingerprint.from_payload(endpoint, payload, observed_at=observed, http_status=http_status)
        if error:
            return (), OrderMetadataSourceStatus(error, endpoint, observed, reason=error, error_code=error, pagination_complete=False, schema_fingerprint=fingerprint, response_body_status="absent" if error == "empty_response" else "present", schema_status=error, records_status="unavailable")
        status_map = {401: ("unauthorized", "unauthorized"), 403: ("forbidden", "forbidden"), 404: ("not_found", "not_found"), 429: ("rate_limited", "rate_limited")}
        if http_status in status_map or http_status >= 500:
            name, code = status_map.get(http_status, ("upstream_unavailable", "upstream_unavailable"))
            return (), OrderMetadataSourceStatus(name, endpoint, observed, error_code=code, pagination_complete=False, schema_fingerprint=fingerprint, response_body_status="present", schema_status=name, records_status="unavailable")
        if not isinstance(payload, (list, Mapping)):
            return (), OrderMetadataSourceStatus("unsupported_top_level_type", endpoint, observed, reason="unsupported_top_level_type", error_code="unsupported_top_level_type", pagination_complete=False, schema_fingerprint=fingerprint, response_body_status="present", schema_status="unsupported_top_level_type", records_status="unavailable")
        if isinstance(payload, Mapping) and "orders" in payload and not isinstance(payload.get("orders"), list):
            return (), OrderMetadataSourceStatus("malformed_confirmed_field", endpoint, observed, reason="orders_must_be_array", error_code="malformed_confirmed_field", pagination_complete=False, schema_fingerprint=fingerprint, response_body_status="present", schema_status="malformed_confirmed_field", records_status="unavailable")
        rows = payload if isinstance(payload, list) else payload.get("orders") if isinstance(payload, Mapping) else None
        if not isinstance(rows, list):
            return (), OrderMetadataSourceStatus("available_but_unverified_schema", endpoint, observed, reason="unverified_order_list_schema", error_code="unverified_schema", pagination_complete=False, schema_fingerprint=fingerprint, response_body_status="present", schema_status="available_but_unverified_schema", records_status="unavailable")
        if not rows:
            return (), OrderMetadataSourceStatus("valid_confirmed_schema", endpoint, observed, raw_status_name="200", reason="no_open_orders", error_code=None, pagination_complete=False, schema_fingerprint=fingerprint, response_body_status="present", schema_status="valid_confirmed_schema", records_status="available_empty", pagination_status="unresolved", authority_status="non_authoritative")
        malformed = 0
        records_list: list[RawOrderMetadata] = []
        for row in rows:
            if not isinstance(row, Mapping):
                malformed += 1
                continue
            order_id = row.get("orderId", row.get("id"))
            if order_id is None or str(order_id) == "":
                malformed += 1
                continue
            records_list.append(RawOrderMetadata(OrderMetadataKey(symbol, str(order_id)), row, "GET /markets/{symbol}/orders", observed))
        records = tuple(records_list)
        status_name = "malformed_confirmed_field" if malformed else "valid_confirmed_schema"
        return records, OrderMetadataSourceStatus(status_name, endpoint, observed, raw_status_name="200", pagination_complete=False, malformed_count=malformed, reason="pagination_not_confirmed", error_code="pagination_not_confirmed", schema_fingerprint=fingerprint, response_body_status="present", schema_status="valid_confirmed_schema", records_status="malformed" if malformed else "available", pagination_status="unresolved", authority_status="non_authoritative")


__all__ = [
    "PRODUCTION_BASE_URL", "ENABLE_ENV", "TOKEN_ENV", "MAX_RESPONSE_BODY_BYTES",
    "AuthenticatedResponseSchemaFingerprint", "AuthenticatedVaultBalanceResult",
    "DreamDexAuthenticatedReadOnlyTransport", "build_authenticated_read_only_transport_from_env",
]


def build_authenticated_read_only_transport_from_env(environ: Mapping[str, str] | None = None):
    """Build the opt-in transport without exposing or logging the bearer token."""
    source = environ if environ is not None else os.environ
    flag_state = _parse_enable_flag(source.get(ENABLE_ENV))
    if flag_state == "disabled":
        from bot.integrations.dreamdex_auth_models import UnconfiguredAuthenticatedReadOnlyTransport
        return UnconfiguredAuthenticatedReadOnlyTransport()
    # Invalid and token-missing states are represented by the strict object,
    # which performs no I/O while retaining a precise configuration status.
    return DreamDexAuthenticatedReadOnlyTransport(environ=source)
