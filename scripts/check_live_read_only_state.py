"""Safe read-only DreamDEX state check (no order or transaction operations)."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import os
import re

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


def _decimal_env(name: str, default: Decimal) -> Decimal:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return Decimal(raw)
    except (InvalidOperation, ValueError):
        raise ValueError(f"{name} must be a decimal")


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
    return direct_owner_blocking_reasons(audit, binding=binding)


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


def _print_report(snapshot, report, validation) -> None:
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
        blocked_reason = ";".join(dict.fromkeys([item for item in [blocked_reason, *direct_owner_reasons, "order_id_lifecycle_unconfirmed", "direct_order_reconciliation_unavailable"] if item]))
    else:
        blocked_reason = ";".join(dict.fromkeys([blocked_reason, *direct_owner_reasons]))
    print(f"Hypothetical trading blocked reason: {blocked_reason}")
    print(f"Dry-run approved: {'YES' if validation.approved else 'NO'}")
    print(f"Dry-run reasons: {', '.join(validation.reasons) or 'none'}")
    print("Real submission enabled: NO")


def main() -> int:
    fixture_path = os.environ.get("DREAMDEX_READ_ONLY_FIXTURE")
    required = ["DREAMDEX_READ_ONLY_OWNER_ADDRESS"]
    if not fixture_path:
        required.extend(("DREAMDEX_READ_ONLY_BASE_URL", "DREAMDEX_READ_ONLY_RPC_URL"))
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        print("READ-ONLY ACCOUNT CHECK")
        print("Missing configuration: " + ", ".join(missing))
        print("No network request or order operation was attempted.")
        print("Real submission enabled: NO")
        return 2
    owner = os.environ["DREAMDEX_READ_ONLY_OWNER_ADDRESS"]
    try:
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
        _print_report(snapshot, report, validation)
        return 0
    except Exception as exc:
        print("READ-ONLY ACCOUNT CHECK")
        print(f"Read-only check failed: {_safe_error(exc, owner)}")
        print("No order submission was attempted.")
        print("Real submission enabled: NO")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
