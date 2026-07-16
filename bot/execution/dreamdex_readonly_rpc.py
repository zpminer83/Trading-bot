"""Strict, typed, read-only JSON-RPC transport for transaction preflight.

Only the methods in ``ALLOWED_RPC_METHODS`` can reach the network.  The
public API intentionally exposes typed methods rather than a caller supplied
``call(method, params)`` escape hatch.  This module never signs or submits.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any, Mapping, Protocol, Sequence
from urllib.parse import urlsplit

from bot.execution.dreamdex_unsigned_transaction import MAX_UINT256

ALLOWED_RPC_METHODS = frozenset({
    "eth_chainId", "eth_getCode", "eth_getTransactionCount", "eth_estimateGas",
    "eth_getBlockByNumber", "eth_gasPrice", "eth_maxPriorityFeePerGas",
    "eth_feeHistory", "eth_getBalance",
})
RPC_METHOD_ALLOWLIST = ALLOWED_RPC_METHODS
MAX_RESPONSE_BODY_BYTES = 1_000_000
DEFAULT_RPC_TIMEOUT_SECONDS = 10.0
_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_HEX_RE = re.compile(r"^0x[0-9a-fA-F]+$")


class DreamDexRpcError(RuntimeError):
    """Sanitized RPC failure; raw response bodies are never retained."""

    def __init__(self, category: str, reason: str = "rpc_unavailable") -> None:
        self.category = category
        self.reason = str(reason)[:160]
        super().__init__(f"{category}:{self.reason}")


def _validate_address(address: Any, field: str = "address") -> str:
    if not isinstance(address, str) or not _ADDRESS_RE.fullmatch(address):
        raise ValueError(f"{field}: invalid_address")
    return address.lower()


def parse_rpc_quantity(value: Any, *, field: str = "quantity", maximum: int = MAX_UINT256) -> int:
    """Parse canonical JSON-RPC hex quantity, rejecting bool/decimal/overflow."""
    if not isinstance(value, str) or not _HEX_RE.fullmatch(value):
        raise ValueError(f"{field}: malformed_hex_quantity")
    body = value[2:]
    if not body or (len(body) > 1 and body[0] == "0"):
        raise ValueError(f"{field}: non_canonical_hex_quantity")
    try:
        parsed = int(body, 16)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{field}: malformed_hex_quantity") from exc
    if parsed < 0 or parsed > maximum:
        raise ValueError(f"{field}: uint256_overflow")
    return parsed


def quantity_hex(value: Any, *, field: str = "quantity") -> str:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > MAX_UINT256:
        raise ValueError(f"{field}: invalid_uint256")
    return hex(value)


def _code_result(value: Any) -> str:
    if value in {"0x", "0x0"}:
        return str(value)
    if not isinstance(value, str) or not value.startswith("0x") or len(value) % 2:
        raise ValueError("target_code: malformed_hex")
    body = value[2:]
    if body and any(char not in "0123456789abcdefABCDEF" for char in body):
        raise ValueError("target_code: malformed_hex")
    return value.lower()


def _hex_data(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.startswith("0x") or len(value) % 2 or any(char not in "0123456789abcdefABCDEF" for char in value[2:]):
        raise ValueError(f"{field}: malformed_hex")
    return value.lower()


def validate_rpc_response(payload: Any, expected_id: int) -> Any:
    """Validate JSON-RPC envelope and return only its result value."""
    if not isinstance(payload, Mapping) or payload.get("jsonrpc") != "2.0" or payload.get("id") != expected_id:
        raise DreamDexRpcError("invalid_jsonrpc_envelope")
    has_result = "result" in payload
    has_error = "error" in payload
    if has_result == has_error:
        raise DreamDexRpcError("result_error_conflict")
    if has_error:
        raise DreamDexRpcError("rpc_error")
    return payload["result"]


@dataclass(frozen=True, repr=False)
class DreamDexRpcFeeBlock:
    latest_block_number: int | None
    base_fee_per_gas_wei: int | None
    source_status: str
    unresolved_reasons: tuple[str, ...] = ()

    def __repr__(self) -> str:
        return f"DreamDexRpcFeeBlock(block={self.latest_block_number!r}, base_fee_present={self.base_fee_per_gas_wei is not None}, source_status={self.source_status!r})"

    def safe_dict(self) -> dict[str, Any]:
        return {"latest_block_number": self.latest_block_number, "base_fee_present": self.base_fee_per_gas_wei is not None, "source_status": self.source_status, "unresolved_reasons": self.unresolved_reasons}


class DreamDexReadOnlyRpc(Protocol):
    def get_chain_id(self) -> int: ...
    def get_contract_code(self, address: str) -> str: ...
    def get_pending_nonce(self, address: str) -> int: ...
    def estimate_gas(self, call: Mapping[str, str]) -> int: ...
    def get_latest_block_fee_evidence(self) -> DreamDexRpcFeeBlock: ...
    def get_gas_price(self) -> int: ...
    def get_max_priority_fee_per_gas(self) -> int: ...
    def get_fee_history(self, block_count: int = 1, newest_block: str = "latest", reward_percentiles: Sequence[int] = ()) -> Mapping[str, Any]: ...
    def get_native_balance(self, address: str) -> int: ...


class DreamDexReadOnlyRpcTransport:
    """HTTP JSON-RPC implementation with a private request primitive."""

    ALLOWED_METHODS = ALLOWED_RPC_METHODS

    def __init__(self, rpc_url: str, *, timeout_seconds: float = DEFAULT_RPC_TIMEOUT_SECONDS, max_response_body_bytes: int = MAX_RESPONSE_BODY_BYTES, http_client: Any = None) -> None:
        parts = urlsplit(str(rpc_url))
        if parts.scheme not in {"http", "https"} or not parts.netloc:
            raise ValueError("rpc_url: invalid")
        if timeout_seconds <= 0 or max_response_body_bytes <= 0:
            raise ValueError("rpc transport limits must be positive")
        self._rpc_url = str(rpc_url)
        self._timeout = float(timeout_seconds)
        self._max_response_body_bytes = int(max_response_body_bytes)
        self._http_client = http_client
        self._request_id = 0

    @property
    def rpc_url_configured(self) -> bool:
        return bool(self._rpc_url)

    def _post(self, payload: Mapping[str, Any]) -> Any:
        client = self._http_client
        try:
            if client is None:
                import httpx
                response = httpx.post(self._rpc_url, json=dict(payload), headers={"Accept": "application/json", "Content-Type": "application/json"}, timeout=self._timeout, follow_redirects=False, trust_env=False)
            else:
                response = client.post(self._rpc_url, json=dict(payload), headers={"Accept": "application/json", "Content-Type": "application/json"}, timeout=self._timeout, follow_redirects=False, trust_env=False)
        except Exception as exc:
            category = "timeout" if "timeout" in str(exc).lower() else "transport_unavailable"
            raise DreamDexRpcError(category) from None
        status = getattr(response, "status_code", None)
        body = getattr(response, "content", b"")
        if isinstance(body, str):
            body = body.encode("utf-8", "replace")
        if len(body) > self._max_response_body_bytes:
            raise DreamDexRpcError("response_too_large")
        if status is None or not 200 <= int(status) < 300:
            raise DreamDexRpcError("http_error")
        try:
            payload_obj = response.json() if hasattr(response, "json") else json.loads(body.decode("utf-8"))
        except Exception as exc:
            raise DreamDexRpcError("malformed_json") from exc
        return payload_obj

    def _call(self, method: str, params: Sequence[Any]) -> Any:
        if method not in self.ALLOWED_METHODS:
            raise ValueError("rpc_method_not_allowlisted")
        self._request_id += 1
        request_id = self._request_id
        payload = self._post({"jsonrpc": "2.0", "id": request_id, "method": method, "params": list(params)})
        return validate_rpc_response(payload, request_id)

    def get_chain_id(self) -> int:
        return parse_rpc_quantity(self._call("eth_chainId", []), field="chain_id")

    def get_contract_code(self, address: str) -> str:
        return _code_result(self._call("eth_getCode", [_validate_address(address), "latest"]))

    def get_pending_nonce(self, address: str) -> int:
        return parse_rpc_quantity(self._call("eth_getTransactionCount", [_validate_address(address), "pending"]), field="pending_nonce", maximum=(1 << 64) - 1)

    def estimate_gas(self, call: Mapping[str, str]) -> int:
        if not isinstance(call, Mapping) or set(call) != {"from", "to", "value", "data"}:
            raise ValueError("estimate_call_fields_invalid")
        normalized = {"from": _validate_address(call["from"], "from"), "to": _validate_address(call["to"], "to"), "value": quantity_hex(parse_rpc_quantity(call["value"], field="value"), field="value"), "data": _hex_data(call["data"], "data")}
        return parse_rpc_quantity(self._call("eth_estimateGas", [normalized]), field="gas_estimate")

    def get_latest_block_fee_evidence(self) -> DreamDexRpcFeeBlock:
        value = self._call("eth_getBlockByNumber", ["latest", False])
        if not isinstance(value, Mapping):
            raise DreamDexRpcError("malformed_block")
        number = parse_rpc_quantity(value.get("number"), field="block_number")
        raw_base = value.get("baseFeePerGas")
        if raw_base is None:
            return DreamDexRpcFeeBlock(number, None, "legacy_candidate")
        try:
            base = parse_rpc_quantity(raw_base, field="base_fee_per_gas")
        except ValueError as exc:
            raise DreamDexRpcError("malformed_fee_evidence") from exc
        return DreamDexRpcFeeBlock(number, base, "eip1559_candidate")

    def get_gas_price(self) -> int:
        return parse_rpc_quantity(self._call("eth_gasPrice", []), field="gas_price")

    def get_max_priority_fee_per_gas(self) -> int:
        return parse_rpc_quantity(self._call("eth_maxPriorityFeePerGas", []), field="priority_fee")

    def get_fee_history(self, block_count: int = 1, newest_block: str = "latest", reward_percentiles: Sequence[int] = ()) -> Mapping[str, Any]:
        if isinstance(block_count, bool) or not isinstance(block_count, int) or block_count <= 0 or block_count > 1024:
            raise ValueError("fee_history_block_count_invalid")
        if not isinstance(newest_block, str):
            raise ValueError("fee_history_newest_block_invalid")
        if any(isinstance(item, bool) or not isinstance(item, int) or item < 0 or item > 100 for item in reward_percentiles):
            raise ValueError("fee_history_percentile_invalid")
        value = self._call("eth_feeHistory", [hex(block_count), newest_block, list(reward_percentiles)])
        if not isinstance(value, Mapping):
            raise DreamDexRpcError("malformed_fee_history")
        return value

    def get_native_balance(self, address: str) -> int:
        return parse_rpc_quantity(self._call("eth_getBalance", [_validate_address(address), "latest"]), field="native_balance")


class FixtureDreamDexReadOnlyRpcTransport:
    """Deterministic typed fixture transport for offline tests."""

    def __init__(self, responses: Mapping[str, Any]) -> None:
        self.responses = dict(responses)
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def _value(self, method: str) -> Any:
        if method not in self.responses:
            raise DreamDexRpcError("rpc_unavailable")
        value = self.responses[method]
        if isinstance(value, Exception):
            raise value
        return value

    def get_chain_id(self) -> int:
        self.calls.append(("eth_chainId", ()))
        return parse_rpc_quantity(self._value("eth_chainId"), field="chain_id")

    def get_contract_code(self, address: str) -> str:
        _validate_address(address)
        self.calls.append(("eth_getCode", (address, "latest")))
        return _code_result(self._value("eth_getCode"))

    def get_pending_nonce(self, address: str) -> int:
        _validate_address(address)
        self.calls.append(("eth_getTransactionCount", (address, "pending")))
        return parse_rpc_quantity(self._value("eth_getTransactionCount"), field="pending_nonce", maximum=(1 << 64) - 1)

    def estimate_gas(self, call: Mapping[str, str]) -> int:
        if not isinstance(call, Mapping) or set(call) != {"from", "to", "value", "data"}:
            raise ValueError("estimate_call_fields_invalid")
        _validate_address(call["from"], "from")
        _validate_address(call["to"], "to")
        quantity_hex(parse_rpc_quantity(call["value"], field="value"), field="value")
        _hex_data(call["data"], "data")
        self.calls.append(("eth_estimateGas", (dict(call),)))
        return parse_rpc_quantity(self._value("eth_estimateGas"), field="gas_estimate")

    def get_latest_block_fee_evidence(self) -> DreamDexRpcFeeBlock:
        self.calls.append(("eth_getBlockByNumber", ("latest", False)))
        value = self._value("eth_getBlockByNumber")
        if not isinstance(value, Mapping):
            raise DreamDexRpcError("malformed_block")
        base = value.get("baseFeePerGas")
        return DreamDexRpcFeeBlock(parse_rpc_quantity(value.get("number"), field="block_number"), None if base is None else parse_rpc_quantity(base, field="base_fee_per_gas"), "eip1559_candidate" if base is not None else "legacy_candidate")

    def get_gas_price(self) -> int:
        self.calls.append(("eth_gasPrice", ()))
        return parse_rpc_quantity(self._value("eth_gasPrice"), field="gas_price")

    def get_max_priority_fee_per_gas(self) -> int:
        self.calls.append(("eth_maxPriorityFeePerGas", ()))
        return parse_rpc_quantity(self._value("eth_maxPriorityFeePerGas"), field="priority_fee")

    def get_fee_history(self, block_count: int = 1, newest_block: str = "latest", reward_percentiles: Sequence[int] = ()) -> Mapping[str, Any]:
        self.calls.append(("eth_feeHistory", (hex(block_count), newest_block, tuple(reward_percentiles))))
        value = self._value("eth_feeHistory")
        if not isinstance(value, Mapping):
            raise DreamDexRpcError("malformed_fee_history")
        return value

    def get_native_balance(self, address: str) -> int:
        _validate_address(address)
        self.calls.append(("eth_getBalance", (address, "latest")))
        return parse_rpc_quantity(self._value("eth_getBalance"), field="native_balance")


# Naming aliases keep the public surface discoverable without introducing a
# second implementation or a generic caller-controlled RPC method.
HttpDreamDexReadOnlyRpcTransport = DreamDexReadOnlyRpcTransport
DreamDexRpcTransport = DreamDexReadOnlyRpcTransport
FixtureReadOnlyRpcTransport = FixtureDreamDexReadOnlyRpcTransport
DreamDexLatestBlockFeeEvidence = DreamDexRpcFeeBlock


__all__ = [
    "ALLOWED_RPC_METHODS", "RPC_METHOD_ALLOWLIST", "MAX_RESPONSE_BODY_BYTES", "DreamDexRpcError",
    "DreamDexRpcFeeBlock", "DreamDexReadOnlyRpc", "DreamDexReadOnlyRpcTransport",
    "FixtureDreamDexReadOnlyRpcTransport", "HttpDreamDexReadOnlyRpcTransport", "DreamDexRpcTransport", "FixtureReadOnlyRpcTransport", "DreamDexLatestBlockFeeEvidence", "validate_rpc_response", "parse_rpc_quantity", "quantity_hex",
]
