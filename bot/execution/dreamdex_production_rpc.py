"""Disarmed, typed production-RPC adapters.

The existing read-only JSON-RPC transport remains the only HTTP primitive.  This
module adds policy validation and a hard mutation gate around its raw-send and
recovery APIs.  Construction is side-effect free; the default policy cannot
dispatch a mutation request.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import json
from typing import Any, Mapping

from bot.execution.dreamdex_execution_primitives import mask_evm_address, mask_hex_hash
from bot.execution.dreamdex_readonly_rpc import DreamDexReadOnlyRpcTransport, DreamDexRpcError
from bot.execution.dreamdex_transaction_submission import (
    DreamDexRawTransactionSubmitter,
    DreamDexRawTransactionHttpSubmitter,
    DreamDexTransactionRecoveryReader,
    DreamDexTransactionByHashHttpReader,
)
from bot.execution.dreamdex_signed_transaction import DreamDexEphemeralSignedTransaction
from bot.execution.dreamdex_signed_transaction import decode_signed_transaction
from bot.execution.dreamdex_transaction_submission import DreamDexRawTransactionSubmissionResponse

SCHEMA_VERSION = "1"
RPC_CONFIGURATION_STATUSES = frozenset({"unavailable", "configured", "test_confirmed", "source_confirmed"})


def _tuple(values: Any) -> tuple[str, ...]:
    return tuple(dict.fromkeys(str(item) for item in (values or ()) if str(item)))


@dataclass(frozen=True, repr=False)
class DreamDexProductionRpcPolicy:
    schema_version: str = SCHEMA_VERSION
    required_chain_id: int = 5031
    rpc_configuration_status: str = "unavailable"
    maximum_request_bytes: int = 1_048_576
    maximum_response_bytes: int = 1_048_576
    connect_timeout_ms: int = 5_000
    read_timeout_ms: int = 10_000
    allow_redirects: bool = False
    automatic_retry_allowed: bool = False
    maximum_submission_attempts: int = 1
    allow_mutation_rpc: bool = False
    allow_receipt_reads: bool = True
    allow_recovery_reads: bool = True
    authoritative: bool = False
    unresolved_reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError("production_rpc_policy_schema_invalid")
        if isinstance(self.required_chain_id, bool) or not isinstance(self.required_chain_id, int) or self.required_chain_id != 5031:
            raise ValueError("production_rpc_chain_id_invalid")
        if self.rpc_configuration_status not in RPC_CONFIGURATION_STATUSES:
            raise ValueError("rpc_configuration_status_invalid")
        for name in ("maximum_request_bytes", "maximum_response_bytes", "connect_timeout_ms", "read_timeout_ms"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name}_invalid")
        if isinstance(self.maximum_submission_attempts, bool) or not isinstance(self.maximum_submission_attempts, int) or self.maximum_submission_attempts != 1:
            raise ValueError("maximum_submission_attempts_must_be_one")
        if self.allow_redirects or self.automatic_retry_allowed:
            raise ValueError("rpc_redirects_or_retry_disabled")
        object.__setattr__(self, "allow_mutation_rpc", bool(self.allow_mutation_rpc))
        object.__setattr__(self, "authoritative", False)
        object.__setattr__(self, "unresolved_reasons", _tuple(self.unresolved_reasons))

    @property
    def complete(self) -> bool:
        return self.rpc_configuration_status in {"configured", "test_confirmed", "source_confirmed"} and not self.unresolved_reasons

    def safe_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "required_chain_id": self.required_chain_id,
            "rpc_configuration_status": self.rpc_configuration_status,
            "maximum_request_bytes": self.maximum_request_bytes,
            "maximum_response_bytes": self.maximum_response_bytes,
            "connect_timeout_ms": self.connect_timeout_ms,
            "read_timeout_ms": self.read_timeout_ms,
            "allow_redirects": False,
            "automatic_retry_allowed": False,
            "maximum_submission_attempts": 1,
            "allow_mutation_rpc": self.allow_mutation_rpc,
            "allow_receipt_reads": self.allow_receipt_reads,
            "allow_recovery_reads": self.allow_recovery_reads,
            "authoritative": False,
            "complete": self.complete,
            "unresolved_reasons": self.unresolved_reasons,
        }

    def __repr__(self) -> str:
        return f"DreamDexProductionRpcPolicy(chain_id=5031, mutation_rpc={self.allow_mutation_rpc!r}, retries=False, redirects=False)"


class HttpDreamDexRawTransactionSubmitter:
    """Typed, single-attempt raw-send adapter with a default disarm."""

    ALLOWED_RPC_METHOD = "eth_sendRawTransaction"
    ALLOWED_RPC_METHODS = frozenset({ALLOWED_RPC_METHOD})
    RPC_METHOD_ALLOWLIST = ALLOWED_RPC_METHODS

    def __init__(self, rpc_url: str, *, policy: DreamDexProductionRpcPolicy | None = None, http_client: Any = None) -> None:
        self.policy = policy or DreamDexProductionRpcPolicy()
        self._delegate = DreamDexRawTransactionHttpSubmitter(
            rpc_url,
            timeout_seconds=max(self.policy.connect_timeout_ms, self.policy.read_timeout_ms) / 1000,
            max_response_body_bytes=self.policy.maximum_response_bytes,
            maximum_signed_payload_bytes=self.policy.maximum_request_bytes,
            http_client=http_client,
        )
        self._invocations = 0

    @property
    def invocation_count(self) -> int:
        return self._invocations

    def describe_capabilities(self) -> Mapping[str, str]:
        return {
            "eth_sendRawTransaction": "partial" if self.policy.allow_mutation_rpc else "disabled",
            "mutation_methods": "available_opt_in_only" if self.policy.allow_mutation_rpc else "disabled",
            "arbitrary_rpc": "unavailable",
            "automatic_retry": "unavailable",
            "redirects": "disabled",
        }

    def submit_raw_transaction(self, ephemeral_signed_transaction: DreamDexEphemeralSignedTransaction) -> DreamDexRawTransactionSubmissionResponse:
        if not isinstance(ephemeral_signed_transaction, DreamDexEphemeralSignedTransaction):
            raise TypeError("typed_ephemeral_signed_transaction_required")
        if not self.policy.allow_mutation_rpc:
            raise DreamDexRpcError("rpc_mutation_disabled")
        if not self.policy.complete:
            raise DreamDexRpcError("rpc_policy_incomplete")
        if self._invocations >= 1:
            raise DreamDexRpcError("submission_attempt_limit_reached")
        raw = bytes(ephemeral_signed_transaction.raw_signed_transaction)
        if not raw or len(raw) > self.policy.maximum_request_bytes:
            raise ValueError("signed_payload_size_invalid")
        # Bound the complete JSON-RPC request envelope as well as the typed
        # signed payload.  The payload is never persisted or included in an
        # exception/diagnostic.
        request_bytes = len(json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": self.ALLOWED_RPC_METHOD,
            "params": ["0x" + raw.hex()],
        }, separators=(",", ":")).encode("utf-8"))
        if request_bytes > self.policy.maximum_request_bytes:
            raise ValueError("rpc_request_size_limit")
        self._invocations += 1
        # The legacy adapter performs exactly one allowlisted call and returns
        # only sanitized typed response metadata.
        response = self._delegate.submit_raw_transaction(ephemeral_signed_transaction)
        try:
            decoded = decode_signed_transaction(bytes(ephemeral_signed_transaction.raw_signed_transaction))
            local_hash = decoded.signed_transaction_hash
        except Exception:
            raise DreamDexRpcError("signed_payload_hash_unavailable") from None
        return replace(response, locally_calculated_transaction_hash=local_hash, exact_hash_match=(response.rpc_returned_transaction_hash == local_hash if response.rpc_returned_transaction_hash else None))


class HttpDreamDexTransactionRecoveryReader:
    """One-shot, read-only transaction lookup adapter."""

    ALLOWED_RPC_METHOD = "eth_getTransactionByHash"
    ALLOWED_RPC_METHODS = frozenset({ALLOWED_RPC_METHOD})
    RPC_METHOD_ALLOWLIST = ALLOWED_RPC_METHODS

    def __init__(self, rpc_url: str, *, policy: DreamDexProductionRpcPolicy | None = None, http_client: Any = None) -> None:
        self.policy = policy or DreamDexProductionRpcPolicy()
        self._delegate = DreamDexTransactionByHashHttpReader(
            rpc_url,
            timeout_seconds=max(self.policy.connect_timeout_ms, self.policy.read_timeout_ms) / 1000,
            max_response_body_bytes=self.policy.maximum_response_bytes,
            http_client=http_client,
        )
        self._lookups = 0

    @property
    def lookup_count(self) -> int:
        return self._lookups

    def describe_capabilities(self) -> Mapping[str, str]:
        return {"eth_getTransactionByHash": "partial" if self.policy.allow_recovery_reads else "disabled", "polling": "unavailable", "arbitrary_rpc": "unavailable"}

    def get_transaction_by_hash(self, transaction_hash: str) -> Mapping[str, Any] | None:
        if not self.policy.allow_recovery_reads:
            raise DreamDexRpcError("rpc_recovery_reads_disabled")
        if not self.policy.complete:
            raise DreamDexRpcError("rpc_policy_incomplete")
        if self._lookups >= 1:
            raise DreamDexRpcError("recovery_lookup_attempt_limit_reached")
        self._lookups += 1
        return self._delegate.get_transaction_by_hash(transaction_hash)


def build_production_rpc_policy(**kwargs: Any) -> DreamDexProductionRpcPolicy:
    return DreamDexProductionRpcPolicy(**kwargs)


# Discoverable aliases; no second transport implementation is introduced.
DreamDexRawTransactionSubmitterHttp = HttpDreamDexRawTransactionSubmitter
DreamDexReadOnlyTransactionRecoveryHttpReader = HttpDreamDexTransactionRecoveryReader
DreamDexProductionReadOnlyRpcTransport = DreamDexReadOnlyRpcTransport

__all__ = [
    "SCHEMA_VERSION", "RPC_CONFIGURATION_STATUSES", "DreamDexProductionRpcPolicy",
    "DreamDexRawTransactionSubmitter", "DreamDexTransactionRecoveryReader",
    "HttpDreamDexRawTransactionSubmitter", "HttpDreamDexTransactionRecoveryReader",
    "DreamDexRawTransactionSubmitterHttp", "DreamDexReadOnlyTransactionRecoveryHttpReader",
    "DreamDexProductionReadOnlyRpcTransport",
    "build_production_rpc_policy",
]
