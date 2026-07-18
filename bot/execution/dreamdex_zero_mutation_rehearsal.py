"""Offline, fail-closed rehearsal of a single DreamDEX place-order candidate.

This module deliberately stops before production journal, signing, nonce lease,
prompt, keystore, and submission.  A caller may provide an explicit typed
read-only collector for evidence; no collector is invoked by default.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
import time
from types import MappingProxyType
from typing import Any, Callable, Mapping, Protocol

from .dreamdex_execution_primitives import mask_evm_address, mask_hex_hash, sha256_hex, validate_evm_address

ADDRESS_ROLE_SOURCE_NAMES = frozenset({
    "dedicated_owner_environment",
    "dedicated_trading_environment",
    "authoritative_public_market_evidence",
    "explicit_typed_test_fixture",
    "unavailable",
})
ADDRESS_ROLE_BINDING_STATUSES = frozenset({"confirmed", "unavailable", "role_conflict"})


@dataclass(frozen=True, repr=False)
class DreamDexLiveReadOnlyAddressRoleConfiguration:
    """Value-free binding of the three public address roles.

    Raw addresses deliberately do not belong to this model.  Callers retain
    them only in their short-lived reader closures and expose masked values in
    CLI diagnostics.
    """

    transaction_owner_configured: bool = False
    trading_account_configured: bool = False
    market_target_configured: bool = False
    transaction_owner_source: str = "unavailable"
    trading_account_source: str = "unavailable"
    market_target_source: str = "unavailable"
    transaction_owner_valid: bool = False
    trading_account_valid: bool = False
    market_target_valid: bool = False
    owner_trading_addresses_distinct: bool = False
    owner_target_addresses_distinct: bool = False
    trading_target_addresses_distinct: bool = False
    direct_owner_mode_selected: bool = False
    role_binding_status: str = "unavailable"
    market_target_address_masked: str = "<missing>"
    configuration_fingerprint: str = ""
    blockers: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("transaction_owner_source", "trading_account_source", "market_target_source"):
            if getattr(self, name) not in ADDRESS_ROLE_SOURCE_NAMES:
                raise ValueError("invalid_address_role_source")
        if self.role_binding_status not in ADDRESS_ROLE_BINDING_STATUSES:
            raise ValueError("invalid_address_role_binding_status")
        object.__setattr__(self, "blockers", tuple(dict.fromkeys(str(item) for item in self.blockers)))
        object.__setattr__(self, "direct_owner_mode_selected", bool(self.direct_owner_mode_selected))

    @property
    def ready(self) -> bool:
        return self.role_binding_status == "confirmed" and self.direct_owner_mode_selected

    def safe_dict(self) -> dict[str, Any]:
        return {
            "transaction_owner_configured": self.transaction_owner_configured,
            "trading_account_configured": self.trading_account_configured,
            "market_target_configured": self.market_target_configured,
            "transaction_owner_source": self.transaction_owner_source,
            "trading_account_source": self.trading_account_source,
            "market_target_source": self.market_target_source,
            "transaction_owner_valid": self.transaction_owner_valid,
            "trading_account_valid": self.trading_account_valid,
            "market_target_valid": self.market_target_valid,
            "owner_trading_addresses_distinct": self.owner_trading_addresses_distinct,
            "owner_target_addresses_distinct": self.owner_target_addresses_distinct,
            "trading_target_addresses_distinct": self.trading_target_addresses_distinct,
            "direct_owner_mode_selected": self.direct_owner_mode_selected,
            "role_binding_status": self.role_binding_status,
            "market_target_address": self.market_target_address_masked,
            "configuration_fingerprint": mask_hex_hash(self.configuration_fingerprint) if self.configuration_fingerprint else "",
            "blockers": list(self.blockers),
        }

    def __repr__(self) -> str:
        return f"DreamDexLiveReadOnlyAddressRoleConfiguration(status={self.role_binding_status!r}, direct_owner={self.direct_owner_mode_selected!r})"

READ_ONLY_REHEARSAL_RPC_ALLOWLIST = frozenset({
    "eth_chainId", "eth_getCode", "eth_getTransactionCount", "eth_estimateGas",
    "eth_gasPrice", "eth_maxPriorityFeePerGas", "eth_getBalance",
})
READ_ONLY_REHEARSAL_FORBIDDEN_PREFIXES = ("eth_send", "personal_", "wallet_", "debug_", "trace_")
READ_ONLY_REHEARSAL_FORBIDDEN_METHODS = frozenset({
    "eth_sendTransaction", "eth_sendRawTransaction", "eth_getTransactionReceipt",
    "eth_getTransactionByHash", "eth_getLogs", "eth_subscribe", "eth_newFilter",
    "personal_sign", "wallet_sendTransaction",
})

# Endpoint configuration is represented by symbolic source names only.  Raw
# URLs are deliberately kept outside the rehearsal evidence model.
LIVE_ENDPOINT_TYPES = frozenset({"public_api", "rpc"})
LIVE_ENDPOINT_SOURCE_NAMES = frozenset({
    "not_configured", "pinned_production", "dedicated_public_read_only",
    "dedicated_public_api", "dedicated_rpc_read_only", "dedicated_rpc",
    "dedicated_read_only_env", "dedicated_dreamdex_env",
    "pinned_somnia_mainnet", "unavailable",
})
LIVE_ENDPOINT_SCHEME_STATUSES = frozenset({"https", "local_fixture", "invalid", "unavailable"})

# Evidence vocabulary is deliberately finite so a diagnostic cannot quietly
# turn an unknown result into an affirmative trading prerequisite.
LIVE_EVIDENCE_RESULT_STATUSES = frozenset({
    "confirmed", "not_configured", "not_attempted_due_to_prerequisite",
    "configured", "valid", "invalid", "role_conflict", "authenticated_source_unavailable",
    "authentication_unavailable", "authentication_rejected",
    "transport_unavailable", "response_malformed", "schema_unsupported",
    "source_non_authoritative", "stale", "identity_mismatch",
    "confirmed_unavailable_from_source",
})
LIVE_AUTHENTICATION_STATUSES = frozenset({
    "not_configured", "not_applicable", "session_not_configured", "session_expired",
    "credential_rejected", "request_unauthorized", "request_forbidden",
    "transport_failed", "response_schema_unsupported", "authentication_unavailable",
    "authenticated_success",
})


@dataclass(frozen=True, repr=False)
class DreamDexLiveReadOnlyEvidenceStatus:
    """Safe, value-free status for one live read-only evidence branch."""

    evidence_name: str
    request_performed: bool = False
    source_category: str = "unknown"
    transport_status: str = "not_configured"
    authentication_status: str = "not_configured"
    schema_status: str = "schema_unsupported"
    freshness_status: str = "unknown"
    identity_status: str = "unresolved"
    authority_status: str = "non_authoritative"
    result_status: str = "not_configured"
    prerequisite: str | None = None
    response_shape_fingerprint: str = ""
    blocker: str | None = None
    validation_errors: tuple[str, ...] = ()
    payload_byte_length: int | None = None
    parser_version: str = "live-read-only-evidence-v1"
    typed_method: str | None = None
    purpose: str | None = None
    safe_error_category: str | None = None

    def __post_init__(self) -> None:
        if self.result_status not in LIVE_EVIDENCE_RESULT_STATUSES:
            raise ValueError("invalid_live_evidence_result_status")
        if self.authentication_status not in LIVE_AUTHENTICATION_STATUSES:
            raise ValueError("invalid_live_authentication_status")
        safe_errors = []
        for value in self.validation_errors:
            text = str(value).lower()
            if any(part in text for part in ("http", "0x", "token", "authorization", "cookie", "nonce", "signature")):
                safe_errors.append("sanitized_error")
            else:
                safe_errors.append(text[:80])
        object.__setattr__(self, "validation_errors", tuple(safe_errors))

    def safe_dict(self) -> dict[str, Any]:
        return {
            "evidence_name": self.evidence_name,
            "request_performed": self.request_performed,
            "source_category": self.source_category,
            "transport_status": self.transport_status,
            "authentication_status": self.authentication_status,
            "schema_status": self.schema_status,
            "freshness_status": self.freshness_status,
            "identity_status": self.identity_status,
            "authority_status": self.authority_status,
            "result_status": self.result_status,
            "prerequisite": self.prerequisite,
            "response_shape_fingerprint": mask_hex_hash(self.response_shape_fingerprint) if self.response_shape_fingerprint else "",
            "blocker": self.blocker,
            "validation_errors": list(self.validation_errors),
            "payload_byte_length": self.payload_byte_length,
            "parser_version": self.parser_version,
            "typed_method": self.typed_method,
            "purpose": self.purpose,
            "safe_error_category": self.safe_error_category,
        }

    def __repr__(self) -> str:
        return f"DreamDexLiveReadOnlyEvidenceStatus({self.evidence_name!r}, {self.result_status!r})"


class ReadOnlyRehearsalReader(Protocol):
    """Typed callable boundary for one read-only evidence source."""

    def __call__(self) -> Any: ...


@dataclass(frozen=True, repr=False)
class DreamDexLiveReadOnlyEndpointConfigurationStatus:
    """Value-free validation result for one public or RPC endpoint.

    The endpoint itself is intentionally absent.  This object is suitable for
    diagnostics and journaling without leaking hosts, paths, query strings or
    embedded credentials.
    """

    endpoint_type: str
    configured: bool = False
    source_name: str = "not_configured"
    syntax_valid: bool = False
    scheme_status: str = "unavailable"
    credentials_embedded: bool = False
    redirects_allowed: bool = False
    transport_ready: bool = False
    configuration_fingerprint: str = ""
    blockers: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.endpoint_type not in LIVE_ENDPOINT_TYPES:
            raise ValueError("invalid_live_endpoint_type")
        if self.source_name not in LIVE_ENDPOINT_SOURCE_NAMES:
            raise ValueError("invalid_live_endpoint_source")
        if self.scheme_status not in LIVE_ENDPOINT_SCHEME_STATUSES:
            raise ValueError("invalid_live_endpoint_scheme_status")
        object.__setattr__(self, "redirects_allowed", False)
        object.__setattr__(self, "blockers", tuple(dict.fromkeys(str(item) for item in self.blockers)))

    @property
    def ready(self) -> bool:
        return self.configured and self.syntax_valid and not self.credentials_embedded and self.transport_ready

    def safe_dict(self) -> dict[str, Any]:
        return {
            "endpoint_type": self.endpoint_type,
            "configured": self.configured,
            "source_name": self.source_name,
            "syntax_valid": self.syntax_valid,
            "scheme_status": self.scheme_status,
            "credentials_embedded": self.credentials_embedded,
            "redirects_allowed": False,
            "transport_ready": self.transport_ready,
            "configuration_fingerprint": mask_hex_hash(self.configuration_fingerprint) if self.configuration_fingerprint else "",
            "blockers": list(self.blockers),
        }

    def __repr__(self) -> str:
        return f"DreamDexLiveReadOnlyEndpointConfigurationStatus({self.endpoint_type!r}, ready={self.ready!r})"


def _default_endpoint_status() -> DreamDexLiveReadOnlyEndpointConfigurationStatus:
    return DreamDexLiveReadOnlyEndpointConfigurationStatus(endpoint_type="public_api")


def _default_rpc_endpoint_status() -> DreamDexLiveReadOnlyEndpointConfigurationStatus:
    return DreamDexLiveReadOnlyEndpointConfigurationStatus(endpoint_type="rpc")


@dataclass(frozen=True, repr=False)
class DreamDexLiveReadOnlyConfigurationStatus:
    """Safe summary of discovered non-secret live-read-only configuration."""

    public_api_configured: bool = False
    rpc_configured: bool = False
    authenticated_session_configured: bool = False
    authenticated_session_current: bool = False
    required_chain_id: int = 5031
    market_symbol: str = "SOMI:USDso"
    public_transport_ready: bool = False
    rpc_transport_ready: bool = False
    account_transport_ready: bool = False
    configuration_fingerprint: str = ""
    blockers: tuple[str, ...] = ()
    public_endpoint_status: DreamDexLiveReadOnlyEndpointConfigurationStatus = field(default_factory=_default_endpoint_status)
    rpc_endpoint_status: DreamDexLiveReadOnlyEndpointConfigurationStatus = field(default_factory=_default_rpc_endpoint_status)

    def __post_init__(self) -> None:
        if self.required_chain_id != 5031:
            raise ValueError("live_read_only_chain_must_be_5031")
        if not self.market_symbol or ":" not in self.market_symbol or "://" in self.market_symbol or any(char.isspace() for char in self.market_symbol):
            raise ValueError("live_read_only_market_symbol_invalid")
        object.__setattr__(self, "blockers", tuple(dict.fromkeys(str(item) for item in self.blockers)))

    @property
    def ready_for_public(self) -> bool:
        return self.public_api_configured and self.public_transport_ready

    @property
    def ready_for_rpc(self) -> bool:
        return self.rpc_configured and self.rpc_transport_ready

    def safe_dict(self) -> dict[str, Any]:
        return {
            "public_api_configured": self.public_api_configured,
            "rpc_configured": self.rpc_configured,
            "authenticated_session_configured": self.authenticated_session_configured,
            "authenticated_session_current": self.authenticated_session_current,
            "required_chain_id": self.required_chain_id,
            "market_symbol": self.market_symbol,
            "public_transport_ready": self.public_transport_ready,
            "rpc_transport_ready": self.rpc_transport_ready,
            "account_transport_ready": self.account_transport_ready,
            "configuration_fingerprint": mask_hex_hash(self.configuration_fingerprint) if self.configuration_fingerprint else "",
            "blockers": list(self.blockers),
            "public_endpoint_status": self.public_endpoint_status.safe_dict(),
            "rpc_endpoint_status": self.rpc_endpoint_status.safe_dict(),
        }

    def __repr__(self) -> str:
        return "DreamDexLiveReadOnlyConfigurationStatus(<safe>)"


def _default_configuration_status() -> DreamDexLiveReadOnlyConfigurationStatus:
    return DreamDexLiveReadOnlyConfigurationStatus()


@dataclass(frozen=True, repr=False)
class DreamDexLiveReadOnlyRehearsalDependencies:
    """The deliberately narrow dependency bundle for explicit live rehearsal.

    It contains only readers and immutable safe configuration snapshots.  No
    signer, secret provider, journal repository, approval provider, submitter,
    or raw transaction transport can be attached to this type.
    """

    public_market_reader: ReadOnlyRehearsalReader
    authenticated_account_reader: ReadOnlyRehearsalReader
    typed_rpc_preflight_reader: ReadOnlyRehearsalReader
    monotonic_clock: Callable[[], float] = time.monotonic
    safe_config: Mapping[str, Any] = field(default_factory=dict)
    risk_snapshot: Mapping[str, Any] = field(default_factory=dict)
    fair_play_snapshot: Mapping[str, Any] = field(default_factory=dict)
    configuration_status: DreamDexLiveReadOnlyConfigurationStatus = field(default_factory=_default_configuration_status)
    address_role_configuration: DreamDexLiveReadOnlyAddressRoleConfiguration = field(default_factory=DreamDexLiveReadOnlyAddressRoleConfiguration)
    address_role_builder: Callable[[str | None], DreamDexLiveReadOnlyAddressRoleConfiguration] | None = None

    def __post_init__(self) -> None:
        for name in (
            "public_market_reader",
            "authenticated_account_reader",
            "typed_rpc_preflight_reader",
            "monotonic_clock",
        ):
            if not callable(getattr(self, name)):
                raise TypeError(f"{name}_must_be_callable")
        if self.address_role_builder is not None and not callable(self.address_role_builder):
            raise TypeError("address_role_builder_must_be_callable")
        try:
            safe_config = dict(self.safe_config)
            risk_snapshot = dict(self.risk_snapshot)
            fair_play_snapshot = dict(self.fair_play_snapshot)
        except (TypeError, ValueError):
            raise TypeError("read_only_snapshots_must_be_mappings") from None
        forbidden = ("token", "bearer", "private", "password", "secret", "key", "seed")
        for key in safe_config:
            if any(part in str(key).lower() for part in forbidden):
                raise ValueError("secret_configuration_not_allowed")
        object.__setattr__(self, "safe_config", MappingProxyType(safe_config))
        object.__setattr__(self, "risk_snapshot", MappingProxyType(risk_snapshot))
        object.__setattr__(self, "fair_play_snapshot", MappingProxyType(fair_play_snapshot))

    def __repr__(self) -> str:
        return "DreamDexLiveReadOnlyRehearsalDependencies(<read-only>)"


def _d(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _safe_fp(value: Any) -> str:
    return sha256_hex(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode())


def _role_address_valid(value: Any) -> bool:
    """Strict local validation; no ENS, chain lookup, or network fallback."""
    if isinstance(value, bool) or not isinstance(value, str):
        return False
    if value != value.strip() or any(ord(char) < 32 or char.isspace() for char in value):
        return False
    try:
        normalized = validate_evm_address(value, field="role_address")
    except (TypeError, ValueError):
        return False
    if normalized == "0x" + "0" * 40:
        return False
    # Lower/upper hex are accepted by Ethereum convention.  Mixed-case input
    # is accepted only when the installed eth-utils checksum implementation
    # confirms it; no address is repaired or normalized via a lookup.
    if value[2:].islower() or value[2:].isupper():
        return True
    try:
        from eth_utils import to_checksum_address
        return value == to_checksum_address(normalized)
    except Exception:
        return False


def build_address_role_configuration(
    *,
    transaction_owner: Any = None,
    trading_account: Any = None,
    market_target: Any = None,
    transaction_owner_source: str = "unavailable",
    trading_account_source: str = "unavailable",
    market_target_source: str = "unavailable",
    explicit_typed_test_fixture: bool = False,
) -> DreamDexLiveReadOnlyAddressRoleConfiguration:
    """Build a role-only snapshot without retaining any raw address."""
    owner_configured = bool(transaction_owner)
    trading_configured = bool(trading_account)
    target_configured = bool(market_target)
    owner_valid = _role_address_valid(transaction_owner)
    trading_valid = _role_address_valid(trading_account)
    target_valid = _role_address_valid(market_target)
    if explicit_typed_test_fixture:
        transaction_owner_source = trading_account_source = market_target_source = "explicit_typed_test_fixture"
    blockers: list[str] = []
    if not owner_configured:
        blockers.extend(("transaction_owner_address_unavailable", "transaction_owner_not_explicitly_configured"))
    elif not owner_valid:
        blockers.append("transaction_owner_address_invalid")
    if not trading_configured:
        blockers.append("trading_account_address_unavailable")
    elif not trading_valid:
        blockers.append("trading_account_address_invalid")
    if not target_configured:
        blockers.append("market_target_address_unavailable")
    elif not target_valid:
        blockers.append("market_target_address_invalid")
    owner_trading_distinct = owner_valid and trading_valid and str(transaction_owner).lower() != str(trading_account).lower()
    owner_target_distinct = owner_valid and target_valid and str(transaction_owner).lower() != str(market_target).lower()
    trading_target_distinct = trading_valid and target_valid and str(trading_account).lower() != str(market_target).lower()
    if owner_valid and trading_valid and not owner_trading_distinct:
        blockers.append("transaction_owner_trading_account_role_conflict")
    if owner_valid and target_valid and not owner_target_distinct:
        blockers.append("transaction_owner_market_target_role_conflict")
    if trading_valid and target_valid and not trading_target_distinct:
        blockers.append("trading_account_market_target_role_conflict")
    conflicts = any(reason.endswith("role_conflict") for reason in blockers)
    direct_owner_selected = owner_valid and not conflicts
    if not owner_valid:
        blockers.append("automatic_address_role_substitution_forbidden")
    status = "role_conflict" if conflicts else ("confirmed" if owner_valid and trading_valid and target_valid else "unavailable")
    fingerprint = _safe_fp({
        "owner_configured": owner_configured, "trading_configured": trading_configured,
        "target_configured": target_configured, "owner_valid": owner_valid,
        "trading_valid": trading_valid, "target_valid": target_valid,
        "owner_trading_distinct": owner_trading_distinct,
        "owner_target_distinct": owner_target_distinct,
        "trading_target_distinct": trading_target_distinct,
        "status": status,
    })
    return DreamDexLiveReadOnlyAddressRoleConfiguration(
        transaction_owner_configured=owner_configured,
        trading_account_configured=trading_configured,
        market_target_configured=target_configured,
        transaction_owner_source=transaction_owner_source if owner_configured else "unavailable",
        trading_account_source=trading_account_source if trading_configured else "unavailable",
        market_target_source=market_target_source if target_configured else "unavailable",
        transaction_owner_valid=owner_valid,
        trading_account_valid=trading_valid,
        market_target_valid=target_valid,
        owner_trading_addresses_distinct=owner_trading_distinct,
        owner_target_addresses_distinct=owner_target_distinct,
        trading_target_addresses_distinct=trading_target_distinct,
        direct_owner_mode_selected=direct_owner_selected,
        role_binding_status=status,
        market_target_address_masked=mask_evm_address(market_target) if target_valid else "<missing>",
        configuration_fingerprint=fingerprint,
        blockers=tuple(dict.fromkeys(blockers)),
    )


@dataclass(frozen=True, repr=False)
class DreamDexZeroMutationRehearsalPolicy:
    schema_version: str = "dreamdex-zero-mutation-rehearsal-v1"
    required_chain_id: int = 5031
    required_market_symbol: str | None = None
    required_market_address: str | None = None
    expected_signer_address: str | None = None
    maximum_market_age_ms: int = 30_000
    maximum_account_age_ms: int = 30_000
    maximum_order_notional: Decimal = Decimal("100")
    maximum_position_notional: Decimal = Decimal("100")
    maximum_open_orders: int = 1
    maximum_fee_wei: int = 10**18
    require_authoritative_market_data: bool = True
    require_authoritative_account_data: bool = True
    require_market_rules: bool = True
    require_trading_enabled: bool = True
    require_contract_code: bool = True
    require_pending_nonce: bool = True
    require_fee_evidence: bool = True
    require_gas_estimate: bool = True
    require_balance_evidence: bool = True
    require_runtime_launch_gate: bool = True
    require_risk_approval: bool = True
    require_fair_play_approval: bool = True
    allow_temporary_rehearsal_journal: bool = False
    allow_approval_prompt: bool = False
    allow_keystore_access: bool = False
    allow_signing: bool = False
    allow_submission: bool = False
    authoritative: bool = False
    unresolved_reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.required_chain_id != 5031:
            raise ValueError("rehearsal_chain_must_be_5031")
        if self.maximum_market_age_ms < 0 or self.maximum_account_age_ms < 0:
            raise ValueError("rehearsal_age_limits_must_be_non_negative")
        if self.maximum_order_notional <= 0 or self.maximum_position_notional <= 0:
            raise ValueError("rehearsal_notional_limits_must_be_positive")
        if self.maximum_open_orders < 1 or self.maximum_fee_wei < 0:
            raise ValueError("rehearsal_numeric_limits_invalid")
        for name in ("required_market_address", "expected_signer_address"):
            value = getattr(self, name)
            if value is not None:
                validate_evm_address(value, field=name)
        for flag in ("allow_temporary_rehearsal_journal", "allow_approval_prompt", "allow_keystore_access", "allow_signing", "allow_submission", "authoritative"):
            if getattr(self, flag):
                raise ValueError(f"{flag}_must_remain_disabled")

    def safe_dict(self) -> dict[str, Any]:
        result = {"schema_version": self.schema_version, "required_chain_id": self.required_chain_id,
                  "required_market_symbol": self.required_market_symbol,
                  "required_market_address": mask_evm_address(self.required_market_address) if self.required_market_address else None,
                  "expected_signer_address": mask_evm_address(self.expected_signer_address) if self.expected_signer_address else None,
                  "maximum_market_age_ms": self.maximum_market_age_ms, "maximum_account_age_ms": self.maximum_account_age_ms,
                  "maximum_order_notional": str(self.maximum_order_notional), "maximum_position_notional": str(self.maximum_position_notional),
                  "maximum_open_orders": self.maximum_open_orders, "maximum_fee_wei": self.maximum_fee_wei,
                  "require_authoritative_market_data": self.require_authoritative_market_data,
                  "require_authoritative_account_data": self.require_authoritative_account_data,
                  "require_market_rules": self.require_market_rules,
                  "require_trading_enabled": self.require_trading_enabled,
                  "require_contract_code": self.require_contract_code,
                  "require_pending_nonce": self.require_pending_nonce,
                  "require_fee_evidence": self.require_fee_evidence,
                  "require_gas_estimate": self.require_gas_estimate,
                  "require_balance_evidence": self.require_balance_evidence,
                  "require_runtime_launch_gate": self.require_runtime_launch_gate,
                  "require_risk_approval": self.require_risk_approval,
                  "require_fair_play_approval": self.require_fair_play_approval,
                  "allow_temporary_rehearsal_journal": False, "allow_approval_prompt": False, "allow_keystore_access": False,
                  "allow_signing": False, "allow_submission": False, "authoritative": False,
                  "unresolved_reasons": list(self.unresolved_reasons)}
        return result

    def __repr__(self) -> str:
        return "DreamDexZeroMutationRehearsalPolicy(<safe>)"


@dataclass(frozen=True, repr=False)
class DreamDexZeroMutationRehearsalEvidence:
    market_status: str = "unavailable"
    account_status: str = "unavailable"
    rpc_status: str = "unavailable"
    chain_id: int | None = None
    target_code_status: str = "unavailable"
    pending_nonce_status: str = "unavailable"
    native_balance_status: str = "unavailable"
    gas_estimate_status: str = "unavailable"
    fee_status: str = "unavailable"
    market_rules_status: str = "unavailable"
    runtime_gate_status: str = "unavailable"
    risk_status: str = "unavailable"
    fair_play_status: str = "unavailable"
    market_age_ms: int | None = None
    account_age_ms: int | None = None
    source_fingerprint: str = ""
    market_fingerprint: str = ""
    account_fingerprint: str = ""
    risk_fingerprint: str = ""
    fair_play_fingerprint: str = ""
    observed_monotonic_ms: int | None = None
    network_read_call_count: int = 0
    source_authority: str = "non_authoritative"
    market_identity_status: str = "unavailable"
    account_identity_status: str = "unavailable"
    trading_enabled: bool | None = None
    contract_code_present: bool | None = None
    pending_nonce: int | None = None
    gas_estimate: int | None = None
    estimated_fee_wei: int | None = None
    native_balance_wei: int | None = None
    drawdown_fraction: Decimal | None = None
    preemptive_drawdown: Decimal | None = None
    hard_drawdown_limit: Decimal | None = None
    kill_switch_latched: bool = False
    emergency_exit_requested: bool = False
    emergency_exit_completed: bool = False
    gap_risk_status: str = "unavailable"
    gap_risk_budget_approved: bool | None = None
    gap_risk_blockers: tuple[str, ...] = ()
    account_authority_status: str = "unresolved"
    open_order_status: str = "unavailable"
    fills_status: str = "unavailable"
    public_market_call_count: int = 0
    authenticated_account_call_count: int = 0
    read_only_rpc_call_count: int = 0
    public_market_snapshot_count: int = 0
    public_http_request_count: int = 0
    authenticated_http_request_count: int = 0
    read_only_rpc_request_count: int = 0
    total_network_read_request_count: int = 0
    projected_shocked_drawdown: Decimal | None = None
    maximum_total_fee_wei: int | None = None
    transaction_type: str = "unresolved"
    orderbook_status: str = "unavailable"
    open_order_count: int | None = None
    evidence_statuses: tuple[DreamDexLiveReadOnlyEvidenceStatus, ...] = ()
    native_gas_balance_evidence: str = "not_configured"
    authenticated_trading_balance_evidence: str = "not_configured"
    available_order_currency_balance: str = "not_configured"
    available_base_asset_balance: str = "not_configured"
    primary_blockers: tuple[str, ...] = ()
    derived_blockers: tuple[str, ...] = ()
    not_attempted_stages: tuple[str, ...] = ()
    configuration_status: DreamDexLiveReadOnlyConfigurationStatus = field(default_factory=_default_configuration_status)
    market_listed_status: str = "unavailable"
    market_lifecycle_status: str = "unavailable"
    trading_enabled_status: str = "unavailable"
    place_operation_status: str = "unavailable"
    cancel_operation_status: str = "unavailable"
    trading_status_source: str = "unavailable"
    trading_status_authority: str = "non_authoritative"
    address_role_configuration: DreamDexLiveReadOnlyAddressRoleConfiguration = field(default_factory=DreamDexLiveReadOnlyAddressRoleConfiguration)

    def safe_dict(self) -> dict[str, Any]:
        return {"market_status": self.market_status, "account_status": self.account_status, "rpc_status": self.rpc_status,
                "chain_id": self.chain_id, "target_code_status": self.target_code_status,
                "orderbook_status": self.orderbook_status,
                "open_order_count": self.open_order_count,
                "pending_nonce_status": self.pending_nonce_status, "native_balance_status": self.native_balance_status,
                "gas_estimate_status": self.gas_estimate_status, "fee_status": self.fee_status,
                "market_rules_status": self.market_rules_status, "runtime_gate_status": self.runtime_gate_status,
                "risk_status": self.risk_status, "fair_play_status": self.fair_play_status,
                "market_age_ms": self.market_age_ms, "account_age_ms": self.account_age_ms,
                "source_fingerprint": mask_hex_hash(self.source_fingerprint) if self.source_fingerprint else "",
                "market_fingerprint": mask_hex_hash(self.market_fingerprint) if self.market_fingerprint else "",
                "account_fingerprint": mask_hex_hash(self.account_fingerprint) if self.account_fingerprint else "",
                "risk_fingerprint": mask_hex_hash(self.risk_fingerprint) if self.risk_fingerprint else "",
                "fair_play_fingerprint": mask_hex_hash(self.fair_play_fingerprint) if self.fair_play_fingerprint else "",
                "observed_monotonic_ms": self.observed_monotonic_ms, "network_read_call_count": self.network_read_call_count,
                "source_authority": self.source_authority, "market_identity_status": self.market_identity_status,
                "account_identity_status": self.account_identity_status, "trading_enabled": self.trading_enabled,
                "contract_code_present": self.contract_code_present, "pending_nonce": self.pending_nonce,
                "gas_estimate": self.gas_estimate, "estimated_fee_wei": self.estimated_fee_wei,
                "native_balance_wei": self.native_balance_wei, "open_order_status": self.open_order_status,
                "drawdown_fraction": str(self.drawdown_fraction) if self.drawdown_fraction is not None else None,
                "preemptive_drawdown": str(self.preemptive_drawdown) if self.preemptive_drawdown is not None else None,
                "hard_drawdown_limit": str(self.hard_drawdown_limit) if self.hard_drawdown_limit is not None else None,
                "kill_switch_latched": self.kill_switch_latched, "emergency_exit_requested": self.emergency_exit_requested,
                "emergency_exit_completed": self.emergency_exit_completed,
                "gap_risk_status": self.gap_risk_status,
                "gap_risk_budget_approved": self.gap_risk_budget_approved,
                "gap_risk_blockers": list(self.gap_risk_blockers),
                "account_authority_status": self.account_authority_status,
                "open_order_status": self.open_order_status,
                "fills_status": self.fills_status,
                "public_market_call_count": self.public_market_call_count,
                "authenticated_account_call_count": self.authenticated_account_call_count,
                "read_only_rpc_call_count": self.read_only_rpc_call_count,
                "public_market_snapshot_count": self.public_market_snapshot_count,
                "public_http_request_count": self.public_http_request_count,
                "authenticated_http_request_count": self.authenticated_http_request_count,
                "read_only_rpc_request_count": self.read_only_rpc_request_count,
                "total_network_read_request_count": self.total_network_read_request_count,
                "projected_shocked_drawdown": str(self.projected_shocked_drawdown) if self.projected_shocked_drawdown is not None else None,
                "maximum_total_fee_wei": self.maximum_total_fee_wei,
                "transaction_type": self.transaction_type,
                "evidence_statuses": [item.safe_dict() for item in self.evidence_statuses],
                "native_gas_balance_evidence": self.native_gas_balance_evidence,
                "authenticated_trading_balance_evidence": self.authenticated_trading_balance_evidence,
                "available_order_currency_balance": self.available_order_currency_balance,
                "available_base_asset_balance": self.available_base_asset_balance,
                "primary_blockers": list(self.primary_blockers),
                "derived_blockers": list(self.derived_blockers),
                "not_attempted_stages": list(self.not_attempted_stages),
                "configuration_status": self.configuration_status.safe_dict(),
                "market_listed_status": self.market_listed_status,
                "market_lifecycle_status": self.market_lifecycle_status,
                "trading_enabled_status": self.trading_enabled_status,
                "place_operation_status": self.place_operation_status,
                "cancel_operation_status": self.cancel_operation_status,
                "trading_status_source": self.trading_status_source,
                "trading_status_authority": self.trading_status_authority,
                "address_role_configuration": self.address_role_configuration.safe_dict()}

    def __repr__(self) -> str:
        return "DreamDexZeroMutationRehearsalEvidence(<safe>)"


@dataclass(frozen=True, repr=False)
class DreamDexRehearsalCandidate:
    operation: str
    market_symbol: str
    side: str
    order_type: str
    price: Decimal
    quantity: Decimal
    notional: Decimal
    noncrossing: bool
    candidate_fingerprint: str
    native_value: Decimal = Decimal("0")
    maximum_transaction_fee: Decimal = Decimal("0")
    nonce: int | None = None
    transaction_type: str = "limit"
    rehearsal_only: bool = True
    non_executable: bool = True
    requires_full_evidence_recollection: bool = True
    requires_new_production_journal_intent: bool = True
    requires_new_nonce_reservation: bool = True
    requires_new_lease: bool = True
    requires_separate_approval_ceremony: bool = True

    def safe_dict(self) -> dict[str, Any]:
        return {"operation": self.operation, "market_symbol": self.market_symbol, "side": self.side,
                "order_type": self.order_type, "price": str(self.price), "quantity": str(self.quantity),
                "notional": str(self.notional), "noncrossing": self.noncrossing,
                "candidate_fingerprint": mask_hex_hash(self.candidate_fingerprint), "native_value": str(self.native_value),
                "maximum_transaction_fee": str(self.maximum_transaction_fee), "nonce": self.nonce,
                "transaction_type": self.transaction_type, "rehearsal_only": self.rehearsal_only,
                "non_executable": self.non_executable,
                "requires_full_evidence_recollection": self.requires_full_evidence_recollection,
                "requires_new_production_journal_intent": self.requires_new_production_journal_intent,
                "requires_new_nonce_reservation": self.requires_new_nonce_reservation,
                "requires_new_lease": self.requires_new_lease,
                "requires_separate_approval_ceremony": self.requires_separate_approval_ceremony}

    def __repr__(self) -> str:
        return "DreamDexRehearsalCandidate(<safe>)"


@dataclass(frozen=True, repr=False)
class DreamDexZeroMutationRehearsalResult:
    schema_version: str
    rehearsal_status: str
    chain_evidence_status: str = "unavailable"
    market_evidence_status: str = "unavailable"
    account_evidence_status: str = "unavailable"
    market_rules_status: str = "unavailable"
    trading_status: str = "unavailable"
    contract_code_status: str = "unavailable"
    pending_nonce_status: str = "unavailable"
    gas_estimate_status: str = "unavailable"
    fee_evidence_status: str = "unavailable"
    balance_status: str = "unavailable"
    risk_status: str = "unavailable"
    fair_play_status: str = "unavailable"
    runtime_launch_status: str = "unavailable"
    unsigned_request_status: str = "unavailable"
    envelope_status: str = "unavailable"
    preflight_status: str = "unavailable"
    approval_preview_status: str = "unavailable"
    approval_binding_status: str = "unavailable"
    production_journal_write_performed: bool = False
    temporary_journal_used: bool = False
    temporary_journal_removed: bool = False
    approval_prompt_performed: bool = False
    keystore_read_performed: bool = False
    password_prompt_performed: bool = False
    signer_invocation_count: int = 0
    submission_call_count: int = 0
    mutation_rpc_call_count: int = 0
    network_read_call_count: int = 0
    ready_for_human_review: bool = False
    ready_for_signer_invocation: bool = False
    ready_for_real_submission: bool = False
    rehearsal_fingerprint: str = ""
    authoritative: bool = False
    blockers: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    validation_errors: tuple[str, ...] = ()
    candidate_fingerprint: str | None = None
    gap_risk_status: str = "unavailable"
    gap_risk_budget_approved: bool | None = None
    mode: str = "fixture"
    public_market_call_count: int = 0
    authenticated_account_call_count: int = 0
    read_only_rpc_call_count: int = 0
    public_market_snapshot_count: int = 0
    public_http_request_count: int = 0
    authenticated_http_request_count: int = 0
    read_only_rpc_request_count: int = 0
    total_network_read_request_count: int = 0
    projected_shocked_drawdown: Decimal | None = None
    maximum_total_fee_wei: int | None = None
    transaction_type: str = "unresolved"
    source_authority: str = "non_authoritative"
    market_identity_status: str = "unavailable"
    account_identity_status: str = "unavailable"
    account_authority_status: str = "unresolved"
    open_order_status: str = "unavailable"
    fills_status: str = "unavailable"
    trading_enabled: bool | None = None
    orderbook_status: str = "unavailable"
    open_order_count: int | None = None
    evidence_statuses: tuple[DreamDexLiveReadOnlyEvidenceStatus, ...] = ()
    native_gas_balance_evidence: str = "not_configured"
    authenticated_trading_balance_evidence: str = "not_configured"
    available_order_currency_balance: str = "not_configured"
    available_base_asset_balance: str = "not_configured"
    primary_blockers: tuple[str, ...] = ()
    derived_blockers: tuple[str, ...] = ()
    not_attempted_stages: tuple[str, ...] = ()
    configuration_status: DreamDexLiveReadOnlyConfigurationStatus = field(default_factory=_default_configuration_status)
    market_listed_status: str = "unavailable"
    market_lifecycle_status: str = "unavailable"
    trading_enabled_status: str = "unavailable"
    place_operation_status: str = "unavailable"
    cancel_operation_status: str = "unavailable"
    trading_status_source: str = "unavailable"
    trading_status_authority: str = "non_authoritative"
    address_role_configuration: DreamDexLiveReadOnlyAddressRoleConfiguration = field(default_factory=DreamDexLiveReadOnlyAddressRoleConfiguration)

    @property
    def readiness_status(self) -> str:
        return "ready" if self.ready_for_human_review else "blocked"

    @property
    def mutation_call_count(self) -> int:
        return self.mutation_rpc_call_count

    @property
    def temporary_rehearsal_journal_used(self) -> bool:
        return self.temporary_journal_used

    @property
    def approval_prompt_shown(self) -> bool:
        return self.approval_prompt_performed

    @property
    def keystore_accessed(self) -> bool:
        return self.keystore_read_performed

    @property
    def password_requested(self) -> bool:
        return self.password_prompt_performed

    @property
    def submission_attempt_count(self) -> int:
        return self.submission_call_count

    @property
    def ready_for_signing(self) -> bool:
        return self.ready_for_signer_invocation

    @property
    def ready_for_submission(self) -> bool:
        return self.ready_for_real_submission

    def safe_dict(self) -> dict[str, Any]:
        return {"schema_version": self.schema_version, "rehearsal_status": self.rehearsal_status,
                "chain_evidence_status": self.chain_evidence_status, "market_evidence_status": self.market_evidence_status,
                "account_evidence_status": self.account_evidence_status, "market_rules_status": self.market_rules_status,
                "orderbook_status": self.orderbook_status,
                "trading_status": self.trading_status, "contract_code_status": self.contract_code_status,
                "pending_nonce_status": self.pending_nonce_status, "gas_estimate_status": self.gas_estimate_status,
                "fee_evidence_status": self.fee_evidence_status, "balance_status": self.balance_status,
                "risk_status": self.risk_status, "fair_play_status": self.fair_play_status,
                "runtime_launch_status": self.runtime_launch_status, "unsigned_request_status": self.unsigned_request_status,
                "envelope_status": self.envelope_status, "preflight_status": self.preflight_status,
                "approval_preview_status": self.approval_preview_status, "approval_binding_status": self.approval_binding_status,
                "production_journal_write_performed": False, "temporary_journal_used": False, "temporary_journal_removed": self.temporary_journal_removed,
                "approval_prompt_performed": False, "keystore_read_performed": False, "password_prompt_performed": False,
                "signer_invocation_count": 0, "submission_call_count": 0, "mutation_rpc_call_count": 0,
                "network_read_call_count": self.network_read_call_count, "ready_for_human_review": self.ready_for_human_review,
                "ready_for_signer_invocation": False, "ready_for_real_submission": False, "rehearsal_fingerprint": mask_hex_hash(self.rehearsal_fingerprint),
                "authoritative": False, "blockers": list(self.blockers), "validation_errors": list(self.validation_errors),
                "gap_risk_status": self.gap_risk_status,
                "gap_risk_budget_approved": self.gap_risk_budget_approved,
                "mode": self.mode,
                "public_market_call_count": self.public_market_call_count,
                "authenticated_account_call_count": self.authenticated_account_call_count,
                "read_only_rpc_call_count": self.read_only_rpc_call_count,
                "public_market_snapshot_count": self.public_market_snapshot_count,
                "public_http_request_count": self.public_http_request_count,
                "authenticated_http_request_count": self.authenticated_http_request_count,
                "read_only_rpc_request_count": self.read_only_rpc_request_count,
                "total_network_read_request_count": self.total_network_read_request_count,
                "projected_shocked_drawdown": str(self.projected_shocked_drawdown) if self.projected_shocked_drawdown is not None else None,
                "maximum_total_fee_wei": self.maximum_total_fee_wei,
                "transaction_type": self.transaction_type,
                "source_authority": self.source_authority,
                "market_identity_status": self.market_identity_status,
                "account_identity_status": self.account_identity_status,
                "account_authority_status": self.account_authority_status,
                "open_order_status": self.open_order_status,
                "fills_status": self.fills_status,
                "trading_enabled": self.trading_enabled,
                "open_order_count": self.open_order_count,
                "candidate_fingerprint": mask_hex_hash(self.candidate_fingerprint),
                "evidence_statuses": [item.safe_dict() for item in self.evidence_statuses],
                "native_gas_balance_evidence": self.native_gas_balance_evidence,
                "authenticated_trading_balance_evidence": self.authenticated_trading_balance_evidence,
                "available_order_currency_balance": self.available_order_currency_balance,
                "available_base_asset_balance": self.available_base_asset_balance,
                "primary_blockers": list(self.primary_blockers),
                "derived_blockers": list(self.derived_blockers),
                "not_attempted_stages": list(self.not_attempted_stages),
                "configuration_status": self.configuration_status.safe_dict(),
                "market_listed_status": self.market_listed_status,
                "market_lifecycle_status": self.market_lifecycle_status,
                "trading_enabled_status": self.trading_enabled_status,
                "place_operation_status": self.place_operation_status,
                "cancel_operation_status": self.cancel_operation_status,
                "trading_status_source": self.trading_status_source,
                "trading_status_authority": self.trading_status_authority,
                "address_role_configuration": self.address_role_configuration.safe_dict(),
                # Compatibility aliases for early offline callers.
                "readiness_status": self.readiness_status, "mutation_call_count": self.mutation_call_count,
                "temporary_rehearsal_journal_used": self.temporary_rehearsal_journal_used,
                "approval_prompt_shown": self.approval_prompt_shown, "keystore_accessed": self.keystore_accessed,
                "password_requested": self.password_requested, "submission_attempt_count": self.submission_attempt_count,
                "ready_for_signing": self.ready_for_signing, "ready_for_submission": self.ready_for_submission}

    def __repr__(self) -> str:
        return "DreamDexZeroMutationRehearsalResult(<safe>)"


def build_rehearsal_candidate(*, market_symbol: str, side: str, price: Any, quantity: Any,
                              market_rules: Mapping[str, Any], best_bid: Any = None,
                              best_ask: Any = None, policy: DreamDexZeroMutationRehearsalPolicy | None = None) -> DreamDexRehearsalCandidate | None:
    policy = policy or DreamDexZeroMutationRehearsalPolicy(required_market_symbol=market_symbol)
    if market_symbol != policy.required_market_symbol or side != "BUY":
        return None
    required = ("tick_size", "quantity_step", "minimum_quantity", "minimum_notional")
    if any(_d(market_rules.get(key)) is None or _d(market_rules.get(key)) <= 0 for key in required):
        return None
    p, q = _d(price), _d(quantity)
    if p is None or q is None or p <= 0 or q <= 0:
        return None
    tick, step, minimum, minimum_notional = (_d(market_rules[k]) for k in required)
    if p % tick != 0 or q < minimum or q % step != 0:
        return None
    if side == "BUY" and best_ask is not None and p >= _d(best_ask):
        return None
    if side == "SELL" and best_bid is not None and p <= _d(best_bid):
        return None
    notional = p * q
    if notional < minimum_notional or notional > policy.maximum_order_notional:
        return None
    payload = {"operation": "place_order", "market_symbol": market_symbol, "side": side, "order_type": "limit", "price": str(p), "quantity": str(q)}
    return DreamDexRehearsalCandidate("place_order", market_symbol, side, "limit", p, q, notional, True, _safe_fp(payload))


def collect_live_read_only_rehearsal_evidence(collector: Callable[[], DreamDexZeroMutationRehearsalEvidence | Mapping[str, Any]]) -> DreamDexZeroMutationRehearsalEvidence:
    value = collector()
    if isinstance(value, DreamDexZeroMutationRehearsalEvidence):
        return value
    if isinstance(value, Mapping):
        return DreamDexZeroMutationRehearsalEvidence(**dict(value))
    raise TypeError("typed_read_only_rehearsal_evidence_required")


def _reader_value(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _safe_source_status(value: Any, default: str = "unavailable") -> str:
    status = _reader_value(value, "status", default)
    return str(status) if status is not None else default


def _best_level(levels: Any, *, reverse: bool) -> Decimal | None:
    """Read one safe price from a typed/read-only order-book snapshot."""
    if isinstance(levels, Mapping):
        levels = [{"price": price, "quantity": quantity} for price, quantity in levels.items()]
    if not isinstance(levels, (list, tuple)):
        return None
    prices: list[Decimal] = []
    for level in levels:
        if isinstance(level, Mapping):
            raw = level.get("price", level.get("p"))
            raw_quantity = level.get("quantity", level.get("qty", level.get("amount", level.get("q"))))
        elif isinstance(level, (list, tuple)) and level:
            raw = level[0]
            raw_quantity = level[1] if len(level) > 1 else None
        else:
            raw = None
            raw_quantity = None
        parsed = _d(raw)
        quantity = _d(raw_quantity)
        if parsed is not None and parsed > 0 and (raw_quantity is None or (quantity is not None and quantity > 0)):
            prices.append(parsed)
    if not prices:
        return None
    return max(prices) if reverse else min(prices)


def _safe_exception_category(exc: BaseException | None) -> tuple[str, str]:
    """Map an exception to a non-sensitive transport/auth/schema category."""
    if exc is None:
        return "confirmed", "confirmed"
    text = str(exc).lower()
    if "401" in text or "unauthorized" in text:
        return "authentication_rejected", "request_unauthorized"
    if "403" in text or "forbidden" in text:
        return "authentication_rejected", "request_forbidden"
    if "json" in text or "decode" in text or "malformed" in text:
        return "response_malformed", "response_schema_unsupported"
    return "transport_unavailable", "transport_failed"


def _evidence_status(name: str, *, performed: bool, result: str,
                     source: str = "unknown", transport: str | None = None,
                     auth: str = "not_configured", schema: str = "schema_unsupported",
                     freshness: str = "unknown", identity: str = "unresolved",
                     authority: str = "non_authoritative", prerequisite: str | None = None,
                     blocker: str | None = None, fingerprint: str = "",
                     validation_errors: tuple[str, ...] = (), typed_method: str | None = None,
                     purpose: str | None = None, safe_error_category: str | None = None) -> DreamDexLiveReadOnlyEvidenceStatus:
    if transport is None:
        transport = "confirmed" if result == "confirmed" else result
    return DreamDexLiveReadOnlyEvidenceStatus(
        evidence_name=name, request_performed=performed, source_category=source,
        transport_status=transport, authentication_status=auth,
        schema_status=schema, freshness_status=freshness, identity_status=identity,
        authority_status=authority, result_status=result, prerequisite=prerequisite,
        blocker=blocker, response_shape_fingerprint=fingerprint,
        typed_method=typed_method, purpose=purpose, safe_error_category=safe_error_category,
        validation_errors=validation_errors)


def collect_live_read_only_rehearsal_evidence_from_dependencies(
    dependencies: DreamDexLiveReadOnlyRehearsalDependencies,
) -> DreamDexZeroMutationRehearsalEvidence:
    """Collect only read-only evidence from the explicit dependency bundle.

    Source exceptions are intentionally reduced to unavailable statuses; raw
    exception text, URLs, headers, tokens, and payload values never enter the
    evidence model.
    """
    started = dependencies.monotonic_clock()
    public_calls = auth_calls = rpc_calls = 0
    market_exc = account_exc = rpc_exc = None
    try:
        market = dependencies.public_market_reader()
        public_calls = 1
    except Exception as exc:
        market_exc = exc
        market = None
    try:
        account = dependencies.authenticated_account_reader()
        auth_calls = 1 if account is not None else 0
    except Exception as exc:
        account_exc = exc
        account = None
    try:
        rpc = dependencies.typed_rpc_preflight_reader()
        rpc_calls = int(_reader_value(rpc, "read_only_rpc_call_count", 0) or 0)
    except Exception as exc:
        rpc_exc = exc
        rpc = None

    metadata = _reader_value(market, "metadata")
    rules = _reader_value(metadata, "trading_rules")
    market_status = _safe_source_status(market)
    rules_available = bool(_reader_value(rules, "available", False))
    rules_status_for = getattr(rules, "status_for", None)
    if callable(rules_status_for):
        lifecycle_evidence_status = str(rules_status_for("market_status"))
        trading_evidence_status = str(rules_status_for("trading_enabled"))
    else:
        lifecycle_evidence_status = "confirmed" if _reader_value(rules, "market_status") is not None else "unavailable"
        trading_evidence_status = "confirmed" if _reader_value(rules, "trading_enabled") is True else "unavailable"
    book = _reader_value(market, "orderbook")
    if isinstance(book, Mapping):
        bids = book.get("bids", book.get("bid", []))
        asks = book.get("asks", book.get("ask", []))
    else:
        bids = asks = []
    best_bid = _best_level(bids, reverse=True)
    best_ask = _best_level(asks, reverse=False)
    spread_bps = None
    if best_bid is not None and best_ask is not None and best_bid < best_ask:
        spread_bps = (best_ask - best_bid) / ((best_ask + best_bid) / Decimal("2")) * Decimal("10000")
    maximum_spread_bps = _d(dependencies.safe_config.get("maximum_spread_bps", "1000")) or Decimal("1000")
    orderbook_status = "available" if best_bid is not None and best_ask is not None and best_bid < best_ask and spread_bps is not None and spread_bps <= maximum_spread_bps else "unavailable"
    market_symbol = _reader_value(metadata, "symbol")
    market_pool = _reader_value(metadata, "pool_contract", _reader_value(metadata, "pool_address"))
    role_configuration = dependencies.address_role_configuration
    role_builder = dependencies.address_role_builder
    if callable(role_builder):
        try:
            role_configuration = role_builder(market_pool)
        except Exception:
            role_configuration = dependencies.address_role_configuration
    expected_symbol = dependencies.safe_config.get("required_market_symbol")
    expected_pool = dependencies.safe_config.get("required_market_address")
    market_identity = "confirmed" if market_symbol and (expected_symbol is None or market_symbol == expected_symbol) and market_pool and (expected_pool is None or market_pool == expected_pool) else "unavailable"
    trading_enabled = _reader_value(rules, "trading_enabled") is True
    market_listed_status = "confirmed" if metadata is not None and market_symbol else "unavailable"
    market_lifecycle_status = "confirmed" if lifecycle_evidence_status == "confirmed" else "confirmed_unavailable_from_source"
    trading_status_source = "public_markets" if trading_evidence_status == "confirmed" else "unavailable"
    trading_status_authority = "authoritative" if trading_evidence_status == "confirmed" else "non_authoritative"
    observed_at = _reader_value(metadata, "observed_at", _reader_value(market, "observed_at"))
    market_age_ms = None
    if observed_at is not None:
        try:
            from datetime import datetime, timezone
            now = datetime.now(timezone.utc)
            if getattr(observed_at, "tzinfo", None) is None:
                observed_at = observed_at.replace(tzinfo=timezone.utc)
            market_age_ms = max(0, int((now - observed_at).total_seconds() * 1000))
        except Exception:
            market_age_ms = None

    account_identity = _reader_value(account, "account_address_semantics", "unresolved")
    account_authority = "confirmed" if account_identity in {"resolved", "authoritative"} else "unresolved"
    open_order_status = str(_reader_value(account, "open_orders_status", "unavailable"))
    fills_status = str(_reader_value(account, "fills_status", "unavailable"))
    account_status = "available" if account is not None and account_authority == "confirmed" and open_order_status in {"available", "confirmed", "available_empty"} and fills_status in {"available", "confirmed", "available_empty"} else "unavailable"
    account_age_ms = None
    account_observed = _reader_value(account, "observed_at")
    if account_observed is not None:
        try:
            from datetime import datetime, timezone
            now = datetime.now(timezone.utc)
            if getattr(account_observed, "tzinfo", None) is None:
                account_observed = account_observed.replace(tzinfo=timezone.utc)
            account_age_ms = max(0, int((now - account_observed).total_seconds() * 1000))
        except Exception:
            account_age_ms = None

    maximum_market_age_ms = int(dependencies.safe_config.get("maximum_market_age_ms", 30_000) or 30_000)
    maximum_account_age_ms = int(dependencies.safe_config.get("maximum_account_age_ms", 30_000) or 30_000)
    market_freshness = "stale" if market_age_ms is not None and market_age_ms > maximum_market_age_ms else ("fresh" if market_age_ms is not None else "unknown")
    account_freshness = "stale" if account_age_ms is not None and account_age_ms > maximum_account_age_ms else ("fresh" if account_age_ms is not None else "unknown")

    rpc_map = rpc if isinstance(rpc, Mapping) else {}
    read_counts = dependencies.safe_config.get("_network_read_counts", {})
    if not isinstance(read_counts, Mapping):
        read_counts = {}
    public_http_calls = int(
        dependencies.safe_config.get(
            "_public_http_request_count",
            read_counts.get("public_http", public_calls),
        )
        or 0
    )
    authenticated_http_calls = int(
        dependencies.safe_config.get(
            "_authenticated_http_request_count",
            read_counts.get("authenticated_http", auth_calls),
        )
        or 0
    )
    rpc_request_calls = int(rpc_calls)
    total_network_reads = public_http_calls + authenticated_http_calls + rpc_request_calls
    configuration = dependencies.configuration_status
    rpc_status = str(rpc_map.get("status", "unavailable"))
    gap_map = dependencies.risk_snapshot
    fair_map = dependencies.fair_play_snapshot
    gap_status = str(gap_map.get("status", "unavailable"))
    gap_approved = gap_map.get("gap_risk_budget_approved")
    projected_dd = _d(gap_map.get("projected_shocked_drawdown"))
    fair_status = str(fair_map.get("status", "unavailable"))

    market_error, market_auth = _safe_exception_category(market_exc)
    account_error, account_auth = _safe_exception_category(account_exc)
    rpc_error, rpc_auth = _safe_exception_category(rpc_exc)
    market_transport = "confirmed" if market is not None else market_error
    account_transport = "confirmed" if account is not None else account_error
    rpc_transport = "confirmed" if rpc is not None else rpc_error
    market_result = "confirmed" if market is not None else "transport_unavailable"
    if market is not None and not market_symbol:
        market_result = "schema_unsupported"
    identity_result = "confirmed" if market_identity == "confirmed" else ("identity_mismatch" if market_symbol and expected_symbol and market_symbol != expected_symbol else "confirmed_unavailable_from_source")
    orderbook_result = "confirmed" if orderbook_status == "available" else ("schema_unsupported" if market is not None else "transport_unavailable")
    rules_result = "confirmed" if rules_available else ("confirmed_unavailable_from_source" if metadata is not None else "transport_unavailable")
    if trading_evidence_status == "confirmed":
        trading_result = "confirmed"
        trading_blocker = None if trading_enabled else "market_trading_disabled"
    else:
        trading_result = "confirmed_unavailable_from_source"
        trading_blocker = "trading_status_authoritative_source_unavailable"
    raw_auth_transport = str(_reader_value(account, "authenticated_transport_status", ""))
    auth_state = str(_reader_value(_reader_value(account, "auth_snapshot"), "state", ""))
    if auth_state in {"expired", "session_expired"}:
        auth_status = "session_expired"
    elif auth_state in {"rejected", "credential_rejected"}:
        auth_status = "credential_rejected"
    else:
        auth_status = "authenticated_success" if account is not None and raw_auth_transport in {"available", "authenticated_success", "confirmed"} else (
            "session_not_configured" if account is not None and raw_auth_transport in {"", "unconfigured", "not_configured"} else
            (account_auth if account_exc else "authentication_unavailable"))
    account_result = "confirmed" if account_status == "available" else ("source_non_authoritative" if account is not None else "authentication_unavailable")
    account_identity_result = (
        "confirmed" if account_authority == "confirmed" else
        ("authenticated_source_unavailable" if account is None else "source_non_authoritative")
    )
    authenticated_obj = _reader_value(account, "authenticated")
    authenticated_balance_status = _reader_value(authenticated_obj, "balances_status")
    authenticated_balance = _reader_value(account, "authenticated_trading_balance_status", _reader_value(account, "balance_status", _reader_value(authenticated_balance_status, "status", "unavailable")))
    order_currency = _reader_value(account, "order_currency_balance_status", _reader_value(account, "quote_balance_status", "unavailable"))
    base_asset = _reader_value(account, "base_asset_balance_status", _reader_value(account, "base_balance_status", "unavailable"))
    def _balance_result(value: Any, *, configured: bool) -> str:
        if str(value) in {"available", "confirmed", "authoritative"}:
            return "confirmed"
        return "confirmed_unavailable_from_source" if configured else "not_configured"

    def _role_result(configured: bool, valid: bool) -> str:
        return "valid" if valid else ("invalid" if configured else "not_configured")

    role_evidence_statuses = [
        _evidence_status(
            "transaction_owner_configuration", performed=False,
            result=_role_result(role_configuration.transaction_owner_configured, role_configuration.transaction_owner_valid),
            source=role_configuration.transaction_owner_source,
            authority="non_authoritative", auth="not_applicable", schema="not_applicable",
            blocker=role_configuration.blockers[0] if not role_configuration.transaction_owner_valid and role_configuration.blockers else None,
            purpose="explicit contest-owner transaction signer role",
        ),
        _evidence_status(
            "trading_account_configuration", performed=False,
            result=_role_result(role_configuration.trading_account_configured, role_configuration.trading_account_valid),
            source=role_configuration.trading_account_source,
            authority="non_authoritative", auth="not_applicable", schema="not_applicable", purpose="authenticated trading account role",
        ),
        _evidence_status(
            "market_target_configuration", performed=False,
            result=_role_result(role_configuration.market_target_configured, role_configuration.market_target_valid),
            source=role_configuration.market_target_source,
            authority="authoritative" if role_configuration.market_target_valid else "non_authoritative",
            auth="not_applicable", schema="not_applicable",
            typed_method="GET /markets", purpose="exact market/pool transaction target",
        ),
        _evidence_status(
            "owner_trading_role_separation", performed=False,
            result=("confirmed" if role_configuration.owner_trading_addresses_distinct else ("role_conflict" if role_configuration.transaction_owner_configured and role_configuration.trading_account_configured else "not_configured")),
            source="role_binding", authority="non_authoritative", auth="not_applicable", schema="not_applicable",
            blocker="transaction_owner_trading_account_role_conflict" if role_configuration.transaction_owner_configured and role_configuration.trading_account_configured and not role_configuration.owner_trading_addresses_distinct else None,
            purpose="prevent owner/trading substitution",
        ),
        _evidence_status(
            "owner_target_role_separation", performed=False,
            result=("confirmed" if role_configuration.owner_target_addresses_distinct else ("role_conflict" if role_configuration.transaction_owner_valid and role_configuration.market_target_valid else "not_configured")),
            source="role_binding", authority="non_authoritative", auth="not_applicable", schema="not_applicable",
            blocker="transaction_owner_market_target_role_conflict" if role_configuration.transaction_owner_valid and role_configuration.market_target_valid and not role_configuration.owner_target_addresses_distinct else None,
            purpose="prevent owner/target substitution",
        ),
        _evidence_status(
            "trading_target_role_separation", performed=False,
            result=("confirmed" if role_configuration.trading_target_addresses_distinct else ("role_conflict" if role_configuration.trading_account_valid and role_configuration.market_target_valid else "not_configured")),
            source="role_binding", authority="non_authoritative", auth="not_applicable", schema="not_applicable",
            blocker="trading_account_market_target_role_conflict" if role_configuration.trading_account_valid and role_configuration.market_target_valid and not role_configuration.trading_target_addresses_distinct else None,
            purpose="prevent trading-account/target substitution",
        ),
    ]
    evidence_statuses: list[DreamDexLiveReadOnlyEvidenceStatus] = role_evidence_statuses + [
        _evidence_status("market_identity", performed=public_calls > 0, result=identity_result,
                         source="public_market", transport=market_transport, schema="confirmed" if metadata is not None else "schema_unsupported",
                         identity=identity_result, authority="authoritative" if market_identity == "confirmed" else "non_authoritative",
                         blocker=None if identity_result == "confirmed" else "market_identity_unconfirmed",
                         typed_method="GET /markets", purpose="exact symbol and pool identity"),
        _evidence_status("order_book", performed=public_calls > 0, result=orderbook_result, source="public_market",
                         transport=market_transport, schema="confirmed" if orderbook_status == "available" else "schema_unsupported",
                         freshness=market_freshness, authority="authoritative" if orderbook_status == "available" and market_freshness != "stale" else "non_authoritative",
                         blocker=None if orderbook_result == "confirmed" else "orderbook_evidence_unavailable",
                         typed_method="GET /orderbooks?symbols={symbol}", purpose="fresh bid/ask/depth"),
        _evidence_status("market_rules", performed=public_calls > 0, result=rules_result, source="public_market", transport=market_transport,
                         schema="confirmed" if rules_available else "schema_unsupported", authority="authoritative" if rules_available else "non_authoritative",
                         blocker=None if rules_result == "confirmed" else "market_rules_unavailable",
                         typed_method="GET /markets", purpose="complete trading rules"),
        _evidence_status("trading_status", performed=public_calls > 0, result=trading_result, source="public_market", transport=market_transport,
                         schema="confirmed" if trading_evidence_status == "confirmed" else "schema_unsupported",
                         authority=trading_status_authority,
                         blocker=trading_blocker,
                         typed_method="GET /markets", purpose="explicit trading status"),
        _evidence_status("trading_enabled", performed=public_calls > 0, result=trading_result, source="public_market", transport=market_transport,
                         schema="confirmed" if trading_evidence_status == "confirmed" else "schema_unsupported",
                         authority=trading_status_authority, blocker=trading_blocker,
                         typed_method="GET /markets", purpose="explicit trading-enabled flag"),
        _evidence_status("market_listed", performed=public_calls > 0, result="confirmed" if market_listed_status == "confirmed" else "confirmed_unavailable_from_source",
                         source="public_market", transport=market_transport,
                         schema="confirmed" if metadata is not None else "schema_unsupported",
                         identity="confirmed" if market_listed_status == "confirmed" else "unresolved",
                         authority="authoritative" if market_listed_status == "confirmed" else "non_authoritative",
                         blocker=None if market_listed_status == "confirmed" else "market_not_listed",
                         typed_method="GET /markets", purpose="market listing"),
        _evidence_status("market_lifecycle", performed=public_calls > 0, result="confirmed" if market_lifecycle_status == "confirmed" else "confirmed_unavailable_from_source",
                         source="public_market", transport=market_transport,
                         schema="confirmed" if lifecycle_evidence_status == "confirmed" else "schema_unsupported",
                         authority="authoritative" if lifecycle_evidence_status == "confirmed" else "non_authoritative",
                         blocker=None if lifecycle_evidence_status == "confirmed" else "market_lifecycle_unconfirmed",
                         typed_method="GET /markets", purpose="market lifecycle status"),
        _evidence_status("place_supported", performed=public_calls > 0, result="confirmed_unavailable_from_source", source="public_market",
                         transport=market_transport, authority="non_authoritative", blocker="place_operation_support_unconfirmed",
                         typed_method="GET /markets", purpose="place operation support"),
        _evidence_status("cancel_supported", performed=public_calls > 0, result="confirmed_unavailable_from_source", source="public_market",
                         transport=market_transport, authority="non_authoritative", blocker="cancel_operation_support_unconfirmed",
                         typed_method="GET /markets", purpose="cancel operation support"),
        _evidence_status("account_identity", performed=auth_calls > 0, result=account_identity_result, source="authenticated_account",
                         transport=account_transport, auth=auth_status, schema="confirmed" if account is not None else "schema_unsupported",
                         freshness=account_freshness, authority=account_authority if account_freshness != "stale" else "non_authoritative", blocker=None if account_identity_result == "confirmed" and account_freshness != "stale" else "account_identity_not_authoritative",
                         typed_method="authenticated account snapshot", purpose="authenticated identity and role binding"),
        _evidence_status("trading_balances", performed=auth_calls > 0, result=("authentication_unavailable" if account is None else _balance_result(authenticated_balance, configured=True)), source="authenticated_account",
                         transport=account_transport, auth=auth_status, authority="authoritative" if account is not None and _balance_result(authenticated_balance, configured=True) == "confirmed" else "non_authoritative",
                         blocker=None if account is not None and _balance_result(authenticated_balance, configured=True) == "confirmed" else "authenticated_trading_balance_unavailable",
                         typed_method="authenticated account balances", purpose="authoritative trading funds"),
        _evidence_status("open_orders", performed=auth_calls > 0, result="confirmed" if open_order_status in {"available", "confirmed", "available_empty"} else "confirmed_unavailable_from_source",
                         source="authenticated_account", transport=account_transport, auth=auth_status, authority=account_authority,
                         blocker=None if open_order_status in {"available", "confirmed", "available_empty"} else "incomplete_open_orders_source",
                         typed_method="authenticated open-orders source", purpose="open order authority"),
        _evidence_status("recent_fills", performed=auth_calls > 0, result="confirmed" if fills_status in {"available", "confirmed", "available_empty"} else "confirmed_unavailable_from_source",
                         source="authenticated_account", transport=account_transport, auth=auth_status, authority=account_authority,
                         blocker=None if fills_status in {"available", "confirmed", "available_empty"} else "incomplete_fills_source",
                         typed_method="authenticated recent-fills source", purpose="fill authority"),
    ]
    call_statuses = rpc_map.get("call_statuses", {}) if isinstance(rpc_map.get("call_statuses", {}), Mapping) else {}
    rpc_error_categories = rpc_map.get("rpc_error_categories", {}) if isinstance(rpc_map.get("rpc_error_categories", {}), Mapping) else {}
    def _rpc_call_status(name: str, value: Any, *, attempted_default: bool = True) -> DreamDexLiveReadOnlyEvidenceStatus:
        status = str(call_statuses.get(name, "confirmed" if value is not None else ("not_configured" if not configuration.rpc_configured else "transport_unavailable")))
        method_names = {
            "chain_id": "eth_chainId", "target_code": "eth_getCode", "pending_nonce": "eth_getTransactionCount",
            "native_gas_balance": "eth_getBalance", "fee_data": "eth_gasPrice|eth_maxPriorityFeePerGas",
            "gas_estimate": "eth_estimateGas",
        }
        purposes = {
            "chain_id": "network identity", "target_code": "target contract binding",
            "pending_nonce": "pending nonce evidence", "native_gas_balance": "native gas only",
            "fee_data": "fee evidence", "gas_estimate": "unsigned candidate gas bound",
        }
        if status == "not_attempted_due_to_prerequisite":
            return _evidence_status(name, performed=False, result=status, source="somnia_rpc", transport=status,
                                     prerequisite="formed_unsigned_candidate", blocker="gas_estimate_prerequisite_unavailable",
                                     typed_method=method_names.get(name), purpose=purposes.get(name))
        return _evidence_status(name, performed=attempted_default and name in call_statuses or value is not None,
                                result="confirmed" if status in {"available", "confirmed"} and value is not None else status,
                                source="somnia_rpc", transport="confirmed" if value is not None else status,
                                schema="confirmed" if value is not None else "schema_unsupported",
                                authority="authoritative" if value is not None else "non_authoritative",
                                typed_method=method_names.get(name), purpose=purposes.get(name),
                                safe_error_category=None if value is not None else str(rpc_error_categories.get(name, status)))
    evidence_statuses.extend([
        _rpc_call_status("chain_id", rpc_map.get("chain_id")),
        _rpc_call_status("target_code", rpc_map.get("contract_code_present")),
        _rpc_call_status("pending_nonce", rpc_map.get("pending_nonce")),
        _rpc_call_status("native_gas_balance", rpc_map.get("native_balance_wei")),
        _rpc_call_status("fee_data", rpc_map.get("fee_per_gas_wei", rpc_map.get("maximum_total_fee_wei"))),
        _rpc_call_status("gas_estimate", rpc_map.get("gas_estimate"), attempted_default=False),
    ])
    elapsed = max(0.0, dependencies.monotonic_clock() - started)
    _ = elapsed  # monotonic timing is intentionally not exposed as a value.
    return DreamDexZeroMutationRehearsalEvidence(
        market_status=market_status if market is not None and orderbook_status == "available" else "unavailable",
        orderbook_status=orderbook_status,
        account_status=account_status,
        rpc_status=rpc_status,
        chain_id=rpc_map.get("chain_id"),
        target_code_status=str(rpc_map.get("target_code_status", "unavailable")),
        pending_nonce_status=str(rpc_map.get("pending_nonce_status", "unavailable")),
        native_balance_status=str(rpc_map.get("native_balance_status", "unavailable")),
        gas_estimate_status=str(rpc_map.get("gas_estimate_status", "unavailable")),
        fee_status=str(rpc_map.get("fee_status", "unavailable")),
        market_rules_status="available" if rules_available else "unavailable",
        runtime_gate_status=str(gap_map.get("runtime_gate_status", "unavailable")),
        risk_status=str(gap_map.get("risk_status", "unavailable")),
        fair_play_status=fair_status,
        market_age_ms=market_age_ms,
        account_age_ms=account_age_ms,
        network_read_call_count=total_network_reads,
        source_authority="authoritative" if market_status == "available" and rules_available else "non_authoritative",
        market_identity_status=market_identity,
        account_identity_status=account_authority,
        trading_enabled=trading_enabled,
        contract_code_present=rpc_map.get("contract_code_present"),
        pending_nonce=rpc_map.get("pending_nonce"),
        gas_estimate=rpc_map.get("gas_estimate"),
        estimated_fee_wei=rpc_map.get("maximum_total_fee_wei"),
        native_balance_wei=rpc_map.get("native_balance_wei"),
        open_order_status=open_order_status,
        fills_status=fills_status,
        open_order_count=_reader_value(account, "open_order_count"),
        drawdown_fraction=_d(gap_map.get("drawdown_fraction")),
        preemptive_drawdown=_d(gap_map.get("preemptive_drawdown")),
        hard_drawdown_limit=_d(gap_map.get("hard_drawdown_limit")),
        kill_switch_latched=bool(gap_map.get("kill_switch_latched", False)),
        emergency_exit_requested=bool(gap_map.get("emergency_exit_requested", False)),
        emergency_exit_completed=bool(gap_map.get("emergency_exit_completed", False)),
        gap_risk_status=gap_status,
        gap_risk_budget_approved=gap_approved,
        gap_risk_blockers=tuple(str(item) for item in gap_map.get("blockers", ())),
        account_authority_status=account_authority,
        public_market_call_count=public_calls,
        authenticated_account_call_count=auth_calls,
        read_only_rpc_call_count=rpc_calls,
        public_market_snapshot_count=public_calls,
        public_http_request_count=public_http_calls,
        authenticated_http_request_count=authenticated_http_calls,
        read_only_rpc_request_count=rpc_request_calls,
        total_network_read_request_count=total_network_reads,
        projected_shocked_drawdown=projected_dd,
        maximum_total_fee_wei=rpc_map.get("maximum_total_fee_wei"),
        transaction_type=str(rpc_map.get("transaction_type", "unresolved")),
        evidence_statuses=tuple(evidence_statuses),
        native_gas_balance_evidence=(
            "confirmed" if rpc_map.get("native_balance_wei") is not None else
            str(rpc_map.get("native_balance_status", "not_configured"))
        ),
        authenticated_trading_balance_evidence=("authentication_unavailable" if account is None else _balance_result(authenticated_balance, configured=True)),
        available_order_currency_balance=("authentication_unavailable" if account is None else _balance_result(order_currency, configured=True)),
        available_base_asset_balance=("authentication_unavailable" if account is None else _balance_result(base_asset, configured=True)),
        configuration_status=configuration,
        market_listed_status=market_listed_status,
        market_lifecycle_status=market_lifecycle_status,
        trading_enabled_status=trading_result,
        place_operation_status="confirmed_unavailable_from_source",
        cancel_operation_status="confirmed_unavailable_from_source",
        trading_status_source=trading_status_source,
        trading_status_authority=trading_status_authority,
        address_role_configuration=role_configuration,
    )


def _causal_blocker_sets(evidence: DreamDexZeroMutationRehearsalEvidence,
                         policy: DreamDexZeroMutationRehearsalPolicy,
                         candidate: DreamDexRehearsalCandidate | None) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """Return primary, derived, and not-attempted blockers without cascades."""
    primary: list[str] = []
    derived: list[str] = []
    not_attempted: list[str] = []
    if evidence.evidence_statuses:
        primary.extend(evidence.configuration_status.blockers)
        primary.extend(evidence.address_role_configuration.blockers)
        primary.extend(evidence.primary_blockers)
        derived.extend(evidence.derived_blockers)
        not_attempted.extend(evidence.not_attempted_stages)
        for item in evidence.evidence_statuses:
            if item.result_status == "confirmed":
                continue
            if item.result_status == "not_attempted_due_to_prerequisite":
                not_attempted.append(item.evidence_name)
                if item.blocker:
                    derived.append(item.blocker)
                continue
            if item.blocker:
                primary.append(item.blocker)
        if evidence.native_gas_balance_evidence != "confirmed":
            primary.append("native_gas_balance_unavailable")
        if evidence.authenticated_trading_balance_evidence != "confirmed":
            primary.append("authenticated_trading_balance_unavailable")
        if evidence.available_order_currency_balance != "confirmed":
            primary.append("order_currency_balance_unavailable")
        if evidence.available_base_asset_balance != "confirmed":
            primary.append("base_asset_balance_unavailable")
        if evidence.gas_estimate_status == "not_attempted_due_to_prerequisite":
            derived.append("gas_estimate_prerequisite_unavailable")
            not_attempted.append("gas_estimate")
        elif evidence.gas_estimate is None and policy.require_gas_estimate:
            primary.append("gas_estimate_unavailable")
        if evidence.chain_id is not None and evidence.chain_id != policy.required_chain_id:
            primary.append("rpc_chain_mismatch")
        if candidate is None:
            derived.append("candidate_not_constructed")
            not_attempted.append("approval_preview_not_constructed")
        return tuple(dict.fromkeys(primary)), tuple(dict.fromkeys(derived)), tuple(dict.fromkeys(not_attempted))
    return (), (), ()


def run_zero_mutation_rehearsal(*, policy: DreamDexZeroMutationRehearsalPolicy,
                                evidence: DreamDexZeroMutationRehearsalEvidence,
                                candidate: DreamDexRehearsalCandidate | None = None,
                                execute_read_only: bool = False,
                                collector: Callable[[], DreamDexZeroMutationRehearsalEvidence | Mapping[str, Any]] | None = None,
                                mode: str = "fixture") -> DreamDexZeroMutationRehearsalResult:
    if mode not in {"fixture", "live-read-only"}:
        raise ValueError("invalid_rehearsal_mode")
    if execute_read_only:
        if collector is None:
            return _result(policy, evidence, ("read_only_collector_unavailable",), candidate, mode=mode)
        evidence = collect_live_read_only_rehearsal_evidence(collector)
    primary_blockers, derived_blockers, not_attempted_stages = _causal_blocker_sets(evidence, policy, candidate)
    blockers: list[str] = list(primary_blockers) + list(derived_blockers)
    if evidence.market_status != "available" or evidence.source_authority != "authoritative" or (evidence.market_age_ms is not None and evidence.market_age_ms > policy.maximum_market_age_ms):
        blockers.append("market_evidence_unavailable_or_stale")
    if evidence.orderbook_status != "available":
        blockers.append("orderbook_evidence_unavailable")
    if evidence.account_status != "available" or evidence.account_age_ms is None or evidence.account_age_ms > policy.maximum_account_age_ms:
        blockers.append("account_evidence_unavailable_or_stale")
    if policy.require_authoritative_account_data and evidence.account_authority_status not in {"confirmed", "authoritative"}:
        blockers.append("account_identity_not_authoritative")
    if policy.require_authoritative_account_data and evidence.open_order_status not in {"available", "confirmed", "available_empty"}:
        blockers.append("open_order_evidence_unavailable")
    if policy.require_authoritative_account_data and evidence.fills_status not in {"available", "confirmed", "available_empty"}:
        blockers.append("fills_evidence_unavailable")
    if evidence.open_order_count is not None and evidence.open_order_count > policy.maximum_open_orders:
        blockers.append("maximum_open_orders_exceeded")
    if policy.require_authoritative_market_data and evidence.market_identity_status not in {"confirmed", "source_confirmed"}:
        blockers.append("market_identity_unconfirmed")
    if policy.require_authoritative_account_data and evidence.account_identity_status not in {"confirmed", "source_confirmed"}:
        blockers.append("account_identity_unconfirmed")
    legacy_status_checks = (("rpc_status", "rpc_evidence_unavailable"), ("target_code_status", "target_contract_code_unavailable"), ("pending_nonce_status", "pending_nonce_unavailable"), ("native_balance_status", "balance_evidence_unavailable"), ("gas_estimate_status", "gas_estimate_unavailable"), ("fee_status", "fee_evidence_unavailable"), ("market_rules_status", "market_rules_unavailable"), ("runtime_gate_status", "runtime_launch_gate_blocked"), ("risk_status", "risk_unavailable"), ("fair_play_status", "fair_play_unavailable"))
    if not evidence.evidence_statuses:
        checks = legacy_status_checks
    else:
        checks = tuple(item for item in legacy_status_checks if item[0] not in {"rpc_status", "gas_estimate_status", "native_balance_status"})
    for field_name, blocker in checks:
        if getattr(evidence, field_name) != "available":
            blockers.append(blocker)
    if evidence.chain_id is not None and evidence.chain_id != policy.required_chain_id:
        blockers.append("rpc_chain_mismatch")
    if evidence.kill_switch_latched:
        blockers.append("kill_switch_latched")
    if evidence.emergency_exit_requested and not evidence.emergency_exit_completed:
        blockers.append("emergency_exit_unresolved")
    if evidence.drawdown_fraction is None:
        blockers.append("drawdown_evidence_unavailable")
    elif evidence.hard_drawdown_limit is None or evidence.drawdown_fraction > evidence.hard_drawdown_limit:
        blockers.append("drawdown_above_hard_limit")
    elif evidence.preemptive_drawdown is None or evidence.drawdown_fraction >= evidence.preemptive_drawdown:
        blockers.append("drawdown_above_preemptive_threshold")
    if evidence.gap_risk_status != "available" or evidence.gap_risk_budget_approved is not True:
        blockers.append("gap_risk_unavailable")
    if evidence.projected_shocked_drawdown is None:
        blockers.append("projected_shocked_drawdown_unavailable")
    elif evidence.hard_drawdown_limit is None or evidence.projected_shocked_drawdown >= evidence.hard_drawdown_limit:
        blockers.append("projected_shocked_drawdown_above_hard_limit")
    if policy.require_trading_enabled and evidence.trading_enabled is not True:
        blockers.append("trading_status_authoritative_source_unavailable")
    if policy.require_contract_code and evidence.contract_code_present is not True:
        blockers.append("target_contract_code_missing")
    if policy.require_pending_nonce and evidence.pending_nonce is None:
        blockers.append("pending_nonce_unavailable")
    if policy.require_gas_estimate and evidence.gas_estimate is None and evidence.gas_estimate_status != "not_attempted_due_to_prerequisite":
        blockers.append("gas_estimate_unavailable")
    if policy.require_fee_evidence and evidence.estimated_fee_wei is None:
        blockers.append("fee_evidence_unavailable")
    if evidence.estimated_fee_wei is not None and evidence.estimated_fee_wei > policy.maximum_fee_wei:
        blockers.append("transaction_fee_limit_exceeded")
    if evidence.maximum_total_fee_wei is not None and evidence.maximum_total_fee_wei > policy.maximum_fee_wei:
        blockers.append("maximum_total_fee_limit_exceeded")
    if policy.require_balance_evidence and evidence.native_balance_wei is None and not evidence.evidence_statuses:
        blockers.append("balance_evidence_unavailable")
    if evidence.native_balance_wei is not None and evidence.estimated_fee_wei is not None and evidence.native_balance_wei < evidence.estimated_fee_wei:
        blockers.append("native_fee_balance_insufficient")
    if candidate is None:
        blockers.append("candidate_unavailable_or_invalid")
    elif candidate.noncrossing is not True or candidate.notional > policy.maximum_order_notional:
        blockers.append("candidate_order_policy_rejected")
    if evidence.evidence_statuses:
        # Keep only causal statuses for live evidence.  The legacy aliases are
        # retained for callers that already consume them, but generic RPC
        # cascades are intentionally removed.
        allowed = set(primary_blockers) | set(derived_blockers) | {
            "rpc_chain_mismatch", "candidate_order_policy_rejected",
        }
        blockers = [item for item in blockers if item in allowed]
    return _result(policy, evidence, tuple(dict.fromkeys(blockers)), candidate, mode=mode,
                   primary_blockers=primary_blockers, derived_blockers=derived_blockers,
                   not_attempted_stages=not_attempted_stages)


def _result(policy: DreamDexZeroMutationRehearsalPolicy, evidence: DreamDexZeroMutationRehearsalEvidence,
            blockers: tuple[str, ...], candidate: DreamDexRehearsalCandidate | None, *, mode: str = "fixture",
            primary_blockers: tuple[str, ...] = (), derived_blockers: tuple[str, ...] = (),
            not_attempted_stages: tuple[str, ...] = ()) -> DreamDexZeroMutationRehearsalResult:
    ready = not blockers
    payload = {"policy": policy.safe_dict(), "evidence": {k: v for k, v in evidence.safe_dict().items() if k not in {"observed_monotonic_ms"}}, "candidate": candidate.safe_dict() if candidate else None}
    fp = _safe_fp(payload)
    status = "available" if ready else "unavailable"
    return DreamDexZeroMutationRehearsalResult(
        schema_version=policy.schema_version,
        rehearsal_status="ready_for_human_review" if ready else "blocked",
        chain_evidence_status="available" if evidence.chain_id == policy.required_chain_id else "unavailable",
        market_evidence_status=evidence.market_status,
        orderbook_status=evidence.orderbook_status,
        account_evidence_status=evidence.account_status,
        market_rules_status=evidence.market_rules_status,
        trading_status=status if evidence.runtime_gate_status == "available" else evidence.runtime_gate_status,
        contract_code_status=evidence.target_code_status,
        pending_nonce_status=evidence.pending_nonce_status,
        gas_estimate_status=evidence.gas_estimate_status,
        fee_evidence_status=evidence.fee_status,
        balance_status=evidence.native_balance_status,
        risk_status=evidence.risk_status,
        fair_play_status=evidence.fair_play_status,
        runtime_launch_status=evidence.runtime_gate_status,
        unsigned_request_status=status if candidate else "unavailable",
        envelope_status=status if ready else "unavailable",
        preflight_status=status if ready else "unavailable",
        approval_preview_status=status if ready else "unavailable",
        approval_binding_status=status if ready else "unavailable",
        production_journal_write_performed=False,
        temporary_journal_used=False,
        temporary_journal_removed=False,
        approval_prompt_performed=False,
        keystore_read_performed=False,
        password_prompt_performed=False,
        signer_invocation_count=0,
        submission_call_count=0,
        mutation_rpc_call_count=0,
        network_read_call_count=evidence.network_read_call_count,
        ready_for_human_review=ready,
        ready_for_signer_invocation=False,
        ready_for_real_submission=False,
        rehearsal_fingerprint=fp,
        authoritative=False,
        blockers=blockers,
        validation_errors=(),
        candidate_fingerprint=candidate.candidate_fingerprint if candidate else None,
        gap_risk_status=evidence.gap_risk_status,
        gap_risk_budget_approved=evidence.gap_risk_budget_approved,
        mode=mode,
        public_market_call_count=evidence.public_market_call_count,
        authenticated_account_call_count=evidence.authenticated_account_call_count,
        read_only_rpc_call_count=evidence.read_only_rpc_call_count,
        public_market_snapshot_count=evidence.public_market_snapshot_count,
        public_http_request_count=evidence.public_http_request_count,
        authenticated_http_request_count=evidence.authenticated_http_request_count,
        read_only_rpc_request_count=evidence.read_only_rpc_request_count,
        total_network_read_request_count=evidence.total_network_read_request_count,
        projected_shocked_drawdown=evidence.projected_shocked_drawdown,
        maximum_total_fee_wei=evidence.maximum_total_fee_wei,
        transaction_type=evidence.transaction_type,
        source_authority=evidence.source_authority,
        market_identity_status=evidence.market_identity_status,
        account_identity_status=evidence.account_identity_status,
        account_authority_status=evidence.account_authority_status,
        open_order_status=evidence.open_order_status,
        fills_status=evidence.fills_status,
        trading_enabled=evidence.trading_enabled,
        open_order_count=evidence.open_order_count,
        evidence_statuses=evidence.evidence_statuses,
        native_gas_balance_evidence=evidence.native_gas_balance_evidence,
        authenticated_trading_balance_evidence=evidence.authenticated_trading_balance_evidence,
        available_order_currency_balance=evidence.available_order_currency_balance,
        available_base_asset_balance=evidence.available_base_asset_balance,
        primary_blockers=primary_blockers,
        derived_blockers=derived_blockers,
        not_attempted_stages=not_attempted_stages,
        configuration_status=evidence.configuration_status,
        market_listed_status=evidence.market_listed_status,
        market_lifecycle_status=evidence.market_lifecycle_status,
        trading_enabled_status=evidence.trading_enabled_status,
        place_operation_status=evidence.place_operation_status,
        cancel_operation_status=evidence.cancel_operation_status,
        trading_status_source=evidence.trading_status_source,
        trading_status_authority=evidence.trading_status_authority,
        address_role_configuration=evidence.address_role_configuration,
    )


__all__ = ["READ_ONLY_REHEARSAL_RPC_ALLOWLIST", "READ_ONLY_REHEARSAL_FORBIDDEN_PREFIXES", "READ_ONLY_REHEARSAL_FORBIDDEN_METHODS"] + [name for name in globals() if name.startswith("DreamDex") or name.startswith("build_") or name.startswith("collect_") or name.startswith("run_")]
