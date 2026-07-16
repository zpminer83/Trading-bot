"""Safe read-only DreamDEX state check (no order or transaction operations)."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import os
import re
from typing import Mapping

from bot.execution.dry_run_order_validator import DryRunOrderValidator, DryRunValidationLimits
from bot.execution.order import OrderIntent
from bot.integrations.dreamdex_auth_models import DreamDexAuthManager
from bot.integrations.dreamdex_authenticated_read_only import build_authenticated_read_only_transport_from_env
from bot.integrations.dreamdex_read_only import DreamDexReadOnlyAdapter, FixtureRpcTransport, FixtureTransport, load_fixture, mask_account_id
from bot.integrations.dreamdex_operator_permissions import (
    audit_fund_owner_semantics,
    audit_open_order_semantics,
    audit_vendor_selectors,
    build_authority_evidence,
    build_capability_matrix,
    build_vendor_snapshot_fingerprint,
    audit_typescript_python_parity,
    discover_operator_registry,
    load_operator_configuration,
    operator_blocking_reasons,
)
from bot.integrations.dreamdex_siwe_http_transport import build_siwe_http_transport_from_env
from bot.integrations.dreamdex_siwe_signer import build_production_siwe_signer_from_env, resolve_auth_mode
from bot.execution.dreamdex_direct_order_encoding import (
    audit_direct_owner_vendor,
    audit_direct_account_construction,
    build_direct_owner_identity,
    build_direct_signer_binding_evidence,
    build_direct_transaction_signer_requirements,
    direct_owner_blocking_reasons,
)
from bot.execution.dreamdex_unsigned_transaction import (
    UnavailableDreamDexTransactionTransport,
    build_unsigned_transaction_requirements,
)
from bot.execution.dreamdex_transaction_envelope import (
    VENDOR_GAS_POLICY_SUMMARY,
    build_transaction_type_policy_evidence,
    describe_transaction_envelope_capabilities,
)
from bot.execution.dreamdex_transaction_lifecycle import (
    create_prepared_lifecycle,
    describe_transaction_lifecycle_capabilities,
)
from bot.execution.dreamdex_order_reconciliation import (
    build_order_reconciliation_graph,
    build_order_reconciliation_preview,
    describe_order_reconciliation_capabilities,
)
from bot.execution.dreamdex_execution_primitives import (
    DreamDexExecutionBlockers,
    build_execution_capability_matrix,
    mask_hex_hash,
)
from bot.execution.dreamdex_reconciliation_bridge import (
    build_reconciliation_bridge_from_evidence,
    build_reconciliation_bridge_preview,
    describe_reconciliation_bridge_capabilities,
)
from bot.execution.dreamdex_transaction_signer import (
    UnavailableDreamDexTransactionSigner,
    DreamDexTransactionSigningPolicy,
    build_transaction_signing_preview,
)
from bot.execution.dreamdex_execution_primitives import mask_evm_address
from bot.execution.dreamdex_readonly_rpc import DreamDexReadOnlyRpcTransport
from bot.execution.dreamdex_transaction_preflight import (
    DreamDexTransactionPreflightPolicy,
    build_transaction_preflight_preview,
    run_transaction_preflight,
    unavailable_preflight_result,
)
from bot.execution.dreamdex_execution_journal import (
    DreamDexExecutionJournalPolicy,
    open_journal,
)
from bot.execution.dreamdex_signing_lease import (
    DreamDexLiveNonceRevalidationPolicy,
    build_signing_lease_preview,
    acquire_signing_lease,
    serialize_signing_lease_diagnostics,
)
from bot.execution.dreamdex_signed_transaction import build_signed_transaction_preview
from bot.execution.dreamdex_transaction_submission import build_transaction_submission_preview
from bot.integrations.dreamdex_authenticated_read_only import _parse_enable_flag


RECONCILIATION_BRIDGE_ENV = "DREAMDEX_ENABLE_RECONCILIATION_EVIDENCE_BRIDGE"
LIVE_PREFLIGHT_ENV = "DREAMDEX_ENABLE_LIVE_TRANSACTION_PREFLIGHT"
PREFLIGHT_RPC_ENV = "DREAMDEX_READ_ONLY_RPC_URL"
PREFLIGHT_ENABLE_ENV = LIVE_PREFLIGHT_ENV
JOURNAL_ENABLE_ENV = "DREAMDEX_ENABLE_EXECUTION_JOURNAL"
JOURNAL_PATH_ENV = "DREAMDEX_EXECUTION_JOURNAL_PATH"
JOURNAL_DIAGNOSTICS_ENV = "DREAMDEX_EXECUTION_JOURNAL_DIAGNOSTICS"
LIVE_NONCE_REVALIDATION_ENV = "DREAMDEX_ENABLE_LIVE_NONCE_REVALIDATION"
SIGNING_LEASE_ENABLE_ENV = "DREAMDEX_ENABLE_LIVE_SIGNING_LEASE"
LIVE_NONCE_MAX_AGE_ENV = "DREAMDEX_LIVE_NONCE_MAX_AGE_MS"
RAW_SUBMISSION_ENABLE_ENV = "DREAMDEX_ENABLE_RAW_TRANSACTION_SUBMISSION"


def _decimal_env(name: str, default: Decimal) -> Decimal:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return Decimal(raw)
    except (InvalidOperation, ValueError):
        raise ValueError(f"{name} must be a decimal")


def raw_transaction_submission_flag(environ: Mapping[str, str]) -> str:
    """Return a strict opt-in flag; no payload is accepted from environment."""
    value = _parse_enable_flag(environ.get(RAW_SUBMISSION_ENABLE_ENV))
    if value == "invalid":
        raise ValueError(f"{RAW_SUBMISSION_ENABLE_ENV} must be a strict boolean")
    return value


def _safe_error(exc: Exception, owner: str | None = None) -> str:
    message = re.sub(r"(?i)(private[_ -]?key|seed[_ -]?phrase|api[_ -]?secret|authorization|bearer|signature)\s*[:=]\s*\S+", r"\1=<redacted>", str(exc))
    return message.replace(owner, mask_account_id(owner))[:300] if owner else message[:300]


def _source(value) -> str:
    if value is None:
        return "unavailable"
    if value.value is not None:
        return f"{value.status}: {value.value}"
    suffix = f" error_code={value.error_code}" if getattr(value, "error_code", None) else ""
    return f"{value.status}: {value.reason or 'unavailable'}{suffix}"


def _source_status(value) -> str:
    if value is None:
        return "unavailable (error_code=unavailable)"
    suffix = f" (error_code={value.error_code})" if getattr(value, "error_code", None) else ""
    return f"{value.status}{suffix}"


def _masked(value: str | None) -> str:
    return mask_account_id(value) if value else "<unresolved>"


def _print_schema_fingerprint(label: str, fingerprint) -> None:
    if fingerprint is None:
        print(f"Authenticated {label} schema: unavailable")
        return
    print(f"Authenticated {label} schema:")
    print(f"  HTTP status: {fingerprint.http_status if fingerprint.http_status is not None else 'unavailable'}")
    print(f"  top-level type: {fingerprint.top_level_type}")
    fields = ", ".join(f"{name}:{kind}" for name, kind in fingerprint.field_types) or "none"
    nested = ", ".join(f"{name}:{kind}" for name, kind in fingerprint.nested_field_types) or "none"
    lengths = ", ".join(f"{name}={length}" for name, length in fingerprint.list_lengths) or "none"
    pagination = ", ".join(fingerprint.pagination_field_names) or "none"
    print(f"  fields: {fields}")
    print(f"  nested fields: {nested}")
    print(f"  list lengths: {lengths}")
    if label == "order list":
        print(f"  pagination field names: {pagination}")


def _print_address_diagnostics(label: str, diagnostics, fallback_address: str | None = None) -> None:
    print(f"{label}:")
    if diagnostics is None:
        print(f"  address masked: {_masked(fallback_address)}")
        print("  platform role: unknown")
        print("  platform role status: unavailable")
        print("  on-chain code type: unavailable")
        print("  on-chain code status: unavailable (error_code=unavailable)")
        print("  deployment status: unavailable")
        print("  account abstraction status: unavailable")
        for name in ("native SOMI", "wallet SOMI", "wallet USDso", "vault SOMI", "vault USDso"):
            print(f"  {name}: unavailable (error_code=unavailable)")
        return
    print(f"  address masked: {_masked(diagnostics.address)}")
    code_suffix = "" if diagnostics.onchain_code_status == "available" else f" (error_code={diagnostics.code.error_code})"
    print(f"  platform role: {diagnostics.platform_role}")
    print(f"  platform role status: {diagnostics.platform_role_status}")
    print(f"  on-chain code type: {diagnostics.onchain_code_type}")
    print(f"  on-chain code status: {diagnostics.onchain_code_status}{code_suffix}")
    print(f"  deployment status: {diagnostics.deployment_status}")
    print(f"  account abstraction status: {diagnostics.account_abstraction_status}")
    print(f"  base asset kind: {diagnostics.base_token.asset_kind.value}")
    print(f"  base token code status: {_source_status(diagnostics.base_token.code)}")
    print(f"  base raw balance status: {_source_status(diagnostics.base_token.raw_balance)}")
    print(f"  base decimals: {_source(diagnostics.base_token.decimals)}")
    print(f"  base balance read method: {diagnostics.base_token.balance_method}")
    print(f"  quote asset kind: {diagnostics.quote_token.asset_kind.value}")
    print(f"  quote token code status: {_source_status(diagnostics.quote_token.code)}")
    print(f"  quote raw balance status: {_source_status(diagnostics.quote_token.raw_balance)}")
    print(f"  quote decimals: {_source(diagnostics.quote_token.decimals)}")
    print(f"  quote balance read method: {diagnostics.quote_token.balance_method}")
    for name, value in (("native SOMI", diagnostics.native_gas), ("wallet SOMI", diagnostics.wallet_somi), ("wallet USDso", diagnostics.wallet_usdso), ("vault SOMI", diagnostics.vault_somi), ("vault USDso", diagnostics.vault_usdso)):
        print(f"  {name}: {_source(value)}")


def _print_identity_binding_evidence(evidence) -> None:
    print("IDENTITY BINDING:")
    if evidence is None:
        print("  binding status: unavailable")
        print("  authoritative: NO")
        print("  unresolved reasons: identity_binding_evidence_unavailable")
        return
    safe = evidence.safe_dict()
    print(f"  owner: {safe['owner_address']} ({safe['owner_platform_role']})")
    print(f"  trading: {safe['trading_address']} ({safe['trading_platform_role']})")
    print(f"  UI role confirmation: {safe['ui_role_confirmation']}")
    print(f"  authenticated vault evidence: {safe['authenticated_vault_probe_status']}")
    print(f"  authenticated orders evidence: {safe['authenticated_order_probe_status']}")
    print(f"  configured query address match: {safe['authenticated_query_address_match']}")
    print(f"  official mapping status: {safe['official_mapping_status']}")
    print(f"  binding status: {safe['binding_status']}")
    print(f"  authoritative: {'YES' if safe['authoritative'] else 'NO'}")
    print(f"  unresolved reasons: {', '.join(safe['unresolved_reasons']) or 'none'}")


def _print_operator_session_model(account, market) -> tuple[str, ...]:
    """Print only source/status evidence; never perform an operator RPC call."""
    print("OPERATOR / SESSION-KEY MODEL:")
    vendor = build_vendor_snapshot_fingerprint()
    config = load_operator_configuration(
        os.environ,
        contest_owner_address=getattr(account, "owner_address", None),
        platform_trading_address=getattr(account, "trading_address", None),
    )
    identity = config.identity.safe_dict()
    selectors = {item.capability: item for item in audit_vendor_selectors(vendor)}
    matrix = build_capability_matrix(vendor)
    authority = build_authority_evidence(
        pool=getattr(market, "pool_address", None),
        owner=config.identity.onchain_fund_owner_address,
        operator=config.identity.operator_address,
        selector_evidence=selectors,
        snapshot=vendor,
    )
    open_orders = audit_open_order_semantics(vendor)
    parity = audit_typescript_python_parity(vendor)
    registry = discover_operator_registry()
    owner_semantics = audit_fund_owner_semantics(vendor)
    print(f"  vendor snapshot status: {vendor.status}")
    print(f"  vendor source fingerprint: {vendor.fingerprint}")
    print(f"  vendor package version: {vendor.package_version or 'unavailable'}")
    print(f"  vendor commit SHA: {vendor.commit_sha or 'unavailable'}")
    print(f"  contest owner: {identity['contest_owner_address']}")
    print(f"  platform trading wallet: {identity['platform_trading_address']}")
    print(f"  on-chain fund owner: {identity['onchain_fund_owner_address']}")
    print(f"  operator address: {identity['operator_address']}")
    print(f"  role mapping status: {identity['role_mapping_status']}")
    print(f"  operator configured: {'YES' if config.operator_configured else 'NO'}")
    print(f"  fund owner configured: {'YES' if config.fund_owner_configured else 'NO'}")
    print(f"  permission probe enabled: {'YES' if config.permission_probe_enabled else 'NO'}")
    print(f"  permission probe flag status: {config.enable_flag_status}")
    print(f"  registry address: {_masked(registry.registry_address)}")
    print(f"  registry address status: {registry.status}")
    print(f"  registry chain ID: {registry.chain_id}")
    print(f"  registry source file: {registry.addresses[0].source_file if registry.addresses else 'unavailable'}")
    print(f"  registry source fingerprint: {registry.addresses[0].source_fingerprint if registry.addresses else 'unavailable'}")
    print(f"  registry conflicts: {', '.join(registry.conflicts) or 'none'}")
    print(f"  pool address status: {'available' if getattr(market, 'pool_address', None) else 'unavailable'}")
    for name, capability in (("placeOrderFor", matrix.place_order_for), ("cancelOrderFor", matrix.cancel_order_for), ("reduceOrderFor", matrix.reduce_order_for)):
        selector = capability.selector or "unavailable"
        print(f"  {name} selector: {selector} ({selectors.get(capability.name).status if selectors.get(capability.name) else 'unavailable'})")
    print(f"  selector consistency: {matrix.selector_consistency}")
    print("  per-pool permission evidence: unconfigured")
    print("  global permission evidence: unconfigured")
    print("  denial evidence: unconfigured")
    print(f"  effective place authority: {authority.effective_place_status}")
    print(f"  effective cancel authority: {authority.effective_cancel_status}")
    print(f"  effective reduce authority: {authority.effective_reduce_status}")
    print(f"  open-order subject semantics: {open_orders.status}")
    print(f"  TypeScript/Python parity: {parity.status}")
    print(f"  fund-owner semantics: {owner_semantics.status}")
    print(f"  isOperatorAuthorized owner parameter: {owner_semantics.owner_parameter}")
    print(f"  placeOrderFor owner subject: {owner_semantics.place_order_for_owner}")
    print(f"  cancelOrderFor owner subject: {owner_semantics.cancel_order_for_owner}")
    print(f"  authority authoritative: {'YES' if authority.authoritative else 'NO'}")
    reasons = list(identity["unresolved_reasons"]) + list(authority.unresolved_reasons) + list(open_orders.unresolved_reasons) + list(operator_blocking_reasons(configuration=config, matrix=matrix, authority=authority, parity=parity))
    print(f"  unresolved reasons: {', '.join(dict.fromkeys(reasons)) or 'none'}")
    return tuple(dict.fromkeys(reasons))


def _print_direct_owner_execution_model(account, market) -> tuple[str, ...]:
    """Print source-backed direct-owner semantics without enabling execution."""
    audit = audit_direct_owner_vendor()
    identity = build_direct_owner_identity(
        contest_login_address=getattr(account, "owner_address", None),
        configured_owner_address=getattr(account, "owner_address", None),
        platform_trading_address=getattr(account, "trading_address", None),
        authenticated_api_subject=getattr(getattr(account, "authenticated", None), "authenticated_subject", None),
    )
    binding = build_direct_signer_binding_evidence(
        contest_owner_address=getattr(account, "owner_address", None),
        platform_trading_address=getattr(account, "trading_address", None),
    )
    requirements = build_direct_transaction_signer_requirements(binding.direct_signer_address)
    trace = audit_direct_account_construction()
    safe_binding = binding.safe_dict()
    print("DIRECT OWNER EXECUTION MODEL:")
    print(f"  selected execution mode: {audit.selected_mode}")
    print(f"  operator mode active: {'YES' if audit.operator_mode_active else 'NO'}")
    print(f"  vendor snapshot status: {'source_confirmed' if audit.vendor_files else 'unavailable'}")
    print(f"  vendor audited files: {len(audit.vendor_files)}")
    print(f"  vendor audit fingerprint: {audit.vendor_fingerprint}")
    print(f"  contest login wallet: {_masked(identity.contest_login_address)}")
    print(f"  platform trading wallet: {_masked(identity.platform_trading_address)}")
    print(f"  transaction signer role: {identity.transaction_signer_role}")
    print(f"  account constructor status: {safe_binding['account_constructor_status']}")
    print(f"  account constructor type: {safe_binding['account_constructor_type']}")
    print(f"  wallet client account binding: {safe_binding['wallet_client_binding_status']}")
    print(f"  execution context account binding: {safe_binding['execution_context_binding_status']}")
    print(f"  transaction from semantics: {safe_binding['transaction_from_semantics']}")
    print(f"  direct signer configured: {'YES' if safe_binding['direct_signer_configured'] in {'user_declared', 'source_compatible', 'source_conflicting'} else 'NO'}")
    print(f"  direct signer address: {safe_binding['direct_signer_address']}")
    print(f"  direct signer source compatibility: {safe_binding['source_compatibility_status']}")
    print(f"  direct signer matches contest owner: {safe_binding['configured_owner_match_status']}")
    print(f"  direct signer matches Smart Wallet: {safe_binding['configured_trading_match_status']}")
    print(f"  direct signer role candidate: {safe_binding['signer_role']}")
    print(f"  direct signer key availability: {safe_binding['key_availability']}")
    print(f"  Smart Wallet used in signing path: {'YES' if safe_binding['smart_wallet_used_in_signing_path'] is True else ('NO' if safe_binding['smart_wallet_used_in_signing_path'] is False else 'unresolved')}")
    print(f"  TypeScript direct support: source_confirmed")
    print(f"  Python direct support: {safe_binding['python_parity_status']}")
    if safe_binding["python_parity_status"] == "partial":
        print("  Python direct diagnostic: python_direct_execution_partial (informational)")
    print(f"  TypeScript/Python parity: {safe_binding['python_parity_status']}")
    print(f"  required signer capabilities: {', '.join(requirements.capabilities)}")
    print(f"  source trace status: {safe_binding['source_trace_status']}")
    print(f"  source trace files: {', '.join(safe_binding['evidence_sources']) or 'unavailable'}")
    print(f"  source trace roles: {', '.join(f'{source}={role}' for source, role in trace.source_roles) or 'unavailable'}")
    print(f"  source trace fingerprints: {', '.join(f'{source}={digest}' for source, digest in trace.source_fingerprints) or 'unavailable'}")
    print(f"  source trace steps: {'; '.join(trace.trace_steps) or 'unavailable'}")
    print(f"  direct signer binding authoritative: {'YES' if safe_binding['authoritative'] else 'NO'}")
    print("  signer candidate matrix:")
    for candidate in binding.candidate_matrix:
        item = candidate.safe_dict()
        print(
            f"    {item['candidate']}: address={item['address']}, "
            f"ctx.account={item['compatible_with_context_account']}, "
            f"tx_sender={item['used_as_transaction_sender']}, "
            f"vault={item['used_as_vault_subject']}, "
            f"rest={item['used_as_rest_subject']}, "
            f"authoritative={'YES' if item['authoritative'] else 'NO'}"
        )
    capabilities = UnavailableDreamDexTransactionTransport().describe_capabilities()
    requirements = build_unsigned_transaction_requirements(
        operation="place_order",
        from_address=binding.direct_signer_address,
        to_address=getattr(market, "pool_address", None),
    )
    print("  unsigned transaction model: available_offline")
    print(f"  unsigned place builder: {capabilities['build_unsigned_place']}")
    print(f"  unsigned cancel builder: {capabilities['build_unsigned_cancel']}")
    print(f"  unsigned reduce builder: {capabilities['build_unsigned_reduce']}")
    print("  unsigned validation: available_offline")
    print("  unsigned preview: available_offline")
    print(f"  transaction chain ID: {requirements.required_chain_id}")
    print(f"  transaction from binding: {safe_binding['transaction_from_semantics']}")
    print(f"  transaction target binding: {'source_confirmed' if getattr(market, 'pool_address', None) else 'unavailable'}")
    print("  raw calldata output allowed: NO")
    print("  gas resolution: unavailable")
    print("  nonce resolution: unavailable")
    print("  fee resolution: unavailable")
    print(f"  signing capability: {capabilities['sign_transaction']}")
    print(f"  submission capability: {capabilities['submit_transaction']}")
    print(f"  receipt capability: {capabilities['wait_for_receipt']}")
    print("  unsigned request authoritative: NO")
    print("  unsigned request ready for signing: NO")
    print("  unsigned request ready for submission: NO")
    print("  unsigned transaction blockers: direct_transaction_transport_unimplemented; direct_signer_key_unavailable; direct_signer_binding_non_authoritative")
    envelope_capabilities = describe_transaction_envelope_capabilities()
    print("  transaction envelope model: available_offline")
    print(f"  envelope builder: {envelope_capabilities['build_unsigned_envelope']}")
    print(f"  envelope validation: {envelope_capabilities['validate_unsigned_envelope']}")
    print("  request fingerprint: unavailable")
    print("  envelope fingerprint: unavailable")
    fee_fingerprints = dict(audit.vendor_file_fingerprints)
    fee_policy = build_transaction_type_policy_evidence(fee_fingerprints)
    print(f"  transaction type policy: {fee_policy.transaction_type_status}")
    print(f"  transaction type source status: {fee_policy.source_status}")
    print(f"  transaction type source: {', '.join(fee_policy.source_paths)}")
    fingerprints_text = ", ".join(f"{path}={digest}" for path, digest in fee_policy.source_fingerprints) or "unavailable"
    print(f"  transaction type source fingerprints: {fingerprints_text}")
    print(f"  transaction type audit: {fee_policy.fee_semantics}")
    print(f"  transaction type conflicts: {', '.join(fee_policy.conflicts) or 'none'}")
    print(f"  vendor gas policy (informational): {VENDOR_GAS_POLICY_SUMMARY}")
    print(f"  nonce resolution capability: {envelope_capabilities['resolve_nonce']}")
    print(f"  gas estimation capability: {envelope_capabilities['estimate_gas']}")
    print(f"  fee resolution capability: {envelope_capabilities['resolve_fees']}")
    print("  externally supplied envelope accepted: NO")
    print("  envelope structurally complete: NO")
    print("  envelope evidence authoritative: NO")
    print("  envelope ready for signing: NO")
    print("  envelope ready for submission: NO")
    print("  envelope raw calldata output allowed: NO")
    print("  envelope blockers: transaction_envelope_unavailable; transaction_type_policy_unresolved; transaction_nonce_unresolved; transaction_gas_unresolved; transaction_fees_unresolved; transaction_envelope_non_authoritative")
    lifecycle_capabilities = describe_transaction_lifecycle_capabilities()
    lifecycle_default = create_prepared_lifecycle(None)
    print("  transaction lifecycle model: available_offline")
    print(f"  prepared lifecycle builder: {lifecycle_capabilities['create_prepared_lifecycle']}")
    print(f"  external submission import: {lifecycle_capabilities['import_external_submission']}")
    print(f"  receipt evidence validation: {lifecycle_capabilities['validate_receipt_evidence']}")
    print(f"  event evidence validation: {lifecycle_capabilities['validate_event_evidence']}")
    print(f"  lifecycle transition validation: {lifecycle_capabilities['validate_state_transition']}")
    print(f"  receipt fetch capability: {lifecycle_capabilities['fetch_receipt']}")
    print(f"  log fetch capability: {lifecycle_capabilities['fetch_logs']}")
    print(f"  replacement detection capability: {lifecycle_capabilities['detect_replacement_live']}")
    print(f"  confirmation wait capability: {lifecycle_capabilities['wait_for_confirmations']}")
    print("  transaction hash: <missing>")
    print(f"  lifecycle state: {lifecycle_default.current_state}")
    print("  receipt confirmation: unavailable")
    print("  required event confirmation: unavailable")
    print("  order ID confirmation: unavailable")
    print("  lifecycle authoritative: NO")
    print("  lifecycle reconciliation: incomplete")
    print("  raw receipt output allowed: NO")
    print("  raw event output allowed: NO")
    print("  lifecycle blockers: transaction_submission_evidence_unavailable; transaction_receipt_evidence_unavailable; transaction_event_evidence_unavailable; transaction_lifecycle_non_authoritative; order_id_lifecycle_unconfirmed")
    print("  event audit source: packages/core/src/contract.ts")
    event_fingerprints = dict(audit.vendor_file_fingerprints)
    print(f"  event audit source fingerprint: {event_fingerprints.get('packages/core/src/contract.ts', 'unavailable')}")
    print(f"  transaction sender: {_masked(audit.identity.transaction_sender_address)}")
    print(f"  contract order owner subject: {audit.identity.contract_order_owner_subject}")
    print(f"  vault owner subject: {audit.identity.vault_owner_subject}")
    print(f"  Smart Wallet semantics: {audit.smart_wallet_semantics}")
    print(f"  owner/Smart Wallet mapping: {identity.mapping_status}")
    print(f"  native value semantics: {audit.native_value_semantics}")
    for operation in audit.operations:
        label = operation.operation.replace("_", " ")
        print(f"  {label} transport: {operation.transport}")
        print(f"  {label} target: {getattr(market, 'pool_address', None) or operation.target}")
        print(f"  {label} method: {operation.method or 'unavailable'}")
        print(f"  {label} selector: {operation.selector or 'unavailable'}")
        print(f"  {label} signer required: {operation.signer_requirement}")
        print(f"  {label} native value: {operation.value_requirement}")
        print(f"  {label} receipt confirmation: {operation.receipt_requirement}")
        if operation.operation in {"place_order", "cancel_order", "reduce_order"}:
            print(f"  {label} transaction from: {operation.from_semantics}")
            print(f"  {label} chain ID semantics: {operation.chain_id_semantics}")
            print(f"  {label} nonce semantics: {operation.nonce_semantics}")
            print(f"  {label} gas policy: {operation.gas_policy}")
            print(f"  {label} fee fields: {operation.fee_fields}")
            print(f"  {label} revert handling: {operation.revert_handling}")
            print(f"  {label} replacement behavior: {operation.replacement_behavior}")
            print(f"  {label} timeout behavior: {operation.timeout_behavior}")
    print(f"  order ID source: {audit.order_id_source}")
    print("  receipt event semantics: OrderPlaced/OrderCancelled topics are source-confirmed")
    print("  SIWE signer sufficient for orders: NO")
    print("  transaction signer capability: unavailable")
    print(f"  direct execution authoritative: {'YES' if audit.authoritative else 'NO'}")
    print(f"  unresolved reasons: {', '.join(audit.unresolved_reasons) or 'none'}")
    lifecycle_blockers = (
        "transaction_submission_evidence_unavailable",
        "transaction_receipt_evidence_unavailable",
        "transaction_event_evidence_unavailable",
        "transaction_lifecycle_non_authoritative",
        "transaction_replacement_status_unavailable",
        "order_id_lifecycle_unconfirmed",
    )
    return tuple(dict.fromkeys((*direct_owner_blocking_reasons(audit, binding=binding), *lifecycle_blockers)))


def _print_market_trading_rules(market) -> None:
    rules = getattr(market, "trading_rules", None)
    print("MARKET TRADING RULES:")
    if rules is None:
        print("  source status: unavailable")
        print("  schema status: unavailable")
        return
    print(f"  symbol: {rules.symbol or market.symbol}")
    print(f"  source status: {rules.source_status}")
    print(f"  schema status: {rules.schema_status}")
    print(f"  market status: {rules.market_status or 'unavailable'}")
    print(f"  trading enabled: {'YES' if rules.trading_enabled is True else 'NO'}")
    for label, value in (("tick size", rules.tick_size), ("quantity step", rules.quantity_step), ("minimum quantity", rules.minimum_quantity), ("minimum notional", rules.minimum_notional), ("base decimals", rules.base_decimals), ("quote decimals", rules.quote_decimals), ("price decimals", rules.price_decimals), ("quantity decimals", rules.quantity_decimals)):
        print(f"  {label}: {value if value is not None else 'unavailable'}")
    print(f"  confirmed order types: {', '.join(rules.confirmed_order_types) if rules.confirmed_order_types is not None else 'unavailable'}")
    print(f"  authoritative fields: {', '.join(rules.authoritative_fields) or 'none'}")
    print(f"  unavailable fields: {', '.join(rules.unavailable_fields) or 'none'}")
    print(f"  conflicts: {', '.join(rules.conflicts) or 'none'}")
    fingerprint = getattr(market, "schema_fingerprint", None)
    if fingerprint is not None:
        fields = ", ".join(f"{name}:{kind}" for name, kind in fingerprint.field_types) or "none"
        nested = ", ".join(f"{name}:{kind}" for name, kind in fingerprint.nested_field_types) or "none"
        print("  public schema fingerprint: observed")
        print(f"    endpoint: {fingerprint.endpoint_name}")
        print(f"    HTTP status: {fingerprint.http_status if fingerprint.http_status is not None else 'unavailable'}")
        print(f"    top-level type: {fingerprint.top_level_type}")
        print(f"    fields: {fields}")
        print(f"    nested fields: {nested}")
        print(f"    list lengths: {', '.join(f'{name}={length}' for name, length in fingerprint.list_lengths) or 'none'}")


def _print_order_reconciliation_graph() -> tuple[str, ...]:
    """Print the empty production-default graph without importing live evidence."""
    capabilities = describe_order_reconciliation_capabilities()
    graph = build_order_reconciliation_graph()
    preview = build_order_reconciliation_preview(graph)
    print("ORDER RECONCILIATION GRAPH:")
    print("  reconciliation graph model: available_offline")
    print(f"  graph builder: {capabilities['build_reconciliation_graph']}")
    print(f"  graph validation: {capabilities['validate_reconciliation_graph']}")
    print(f"  request/envelope correlation: {capabilities['correlate_request_envelope']}")
    print(f"  lifecycle correlation: {capabilities['correlate_transaction_lifecycle']}")
    print(f"  order metadata correlation: {capabilities['correlate_order_metadata']}")
    print(f"  authenticated order correlation: {capabilities['correlate_authenticated_orders']}")
    print(f"  on-chain fill correlation: {capabilities['correlate_onchain_fills']}")
    print("  graph fingerprint: unavailable")
    print(f"  root transaction hash: {preview.transaction_hash_masked}")
    print(f"  root order ID: {preview.order_id_status}")
    print(f"  graph node count: {preview.node_count}")
    print(f"  graph edge count: {preview.edge_count}")
    print(f"  confirmed edges: {preview.confirmed_edges}")
    print(f"  partial edges: {preview.partial_edges}")
    print(f"  mismatch edges: {preview.mismatched_edges}")
    print(f"  unavailable edges: {preview.unavailable_edges}")
    print(f"  account match: {preview.account_match_status}")
    print(f"  market match: {preview.market_match_status}")
    print(f"  lifecycle link: {preview.lifecycle_status}")
    print(f"  metadata link: {preview.metadata_status}")
    print(f"  authenticated current-state link: {preview.authenticated_order_status}")
    print(f"  fill coverage: {preview.fills_status}")
    print("  replacement status: unavailable")
    print("  reorg status: unavailable")
    print(f"  graph authoritative: {'YES' if preview.authoritative else 'NO'}")
    print(f"  graph reconciliation status: {preview.reconciliation_status}")
    print(f"  reconciliation complete: {'YES' if graph.reconciliation_complete else 'NO'}")
    print("  raw evidence output allowed: NO")
    print(f"  graph blockers: {'; '.join(graph.blockers) or 'none'}")
    return graph.blockers


def _print_execution_pipeline_summary() -> None:
    """Print the offline execution architecture without performing I/O."""
    matrix = build_execution_capability_matrix(
        blockers=DreamDexExecutionBlockers.PRODUCTION_DEFAULT_ACTIVE,
    )
    print("EXECUTION PIPELINE SUMMARY:")
    print("  encoding: source_confirmed")
    for label, name in (
        ("unsigned request", "build_unsigned_place"),
        ("envelope", "build_unsigned_envelope"),
        ("lifecycle", "create_prepared_lifecycle"),
        ("reconciliation", "build_reconciliation_graph"),
        ("signing", "sign_transaction"),
        ("submission", "submit_transaction"),
    ):
        print(f"  {label}: {matrix.by_name(name).status}")
    print("  receipt/log fetch: unavailable")
    print("  pipeline authoritative: NO")
    print("  pipeline ready for signing: NO")
    print("  pipeline ready for submission: NO")
    print(f"  active blocker count: {len(matrix.blockers)}")
    print(f"  capability fingerprint: {matrix.fingerprint}")


def _print_reconciliation_evidence_bridge(snapshot, *, enabled: bool) -> None:
    """Print bridge diagnostics from evidence already materialised by snapshot."""
    print("READ-ONLY RECONCILIATION EVIDENCE BRIDGE:")
    print(f"  bridge enabled: {'YES' if enabled else 'NO'}")
    print(f"  bridge execution performed: {'YES' if enabled else 'NO'}")
    print("  network attempt caused by bridge: NO")
    if not enabled:
        print("  authenticated account evidence: unavailable")
        print("  authenticated order evidence: unavailable")
        print("  authenticated open-order evidence: unavailable")
        print("  authenticated pagination complete: NO")
        print("  order metadata evidence: unavailable")
        print("  order metadata record count: 0")
        print("  order metadata conflict count: 0")
        print("  on-chain fill evidence: unavailable")
        print("  on-chain fill record count: 0")
        print("  on-chain fill duplicate count: 0")
        print("  on-chain fill pagination complete: NO")
        print("  on-chain fill reorg status: unavailable")
        print("  lifecycle evidence: unavailable")
        print("  lifecycle record count: 0")
        print("  eligible root count: 0")
        print("  graph count: 0")
        print("  authoritative graph count: 0")
        print("  complete graph count: 0")
        print("  conflicting graph count: 0")
        print("  unrelated evidence count: 0")
        print("  bundle fingerprint: unavailable")
        print("  bridge fingerprint: unavailable")
        print("  bridge authoritative: NO")
        print("  bridge reconciliation complete: NO")
        print("  raw authenticated payload output allowed: NO")
        print("  raw on-chain evidence output allowed: NO")
        print("  bridge blockers: none (flag disabled; no global blocker changed)")
        return
    account = snapshot.account
    authenticated = account.authenticated
    source = account.onchain_fills.source_status
    result = build_reconciliation_bridge_from_evidence(
        lifecycle_records=(),
        authenticated_account=authenticated.balances_status,
        authenticated_orders=authenticated.recent_orders,
        authenticated_open_orders=authenticated.open_orders,
        authenticated_order_source_status=authenticated.recent_orders_status,
        authenticated_open_order_source_status=authenticated.open_orders_status,
        order_metadata_records=(),
        onchain_fills=account.onchain_fills,
        authenticated_subject=None,
        authenticated_address_semantics=account.account_address_semantics,
        authenticated_pagination_complete=authenticated.pagination_complete,
        fill_pagination_complete=account.onchain_fills.pagination_complete,
        fill_reorg_status=source.reorg_status,
        order_metadata_source_status=None,
        onchain_fill_source_status=source,
    )
    preview = build_reconciliation_bridge_preview(result)
    print(f"  authenticated account evidence: {result.evidence_bundle.inventory.authenticated_account_status}")
    print(f"  authenticated order evidence: {preview.authenticated_order_evidence_status}")
    print(f"  authenticated open-order evidence: {preview.authenticated_open_order_evidence_status}")
    print(f"  authenticated pagination complete: {'YES' if preview.authenticated_pagination_complete else 'NO'}")
    print(f"  order metadata evidence: {preview.metadata_evidence_status}")
    print(f"  order metadata record count: {result.evidence_bundle.inventory.order_metadata_record_count}")
    print(f"  order metadata conflict count: {result.evidence_bundle.inventory.order_metadata_conflict_count}")
    print(f"  on-chain fill evidence: {preview.fill_evidence_status}")
    print(f"  on-chain fill record count: {result.evidence_bundle.inventory.onchain_fill_record_count}")
    print(f"  on-chain fill duplicate count: {result.evidence_bundle.inventory.onchain_fill_duplicate_count}")
    print(f"  on-chain fill pagination complete: {'YES' if preview.fill_pagination_complete else 'NO'}")
    print(f"  on-chain fill reorg status: {preview.fill_reorg_status}")
    print(f"  lifecycle evidence: {preview.lifecycle_evidence_status}")
    print(f"  lifecycle record count: {preview.root_lifecycle_count}")
    print(f"  eligible root count: {preview.eligible_root_count}")
    print(f"  graph count: {preview.graph_count}")
    print(f"  authoritative graph count: {preview.authoritative_graph_count}")
    print(f"  complete graph count: {preview.complete_graph_count}")
    print(f"  conflicting graph count: {preview.conflicting_graph_count}")
    print(f"  unrelated evidence count: {preview.unrelated_evidence_count}")
    print(f"  bundle fingerprint: {preview.bundle_fingerprint or 'unavailable'}")
    print(f"  bridge fingerprint: {preview.bridge_fingerprint or 'unavailable'}")
    print(f"  bridge authoritative: {'YES' if preview.authoritative else 'NO'}")
    print(f"  bridge reconciliation complete: {'YES' if preview.reconciliation_complete else 'NO'}")
    print("  raw authenticated payload output allowed: NO")
    print("  raw on-chain evidence output allowed: NO")
    print(f"  bridge blockers: {'; '.join(preview.blockers) or 'none'}")


def _print_authentication_state(account) -> None:
    snapshot = getattr(account, "auth_snapshot", None)
    print("AUTHENTICATION STATE:")
    if snapshot is None:
        print("  auth mode: none")
        print("  manager configured: NO")
        print("  managed auth manager configured: NO")
        print("  signer configured: NO")
        print("  signer status: unavailable")
        print("  signer capability: unavailable")
        print("  signer address: <unresolved>")
        print("  signer address match: unresolved")
        print("  transport configured: NO")
        print("  SIWE HTTP transport configured: NO")
        print("  manual bearer transport configured: NO")
        print("  manual authenticated transport configured: NO")
        print("  SIWE HTTP transport status: disabled")
        print("  auth state: unconfigured")
        print("  auth network attempt performed: NO")
        print("  nonce request performed: NO")
        print("  signer invocation performed: NO")
        print("  signature verification performed: NO")
        print("  signature verification status: unavailable")
        print("  recovered signer address: <missing>")
        print("  recovered signer address match: unresolved")
        print("  signed message integrity: unresolved")
        print("  signer/owner cryptographic match: unresolved")
        print("  external signer configured: NO")
        print("  external signer process started: NO")
        print("  external signer protocol status: unavailable")
        print("  external signer describe performed: NO")
        print("  external signer sign performed: NO")
        print("  external signer exit status: unavailable")
        print("  external signer address match: unresolved")
        print("  external signer environment isolated: unavailable")
        print("  external signer message integrity: unavailable")
        print("  external signer signature verification: unavailable")
        print("  login request performed: NO")
        print("  token present: NO")
        print("  expiry status: unavailable")
        print("  refresh required: NO")
        print("  authenticated subject: <unresolved>")
        print("  identity authoritative: NO")
        print("  owner match: unresolved")
        print("  trading match: unresolved")
        print("  operator match: unresolved")
        print("  address semantics: unresolved")
        print("  unresolved reasons: authentication_manager_unconfigured")
        return
    safe = snapshot.safe_dict()
    manual_configured = bool(getattr(account, "authenticated_transport_status", "") == "configured")
    auth_mode = resolve_auth_mode(
        manual_bearer_configured=manual_configured,
        managed_siwe_configured=bool(safe.get('manager_configured')),
    )
    print(f"  auth mode: {auth_mode}")
    print(f"  manager configured: {'YES' if safe.get('manager_configured') else 'NO'}")
    print(f"  managed auth manager configured: {'YES' if safe.get('manager_configured') else 'NO'}")
    print(f"  signer configured: {'YES' if safe.get('signer_configured') else 'NO'}")
    print(f"  signer status: {safe.get('signer_status', 'unavailable')}")
    print(f"  signer capability: {safe.get('signer_capability') or 'unavailable'}")
    print(f"  signer address: {safe.get('signer_address', '<unresolved>')}")
    print(f"  signer address match: {safe.get('signer_address_match', 'unresolved')}")
    print(f"  transport configured: {'YES' if safe.get('transport_configured') else 'NO'}")
    print(f"  SIWE HTTP transport configured: {'YES' if safe.get('transport_configured') else 'NO'}")
    print(f"  manual bearer transport configured: {'YES' if manual_configured else 'NO'}")
    print(f"  manual authenticated transport configured: {'YES' if manual_configured else 'NO'}")
    print(f"  SIWE HTTP transport status: {safe.get('transport_status', 'unconfigured')}")
    print(f"  auth state: {safe.get('state', 'failed_closed')}")
    print(f"  auth network attempt performed: {'YES' if safe.get('auth_network_attempt_performed') else 'NO'}")
    print(f"  nonce request performed: {'YES' if safe.get('nonce_request_performed') else 'NO'}")
    print(f"  signer invocation performed: {'YES' if safe.get('signer_invocation_performed') else 'NO'}")
    print(f"  signature verification performed: {'YES' if safe.get('signature_verification_performed') else 'NO'}")
    print(f"  signature verification status: {safe.get('signature_verification_status', 'unavailable')}")
    print(f"  recovered signer address: {safe.get('recovered_signer_address', '<missing>')}")
    print(f"  recovered signer address match: {safe.get('recovered_signer_address_match', 'unresolved')}")
    print(f"  signed message integrity: {safe.get('signed_message_integrity', 'unresolved')}")
    print(f"  signer/owner cryptographic match: {safe.get('signer_owner_cryptographic_match', 'unresolved')}")
    print(f"  external signer configured: {'YES' if safe.get('external_signer_configured') else 'NO'}")
    print(f"  external signer process started: {'YES' if safe.get('external_signer_process_started') else 'NO'}")
    print(f"  external signer protocol status: {safe.get('external_signer_protocol_status', 'unavailable')}")
    print(f"  external signer describe performed: {'YES' if safe.get('external_signer_describe_performed') else 'NO'}")
    print(f"  external signer sign performed: {'YES' if safe.get('external_signer_sign_performed') else 'NO'}")
    print(f"  external signer exit status: {safe.get('external_signer_exit_status', 'unavailable')}")
    print(f"  external signer address match: {safe.get('external_signer_address_match', 'unresolved')}")
    print(f"  external signer environment isolated: {safe.get('external_signer_environment_isolated', 'unavailable')}")
    print(f"  external signer message integrity: {safe.get('external_signer_message_integrity', 'unavailable')}")
    print(f"  external signer signature verification: {safe.get('external_signer_signature_verification', 'unavailable')}")
    print(f"  login request performed: {'YES' if safe.get('login_request_performed') else 'NO'}")
    print(f"  token present: {'YES' if safe.get('token_present') else 'NO'}")
    print(f"  expiry status: {safe.get('expiry_status', 'unavailable')}")
    print(f"  refresh required: {'YES' if safe.get('refresh_required') else 'NO'}")
    print(f"  authenticated subject: {safe.get('authenticated_subject', '<unresolved>')}")
    print(f"  identity authoritative: {'YES' if safe.get('identity_authoritative') else 'NO'}")
    print(f"  owner match: {safe.get('owner_match', 'unresolved')}")
    print(f"  trading match: {safe.get('trading_match', 'unresolved')}")
    print(f"  operator match: {safe.get('operator_match', 'unresolved')}")
    print(f"  address semantics: {safe.get('address_semantics', 'unresolved')}")
    reasons = safe.get("unresolved_reasons") or ("none",)
    print(f"  unresolved reasons: {', '.join(reasons) if isinstance(reasons, (tuple, list)) else reasons}")


def _orderbook_timestamp(book) -> datetime | None:
    value = book.get("timestamp", book.get("updatedAt", book.get("updated_at"))) if isinstance(book, dict) else None
    if value is None:
        return None
    try:
        if isinstance(value, (int, float)):
            seconds = float(value) / (1000 if value > 10_000_000_000 else 1)
            return datetime.fromtimestamp(seconds, tz=timezone.utc)
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _print_transaction_signer_boundary() -> None:
    """Print the offline signer policy boundary without constructing a request."""
    policy = DreamDexTransactionSigningPolicy()
    signer = UnavailableDreamDexTransactionSigner()
    preview = build_transaction_signing_preview(policy=policy, signer=signer)
    print("TRANSACTION SIGNER BOUNDARY:")
    print("  signer boundary model: available_offline")
    print("  signing policy validation: available_offline")
    print("  signing request builder: available_offline")
    print("  signer protocol: available_offline")
    print("  signer implementation: unavailable")
    print("  signer address: <missing>")
    print("  signer address match: unresolved")
    print(f"  allowed chain ID: {policy.required_chain_id}")
    print(f"  allowed target: {', '.join(mask_evm_address(v) for v in policy.allowed_target_addresses)}")
    print(f"  allowed operations: {', '.join(policy.allowed_operations)}")
    print(f"  allowed selectors: {', '.join(selector for _, selector in policy.allowed_selectors)}")
    print(f"  native value upper bound: {policy.maximum_native_value_wei if policy.maximum_native_value_wei is not None else 'unavailable'}")
    print(f"  gas limit upper bound: {policy.maximum_gas_limit if policy.maximum_gas_limit is not None else 'unavailable'}")
    print(f"  total fee upper bound: {policy.maximum_total_fee_wei if policy.maximum_total_fee_wei is not None else 'unavailable'}")
    print("  envelope available: NO")
    print("  envelope structurally valid: NO")
    print("  signing policy compliant: NO")
    print("  signer invocation allowed: NO")
    print("  transaction signing capability: unavailable")
    print("  signed transaction serialization: unavailable")
    print("  submission capability: unavailable")
    print("  raw calldata output allowed: NO")
    print("  raw signed transaction output allowed: NO")
    print("  signer boundary authoritative: NO")
    print("  signer boundary blockers: transaction_signer_implementation_unavailable, transaction_signing_request_unavailable, transaction_signer_address_unresolved, transaction_fee_limit_unresolved, transaction_value_limit_unresolved")


def _strict_int_setting(environ: Mapping[str, str], name: str, *, minimum: int = 0, required: bool = False) -> int | None:
    raw = environ.get(name)
    if raw is None or str(raw).strip() == "":
        if required:
            raise ValueError(f"{name} is required")
        return None
    text = str(raw).strip()
    if not re.fullmatch(r"[0-9]+", text):
        raise ValueError(f"{name} must be a strict integer")
    value = int(text)
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return value


def build_execution_journal_policy_from_env(environ: Mapping[str, str]) -> DreamDexExecutionJournalPolicy:
    """Build journal policy from CLI settings without opening or creating a file."""
    busy = _strict_int_setting(environ, "DREAMDEX_EXECUTION_JOURNAL_BUSY_TIMEOUT_MS", minimum=1) or 2500
    max_intents = _strict_int_setting(environ, "DREAMDEX_EXECUTION_JOURNAL_MAX_ACTIVE_INTENTS", minimum=1)
    max_reservations = _strict_int_setting(environ, "DREAMDEX_EXECUTION_JOURNAL_MAX_ACTIVE_RESERVATIONS", minimum=1)
    # Explicit limits are required for an enabled production journal.  The
    # offline default remains bounded so fixture callers can use the policy.
    unresolved = ()
    if _parse_enable_flag(environ.get(JOURNAL_ENABLE_ENV)) == "enabled" and (max_intents is None or max_reservations is None):
        unresolved = ("execution_journal_limits_unresolved",)
    return DreamDexExecutionJournalPolicy(
        busy_timeout_ms=busy,
        maximum_active_intents=max_intents or 10000,
        maximum_active_reservations=max_reservations or 10000,
        unresolved_reasons=unresolved,
        authoritative=False,
    )


def inspect_execution_journal_from_env(environ: Mapping[str, str]):
    """Read an existing journal only when an explicit diagnostic flag is set."""
    flag = _parse_enable_flag(environ.get(JOURNAL_DIAGNOSTICS_ENV))
    if flag == "invalid":
        raise ValueError(f"{JOURNAL_DIAGNOSTICS_ENV} must be a strict boolean")
    if flag != "enabled":
        return None
    raw_path = environ.get(JOURNAL_PATH_ENV)
    if not raw_path:
        return None
    policy = build_execution_journal_policy_from_env(environ)
    # Read-only diagnostic open never creates a database and never mutates it.
    return open_journal(raw_path, policy, mode="ro")


def _print_durable_execution_journal(snapshot=None, *, enabled: bool = False, execution_performed: bool = False, path_configured: bool = False) -> None:
    print("DURABLE EXECUTION JOURNAL:")
    print(f"  journal enabled: {'YES' if enabled else 'NO'}")
    print(f"  journal execution performed: {'YES' if execution_performed else 'NO'}")
    print(f"  journal path configured: {'YES' if path_configured else 'NO'}")
    print("  journal path output allowed: NO")
    print("  storage engine: sqlite3")
    print(f"  schema version: {snapshot.schema_version if snapshot and snapshot.schema_version is not None else 'unavailable'}")
    print(f"  schema compatible: {snapshot.schema_status if snapshot else 'unresolved'}")
    print(f"  integrity status: {snapshot.integrity_status if snapshot else 'unavailable'}")
    print(f"  WAL enabled: {'YES' if enabled and execution_performed else 'NO'}")
    print("  synchronous mode: FULL" if enabled and execution_performed else "  synchronous mode: unavailable")
    print("  writer locking: BEGIN IMMEDIATE" if enabled and execution_performed else "  writer locking: unavailable")
    for label, value in (
        ("intent count", snapshot.intent_count if snapshot else 0),
        ("active intent count", snapshot.active_intent_count if snapshot else 0),
        ("nonce reservation count", snapshot.reservation_count if snapshot else 0),
        ("active nonce reservation count", snapshot.active_reservation_count if snapshot else 0),
        ("unknown state count", snapshot.unknown_state_count if snapshot else 0),
        ("conflicted intent count", snapshot.conflicted_intent_count if snapshot else 0),
    ):
        print(f"  {label}: {value}")
    print(f"  recovery required: {'YES' if snapshot and snapshot.recovery_required else 'NO'}")
    print(f"  safe to create intent: {'YES' if snapshot and snapshot.safe_to_create_intent else 'NO'}")
    print(f"  safe to reserve nonce: {'YES' if snapshot and snapshot.safe_to_reserve_nonce else 'NO'}")
    print("  local nonce reserved: NO")
    print("  network nonce fresh: NO")
    print("  nonce externally exclusive: NO")
    print("  nonce revalidation required: YES")
    print(f"  journal authoritative: {'YES' if snapshot and snapshot.schema_status == 'compatible' and False else 'NO'}")
    print("  signing allowed: NO")
    print("  submission allowed: NO")
    print("  raw transaction output allowed: NO")
    blockers = ", ".join(snapshot.blockers) if snapshot and snapshot.blockers else ("execution_journal_unavailable" if not execution_performed else "none")
    print(f"  journal blockers: {blockers}")


def build_live_nonce_revalidation_policy_from_env(environ: Mapping[str, str]) -> DreamDexLiveNonceRevalidationPolicy:
    max_age = _strict_int_setting(environ, LIVE_NONCE_MAX_AGE_ENV, minimum=0)
    configured_chain = _strict_int_setting(environ, "DREAMDEX_READ_ONLY_REQUIRED_CHAIN_ID", minimum=0)
    unresolved = ("signing_lease_policy_unresolved",) if max_age is None or not environ.get("DREAMDEX_READ_ONLY_OWNER_ADDRESS") else ()
    return DreamDexLiveNonceRevalidationPolicy(
        required_chain_id=5031 if configured_chain is None else configured_chain,
        required_signer_address=environ.get("DREAMDEX_READ_ONLY_OWNER_ADDRESS"),
        maximum_observation_age_ms=max_age,
        unresolved_reasons=unresolved,
    )


def execute_live_signing_lease(*, journal, intent, reservation, finalized_envelope, signing_request, signing_policy, environ: Mapping[str, str], rpc_transport=None):
    """Explicit opt-in orchestration; no typed inputs means no RPC call."""
    flags = (_parse_enable_flag(environ.get(LIVE_NONCE_REVALIDATION_ENV)), _parse_enable_flag(environ.get(SIGNING_LEASE_ENABLE_ENV)))
    if "invalid" in flags:
        raise ValueError("live nonce/signing lease flags must be strict booleans")
    if flags != ("enabled", "enabled"):
        return None
    if any(value is None for value in (journal, intent, reservation, finalized_envelope, signing_request, signing_policy)):
        return None
    policy = build_live_nonce_revalidation_policy_from_env(environ)
    if rpc_transport is None:
        rpc_url = environ.get(PREFLIGHT_RPC_ENV) or environ.get("DREAMDEX_RPC_URL")
        if not rpc_url:
            return None
        rpc_transport = DreamDexReadOnlyRpcTransport(rpc_url)
    return acquire_signing_lease(journal=journal, intent=intent, reservation=reservation, finalized_envelope=finalized_envelope, signing_request=signing_request, signing_policy=signing_policy, policy=policy, rpc=rpc_transport)


def _print_live_nonce_signing_lease(result=None, *, enabled: bool = False, execution_performed: bool = False, rpc_configured: bool = False, policy: DreamDexLiveNonceRevalidationPolicy | None = None) -> None:
    preview = build_signing_lease_preview(result, evidence=result.evidence if result else None)
    print("LIVE NONCE REVALIDATION & SIGNING LEASE:")
    print(f"  live nonce revalidation enabled: {'YES' if enabled else 'NO'}")
    print(f"  signing lease enabled: {'YES' if enabled else 'NO'}")
    print(f"  lease execution performed: {'YES' if execution_performed else 'NO'}")
    print(f"  network calls allowed: {'YES' if enabled and rpc_configured else 'NO'}")
    print(f"  pending tag required: YES")
    print(f"  maximum observation age: {policy.maximum_observation_age_ms if policy and policy.maximum_observation_age_ms is not None else 'unavailable'}")
    print(f"  lease status: {preview.lease_status}")
    print(f"  chain match: {'confirmed' if preview.chain_match is True else 'mismatch' if preview.chain_match is False else 'unresolved'}")
    print(f"  pending nonce status: {preview.pending_nonce_status}")
    print("  pending nonce snapshot only: YES")
    print(f"  local nonce reserved: {'YES' if preview.local_nonce_reserved else 'NO'}")
    print(f"  nonce match: {'YES' if preview.nonce_match else 'NO'}")
    print(f"  nonce observation fresh: {'YES' if preview.nonce_observation_fresh else 'NO' if preview.nonce_observation_fresh is False else 'unresolved'}")
    print(f"  journal integrity status: {preview.journal_integrity_status}")
    print(f"  recovery required: {'YES' if preview.recovery_required else 'NO'}")
    print(f"  active signing lease count: {preview.active_signing_lease_count}")
    print(f"  signing lease acquired: {'YES' if preview.signing_lease_acquired else 'NO'}")
    print("  signer invocation performed: NO")
    print("  signer invocation allowed: NO")
    print(f"  transaction signing capability: {preview.transaction_signing_capability}")
    print("  transaction submission allowed: NO")
    print(f"  lease fingerprint: {mask_hex_hash(preview.lease_fingerprint)}")
    print(f"  lease blockers: {', '.join(preview.blockers) or 'none'}")


def _print_signed_transaction_verification(*, session_result=None, execution_performed: bool = False) -> None:
    capabilities = build_execution_capability_matrix()
    capability = lambda name: capabilities.by_name(name).status
    preview = build_signed_transaction_preview(session_result)
    print("SIGNED TRANSACTION VERIFICATION:")
    print(f"  signing material model: {capability('signing_material_model')}")
    print(f"  bound signer protocol: {capability('bound_transaction_signer_protocol')}")
    print(f"  production signer: {capability('production_bound_signer')}")
    print(f"  signing session execution performed: {'YES' if execution_performed else 'NO'}")
    print(f"  signer invocation performed: {'YES' if preview.signer_invocation_performed else 'NO'}")
    print(f"  ephemeral signed payload received: {'YES' if preview.signed_payload_received else 'NO'}")
    print(f"  signed payload persisted: {'YES' if preview.signed_payload_persisted else 'NO'}")
    print(f"  signed transaction decoder: {capability('decode_signed_transaction')}")
    print(f"  sender recovery: {capability('recover_signed_transaction_sender')}")
    print(f"  independent field verification: {capability('verify_signed_transaction_fields')}")
    print(f"  transaction hash calculation: {capability('calculate_signed_transaction_hash')}")
    print(f"  journal signing-started transition: {capability('journal_signing_started_transition')}")
    print(f"  journal signed transition: {capability('journal_signed_transition')}")
    print(f"  signed artifact available: {'YES' if preview.signed_artifact_available else 'NO'}")
    print("  raw signed transaction output allowed: NO")
    print("  raw signature output allowed: NO")
    print("  ready for submission: NO")
    print(f"  submission capability: {capability('submit_transaction')}")
    print(f"  verification blockers: {', '.join(preview.blockers) or 'none'}")


def _print_transaction_submission_boundary(*, result=None, execution_performed: bool = False, recovery=None) -> None:
    capabilities = build_execution_capability_matrix()
    preview = build_transaction_submission_preview(result, production_submitter_status=capabilities.by_name("production_raw_transaction_submitter").status, recovery_lookup_available=capabilities.by_name("recover_submission_by_hash").status != "unavailable", recovery=recovery)
    print("RAW TRANSACTION SUBMISSION BOUNDARY:")
    print(f"  submission boundary model: {capabilities.by_name('raw_transaction_submission_model').status}")
    print(f"  typed submitter protocol: {capabilities.by_name('raw_transaction_submitter_protocol').status}")
    print(f"  production submitter: {preview.production_submitter_status}")
    print(f"  submission execution performed: {'YES' if execution_performed else 'NO'}")
    print(f"  raw payload available in memory: {'YES' if preview.raw_payload_available_in_memory else 'NO'}")
    print(f"  raw payload persisted: {'YES' if preview.raw_payload_persisted else 'NO'}")
    print(f"  raw payload reference released: {'YES' if preview.raw_payload_reference_released else 'NO'}")
    print("  secure memory zeroization guaranteed: NO")
    print(f"  local transaction hash available: {'YES' if preview.local_transaction_hash_available else 'NO'}")
    print(f"  durable submission record: {'YES' if preview.submission_record_persisted else 'NO'}")
    print(f"  send attempt started: {'YES' if preview.send_attempt_started else 'NO'}")
    print(f"  send attempt count: {preview.send_attempt_count}")
    print(f"  RPC response received: {'YES' if preview.rpc_response_received else 'NO'}")
    print(f"  RPC hash match: {'confirmed' if preview.rpc_hash_match is True else 'mismatch' if preview.rpc_hash_match is False else 'unresolved'}")
    print(f"  journal submission state: {preview.journal_state}")
    print(f"  submitted: {'YES' if preview.submitted else 'NO'}")
    print(f"  submission unknown: {'YES' if preview.submission_unknown else 'NO'}")
    print("  automatic retry allowed: NO")
    print("  replacement allowed: NO")
    print("  recovery lookup protocol: available_offline")
    print(f"  recovery lookup performed: {'YES' if preview.recovery_lookup_performed else 'NO'}")
    print(f"  transaction found by hash: {'YES' if preview.transaction_found_by_hash else 'NO' if preview.transaction_found_by_hash is False else 'unresolved'}")
    print("  ready for receipt lookup: NO")
    print("  ready for resubmission: NO")
    print("  raw transaction output allowed: NO")
    print(f"  submission blockers: {', '.join(preview.blockers) or 'none'}")


def build_live_transaction_preflight_policy(environ: Mapping[str, str]) -> DreamDexTransactionPreflightPolicy:
    """Read CLI-only settings; the production preflight module reads no env."""
    values = {
        "maximum_gas_limit": _strict_int_setting(environ, "DREAMDEX_TX_MAX_GAS_LIMIT", minimum=1),
        "maximum_total_fee_wei": _strict_int_setting(environ, "DREAMDEX_TX_MAX_TOTAL_FEE_WEI", minimum=1),
        "gas_headroom_bps": _strict_int_setting(environ, "DREAMDEX_TX_GAS_HEADROOM_BPS", minimum=10000),
        "legacy_gas_multiplier_bps": _strict_int_setting(environ, "DREAMDEX_TX_LEGACY_GAS_MULTIPLIER_BPS", minimum=1),
        "base_fee_multiplier_bps": _strict_int_setting(environ, "DREAMDEX_TX_BASE_FEE_MULTIPLIER_BPS", minimum=1),
        "maximum_priority_fee_per_gas_wei": _strict_int_setting(environ, "DREAMDEX_TX_MAX_PRIORITY_FEE_PER_GAS_WEI", minimum=1),
    }
    unresolved = ("policy_limits_unresolved",) if any(value is None for value in values.values()) else ()
    return DreamDexTransactionPreflightPolicy(
        required_sender_address=environ.get("DREAMDEX_READ_ONLY_OWNER_ADDRESS"),
        required_target_address=environ.get("DREAMDEX_READ_ONLY_POOL_ADDRESS") or "0x035de7403eac6872787779cca7ccf1b4cdb61379",
        unresolved_reasons=unresolved,
        **values,
    )


build_preflight_policy_from_env = build_live_transaction_preflight_policy


def execute_live_transaction_preflight(envelope, environ: Mapping[str, str], *, rpc_transport=None):
    """Opt-in orchestration; no request is made unless a typed envelope exists."""
    flag = _parse_enable_flag(environ.get(LIVE_PREFLIGHT_ENV))
    if flag == "invalid":
        raise ValueError(f"{LIVE_PREFLIGHT_ENV} must be a strict boolean")
    policy = build_live_transaction_preflight_policy(environ)
    if flag != "enabled":
        return unavailable_preflight_result(policy=policy, reason="live_transaction_preflight_unavailable")
    if envelope is None:
        return unavailable_preflight_result(policy=policy, reason="transaction_preflight_envelope_unavailable")
    rpc_url = environ.get(PREFLIGHT_RPC_ENV) or environ.get("DREAMDEX_RPC_URL")
    if not rpc_url:
        return unavailable_preflight_result(envelope, policy, reason="rpc_configuration_unavailable")
    transport = rpc_transport or DreamDexReadOnlyRpcTransport(rpc_url)
    return run_transaction_preflight(envelope, transport, policy)


def _print_live_transaction_preflight(result=None, *, enabled: bool = False, execution_performed: bool = False, rpc_configured: bool = False, policy: DreamDexTransactionPreflightPolicy | None = None) -> None:
    policy = policy or DreamDexTransactionPreflightPolicy()
    preview = build_transaction_preflight_preview(result)
    evidence = result.evidence if result is not None else None
    params = result.resolved_parameters if result is not None else None
    print("LIVE TRANSACTION PREFLIGHT:")
    print(f"  preflight enabled: {'YES' if enabled else 'NO'}")
    print(f"  preflight execution performed: {'YES' if execution_performed else 'NO'}")
    print(f"  network calls allowed: {'YES' if enabled and rpc_configured else 'NO'}")
    print(f"  rpc configuration: {'configured' if rpc_configured else 'unavailable'}")
    print("  rpc URL output allowed: NO")
    print(f"  chain evidence: {'available' if evidence and evidence.chain_id is not None else 'unavailable'}")
    print(f"  chain ID: {preview.chain_id if preview.chain_id is not None else 'unavailable'}")
    print(f"  chain match: {'confirmed' if preview.chain_match is True else 'mismatch' if preview.chain_match is False else 'unresolved'}")
    print(f"  target contract code: {preview.target_code_status}")
    print(f"  target code byte length: {evidence.target_code_byte_length if evidence and evidence.target_code_byte_length is not None else 0}")
    print(f"  envelope available: {'YES' if result and result.original_envelope_fingerprint else 'NO'}")
    print(f"  envelope structurally valid: {'YES' if result and result.evidence.source_status != 'unavailable' and not result.validation_errors else 'NO'}")
    print(f"  pending nonce evidence: {'available' if evidence and evidence.pending_nonce is not None else 'unavailable'}")
    print("  pending nonce snapshot only: YES")
    print("  nonce reserved: NO")
    print(f"  gas estimate evidence: {preview.gas_estimate_status}")
    print(f"  gas estimate: {params.gas_estimate if params and params.gas_estimate is not None else 'unavailable'}")
    print(f"  gas headroom: {policy.gas_headroom_bps if policy.gas_headroom_bps is not None else 'unavailable'}")
    print(f"  resolved gas limit: {preview.gas_limit if preview.gas_limit is not None else 'unavailable'}")
    print(f"  fee model: {preview.fee_mode}")
    print(f"  fee evidence: {'available' if evidence and evidence.fee_evidence.source_status != 'unavailable' else 'unavailable'}")
    print(f"  maximum possible fee: {preview.maximum_possible_fee_wei if preview.maximum_possible_fee_wei is not None else 'unavailable'}")
    print(f"  total fee policy limit: {policy.maximum_total_fee_wei if policy.maximum_total_fee_wei is not None else 'unavailable'}")
    print(f"  native balance evidence: {preview.native_balance_status}")
    print(f"  native balance sufficient: {'YES' if preview.native_balance_sufficient is True else 'NO' if preview.native_balance_sufficient is False else 'unresolved'}")
    print(f"  finalized envelope available: {'YES' if result and result.finalized_envelope is not None else 'NO'}")
    print(f"  finalized envelope fingerprint: {mask_hex_hash(preview.finalized_envelope_fingerprint)}")
    print(f"  preflight authoritative: {'YES' if result and result.authoritative else 'NO'}")
    print(f"  preflight policy compliant: {'YES' if preview.policy_compliant else 'NO'}")
    print(f"  ready for signing policy review: {'YES' if preview.ready_for_signing_policy_review else 'NO'}")
    print("  signer invocation allowed: NO")
    print("  transaction signing capability: unavailable")
    print("  transaction submission allowed: NO")
    print("  raw calldata output allowed: NO")
    print("  raw RPC payload output allowed: NO")
    print(f"  preflight blockers: {', '.join(preview.blockers) or 'none'}")


def _print_report(snapshot, report, validation, *, reconciliation_bridge_enabled: bool = False, preflight_result=None, preflight_enabled: bool = False, preflight_execution_performed: bool = False, preflight_rpc_configured: bool = False, preflight_policy: DreamDexTransactionPreflightPolicy | None = None, journal_snapshot=None, journal_enabled: bool = False, journal_execution_performed: bool = False, journal_path_configured: bool = False, lease_result=None, lease_enabled: bool = False, lease_execution_performed: bool = False, lease_policy: DreamDexLiveNonceRevalidationPolicy | None = None, signing_session_result=None, signing_session_execution_performed: bool = False) -> None:
    market, account = snapshot.market, snapshot.account
    base, quote = market.base_asset or "SOMI", market.quote_asset or "USDso"
    print("READ-ONLY ACCOUNT CHECK")
    print(f"Owner/login address: {_masked(account.owner_address or account.account_identifier)}")
    print(f"Trading address: {_masked(account.trading_address)}")
    print(f"Trading address status: {account.trading_address_status}")
    print(f"Market: {market.symbol}")
    print(f"Pool address: {market.pool_address or '<unavailable>'}")
    print(f"Base token address: {market.base_token_address or '<unavailable>'}")
    print(f"Quote token address: {market.quote_token_address or '<unavailable>'}")
    print(f"Market metadata status: {getattr(getattr(market, 'trading_rules', None), 'source_status', 'unavailable')}")
    _print_market_trading_rules(market)
    _print_address_diagnostics("OWNER/LOGIN", snapshot.owner_diagnostics, account.owner_address or account.account_identifier)
    _print_address_diagnostics("TRADING/SMART", snapshot.trading_diagnostics, account.trading_address)
    _print_identity_binding_evidence(account.identity_binding_evidence)
    _print_operator_session_model(account, market)
    direct_owner_reasons = _print_direct_owner_execution_model(account, market)
    graph_blockers = _print_order_reconciliation_graph()
    _print_execution_pipeline_summary()
    _print_transaction_signer_boundary()
    _print_live_transaction_preflight(preflight_result, enabled=preflight_enabled, execution_performed=preflight_execution_performed, rpc_configured=preflight_rpc_configured, policy=preflight_policy)
    _print_durable_execution_journal(journal_snapshot, enabled=journal_enabled, execution_performed=journal_execution_performed, path_configured=journal_path_configured)
    _print_live_nonce_signing_lease(lease_result, enabled=lease_enabled, execution_performed=lease_execution_performed, rpc_configured=bool(os.environ.get(PREFLIGHT_RPC_ENV) or os.environ.get("DREAMDEX_RPC_URL")), policy=lease_policy)
    _print_signed_transaction_verification(session_result=signing_session_result, execution_performed=signing_session_execution_performed)
    _print_transaction_submission_boundary(result=None, execution_performed=False)
    _print_reconciliation_evidence_bridge(snapshot, enabled=reconciliation_bridge_enabled)
    book = snapshot.orderbook if isinstance(snapshot.orderbook, dict) else {}
    bids, asks = book.get("bids", []), book.get("asks", [])
    best_bid = bids[0].get("price", bids[0]) if bids and isinstance(bids[0], dict) else (bids[0] if bids else "<unavailable>")
    best_ask = asks[0].get("price", asks[0]) if asks and isinstance(asks[0], dict) else (asks[0] if asks else "<unavailable>")
    print(f"Best bid: {best_bid}")
    print(f"Best ask: {best_ask}")
    timestamp = _orderbook_timestamp(book)
    age = None if timestamp is None else max(Decimal("0"), Decimal(str((datetime.now(timezone.utc) - timestamp).total_seconds())))
    max_age = _decimal_env("DREAMDEX_READ_ONLY_MAX_MARKET_AGE_SECONDS", Decimal("30"))
    print(f"Orderbook timestamp: {timestamp.isoformat() if timestamp else '<unavailable>'}")
    print(f"Orderbook age: {age if age is not None else '<unavailable>'} seconds")
    freshness = "fresh" if account.orderbook_status == "available" and age is not None and age <= max_age else ("stale" if account.orderbook_status == "stale" else "unavailable")
    print(f"Orderbook source status: {'available' if bids and asks else 'unavailable'}")
    print(f"Orderbook freshness: {freshness}")
    print("Wallet token balances:")
    wallet_address = _masked(account.trading_address)
    for asset in (base, quote):
        balance = account.balance(asset)
        print(f"  {asset} (address={wallet_address}): total={balance.total} available={balance.available} status={balance.status}")
    print("Vault balances REST:")
    print(f"  address semantics: {account.vault_address_semantics} ({_masked(account.trading_address)})")
    print(f"  {base}: {_source(account.vault_rest.base)}")
    print(f"  {quote}: {_source(account.vault_rest.quote)}")
    print("Vault balances RPC getWithdrawableBalance:")
    print(f"  address semantics: {account.vault_address_semantics} ({_masked(account.trading_address)})")
    print(f"  {base}: {_source(account.vault_rpc.base_vault)}")
    print(f"  {quote}: {_source(account.vault_rpc.quote_vault)}")
    print(f"Native gas balance (eth_getBalance, owner/login address={_masked(account.owner_address)}; normalized 18 decimals): {_source(account.vault_rpc.native_gas)}")
    print(f"Open-orders source status: {account.open_orders_status}")
    print(f"Fills source status: {account.fills_status}")
    authenticated = account.authenticated
    auth_unconfigured = authenticated.balances_status.error_code == "authenticated_transport_unconfigured"
    confirmed_account_observations = authenticated.balances_status.available and authenticated.open_orders_status.available
    account_evidence = "observed_non_authoritative" if confirmed_account_observations and not authenticated.authoritative_for(account.trading_address) else ("unconfigured" if auth_unconfigured else ("available" if authenticated.available else "unavailable"))
    print(f"Authenticated account source: {account_evidence}")
    print(f"Authenticated account evidence: {account_evidence}")
    print(f"Authenticated transport: {account.authenticated_transport_status}")
    print(f"Authenticated transport configured: {'YES' if account.authenticated_transport_status == 'configured' else 'NO'}")
    print(f"Authenticated request execution: {'enabled' if account.authenticated_request_execution_enabled else 'disabled'}")
    print(f"Authenticated request execution enabled: {'YES' if account.authenticated_request_execution_enabled else 'NO'}")
    print(f"Authenticated vault REST status: {authenticated.balances_status.status}")
    print(f"Authenticated order list status: {authenticated.open_orders_status.schema_status or authenticated.open_orders_status.status}")
    print(f"Authenticated order-by-id status: {account.authenticated_order_by_id_status}")
    print(f"Authenticated schema fingerprint status: {account.authenticated_schema_fingerprint_status}")
    identity_verified = bool(
        account.identity_binding_evidence is not None
        and account.identity_binding_evidence.authoritative
        and authenticated.authoritative_for(account.trading_address)
    )
    print(f"Authenticated identity verified: {'YES' if identity_verified else 'NO'}")
    _print_schema_fingerprint("vault", account.authenticated_vault_fingerprint)
    _print_schema_fingerprint("order list", account.authenticated_order_list_fingerprint)
    _print_schema_fingerprint("order-by-id", account.authenticated_order_by_id_fingerprint)
    print(f"Authenticated balances: {authenticated.balances_status.status}")
    # Keep the historical summary wording while exposing the more precise
    # configured/unconfigured status above.
    legacy_open_orders_status = authenticated.open_orders_status.status
    if authenticated.open_orders_status.error_code in {"authenticated_transport_unconfigured", "authenticated_token_missing"}:
        legacy_open_orders_status = "unavailable"
    print(f"Authenticated open orders: {legacy_open_orders_status}")
    print(f"Authenticated open-order record count: {len(authenticated.open_orders)}")
    print(f"Authenticated fills: {authenticated.fills_status.status}")
    print(f"Authenticated pagination complete: {'YES' if authenticated.pagination_complete else 'NO'}")
    _print_authentication_state(account)
    onchain = account.onchain_fills
    onchain_status = onchain.source_status
    print(f"On-chain fills source: {onchain_status.status if onchain_status.status != 'unavailable' else 'unavailable'}")
    print(f"On-chain latest block: {onchain_status.latest_block if onchain_status.latest_block is not None else 'unavailable'}")
    print(f"On-chain confirmed through block: {onchain_status.confirmed_through_block if onchain_status.confirmed_through_block is not None else 'unavailable'}")
    print(f"On-chain decoded fills: {onchain_status.decoded_fill_count}")
    print(f"On-chain duplicate count: {onchain_status.duplicate_count}")
    print(f"On-chain pagination complete: {'YES' if onchain_status.pagination_complete else 'NO'}")
    print(f"On-chain reorg status: {onchain_status.reorg_status}")
    print(f"On-chain account match status: {onchain_status.account_match_status}")
    print(f"On-chain fills authoritative: {'YES' if onchain_status.authoritative and onchain_status.account_match_status == 'matched' else 'NO'}")
    metadata = account.order_metadata_report
    metadata_source = "unconfigured" if metadata.reason == "authenticated_transport_unconfigured" else ("available" if metadata.resolved_count else "unavailable")
    print(f"Order metadata source: {metadata_source}")
    print(f"Order metadata records resolved: {metadata.resolved_count}")
    print(f"Order metadata conflicts: {metadata.conflict_count}")
    print(f"Fill/order correlation status: {metadata.status}")
    print(f"Account-correlated fills authoritative: {'YES' if metadata.authoritative else 'NO'}")
    print(f"Reconciliation complete: {'YES' if report.completed else 'NO'}")
    print(f"Account address semantics: {account.account_address_semantics}")
    print(f"Hypothetical trading blocked: {'YES' if report.trading_blocked else 'NO'}")
    blocked_reason = report.reason if report.trading_blocked else ", ".join(validation.reasons) or "none"
    if report.trading_blocked:
        blocked_reason = ";".join(dict.fromkeys([item for item in [blocked_reason, *direct_owner_reasons, *graph_blockers, "order_id_lifecycle_unconfirmed", "direct_order_reconciliation_unavailable"] if item]))
    else:
        blocked_reason = ";".join(dict.fromkeys([blocked_reason, *direct_owner_reasons, *graph_blockers]))
    print(f"Hypothetical trading blocked reason: {blocked_reason}")
    print(f"Dry-run approved: {'YES' if validation.approved else 'NO'}")
    print(f"Dry-run reasons: {', '.join(validation.reasons) or 'none'}")
    print("Real submission enabled: NO")


def main() -> int:
    try:
        raw_transaction_submission_flag(os.environ)
    except ValueError as exc:
        print("READ-ONLY ACCOUNT CHECK")
        print(f"Configuration rejected: {exc}")
        print("No network request or order operation was attempted.")
        _print_transaction_submission_boundary(result=None, execution_performed=False)
        print("Real submission enabled: NO")
        return 2
    fixture_path = os.environ.get("DREAMDEX_READ_ONLY_FIXTURE")
    required = ["DREAMDEX_READ_ONLY_OWNER_ADDRESS"]
    if not fixture_path:
        required.extend(("DREAMDEX_READ_ONLY_BASE_URL", "DREAMDEX_READ_ONLY_RPC_URL"))
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        print("READ-ONLY ACCOUNT CHECK")
        print("Missing configuration: " + ", ".join(missing))
        print("No network request or order operation was attempted.")
        _print_live_transaction_preflight(None, enabled=False, execution_performed=False, rpc_configured=False)
        _print_durable_execution_journal(None, enabled=False, execution_performed=False, path_configured=False)
        _print_live_nonce_signing_lease(None, enabled=False, execution_performed=False, rpc_configured=False)
        _print_signed_transaction_verification(session_result=None, execution_performed=False)
        _print_transaction_submission_boundary(result=None, execution_performed=False)
        print("Real submission enabled: NO")
        return 2
    owner = os.environ["DREAMDEX_READ_ONLY_OWNER_ADDRESS"]
    try:
        bridge_flag = _parse_enable_flag(os.environ.get(RECONCILIATION_BRIDGE_ENV))
        if bridge_flag == "invalid":
            raise ValueError(f"{RECONCILIATION_BRIDGE_ENV} must be a strict boolean")
        reconciliation_bridge_enabled = bridge_flag == "enabled"
        preflight_flag = _parse_enable_flag(os.environ.get(LIVE_PREFLIGHT_ENV))
        if preflight_flag == "invalid":
            raise ValueError(f"{LIVE_PREFLIGHT_ENV} must be a strict boolean")
        preflight_enabled = preflight_flag == "enabled"
        preflight_policy = build_live_transaction_preflight_policy(os.environ)
        journal_flag = _parse_enable_flag(os.environ.get(JOURNAL_ENABLE_ENV))
        if journal_flag == "invalid":
            raise ValueError(f"{JOURNAL_ENABLE_ENV} must be a strict boolean")
        journal_enabled = journal_flag == "enabled"
        journal_path_configured = bool(os.environ.get(JOURNAL_PATH_ENV))
        journal_snapshot = None
        journal_execution_performed = False
        journal_handle = inspect_execution_journal_from_env(os.environ)
        if journal_handle is not None:
            try:
                journal_snapshot = journal_handle.build_execution_journal_snapshot()
                journal_execution_performed = True
            finally:
                journal_handle.close()
        nonce_flag = _parse_enable_flag(os.environ.get(LIVE_NONCE_REVALIDATION_ENV))
        lease_flag = _parse_enable_flag(os.environ.get(SIGNING_LEASE_ENABLE_ENV))
        if "invalid" in (nonce_flag, lease_flag):
            raise ValueError("live nonce/signing lease flags must be strict booleans")
        lease_enabled = nonce_flag == "enabled" and lease_flag == "enabled"
        lease_policy = build_live_nonce_revalidation_policy_from_env(os.environ)
        lease_result = None
        lease_execution_performed = False
        symbol = os.environ.get("DREAMDEX_READ_ONLY_MARKET", "SOMI:USDso")
        if fixture_path:
            fixture = load_fixture(fixture_path)
            rest_transport, rpc_transport = FixtureTransport(fixture), FixtureRpcTransport(fixture)
        else:
            from bot.integrations.dreamdex_read_only import HttpGetTransport, HttpRpcTransport
            rest_transport, rpc_transport = HttpGetTransport(os.environ["DREAMDEX_READ_ONLY_BASE_URL"]), HttpRpcTransport(os.environ["DREAMDEX_READ_ONLY_RPC_URL"])
        trading_address = os.environ.get("DREAMDEX_READ_ONLY_TRADING_ADDRESS")
        # The factory reads only the explicit enable flag and bearer-token
        # variable. Construction is side-effect free; GET I/O remains gated.
        authenticated_transport = build_authenticated_read_only_transport_from_env(os.environ)
        siwe_transport = build_siwe_http_transport_from_env(os.environ)
        # The production signer factory is deliberately unavailable and never
        # reads secrets. This keeps the read-only check from attempting SIWE.
        signer = build_production_siwe_signer_from_env(os.environ)
        auth_manager = DreamDexAuthManager(transport=siwe_transport, signer=signer, owner_address=owner)
        adapter = DreamDexReadOnlyAdapter(
            transport=rest_transport,
            rpc_transport=rpc_transport,
            owner=owner,
            trading_address=trading_address,
            symbol=symbol,
            authenticated_transport=authenticated_transport,
            auth_manager=auth_manager,
            owner_platform_role=os.environ.get("DREAMDEX_READ_ONLY_OWNER_PLATFORM_ROLE"),
            trading_platform_role=os.environ.get("DREAMDEX_READ_ONLY_TRADING_PLATFORM_ROLE"),
        )
        snapshot = adapter.fetch_snapshot()
        local_cash = os.environ.get("DREAMDEX_READ_ONLY_LOCAL_CASH")
        local_inventory = os.environ.get("DREAMDEX_READ_ONLY_LOCAL_INVENTORY")
        report = adapter.reconcile(snapshot, local_cash=None if local_cash is None else Decimal(local_cash), local_inventory=None if local_inventory is None else Decimal(local_inventory))
        intent = OrderIntent(symbol, os.environ.get("DREAMDEX_READ_ONLY_DRY_RUN_SIDE", "buy"), "limit", _decimal_env("DREAMDEX_READ_ONLY_DRY_RUN_PRICE", Decimal("0")), _decimal_env("DREAMDEX_READ_ONLY_DRY_RUN_QUANTITY", Decimal("0")))
        validation = DryRunOrderValidator(DryRunValidationLimits(_decimal_env("DREAMDEX_READ_ONLY_MAX_NOTIONAL", Decimal("100000")), _decimal_env("DREAMDEX_READ_ONLY_MAX_INVENTORY", Decimal("100000")))).validate(intent, market=snapshot.market, account=snapshot.account, reconciliation=report, market_fresh=snapshot.market.is_fresh(now=datetime.now(timezone.utc), max_age_seconds=_decimal_env("DREAMDEX_READ_ONLY_MAX_MARKET_AGE_SECONDS", Decimal("30"))))
        # No production request/envelope is constructed by this read-only CLI.
        # Consequently enabling the flag without an explicit typed envelope is
        # a safe, observable no-op and performs zero preflight RPC calls.
        preflight_result = execute_live_transaction_preflight(None, os.environ)
        _print_report(snapshot, report, validation, reconciliation_bridge_enabled=reconciliation_bridge_enabled, preflight_result=preflight_result, preflight_enabled=preflight_enabled, preflight_execution_performed=False, preflight_rpc_configured=bool(os.environ.get(PREFLIGHT_RPC_ENV) or os.environ.get("DREAMDEX_RPC_URL")), preflight_policy=preflight_policy, journal_snapshot=journal_snapshot, journal_enabled=journal_enabled, journal_execution_performed=journal_execution_performed, journal_path_configured=journal_path_configured, lease_result=lease_result, lease_enabled=lease_enabled, lease_execution_performed=lease_execution_performed, lease_policy=lease_policy, signing_session_result=None, signing_session_execution_performed=False)
        return 0
    except Exception as exc:
        print("READ-ONLY ACCOUNT CHECK")
        print(f"Read-only check failed: {_safe_error(exc, owner)}")
        print("No order submission was attempted.")
        print("Real submission enabled: NO")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
