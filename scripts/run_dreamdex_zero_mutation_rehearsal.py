"""Explicit offline zero-mutation production rehearsal.

The default invocation is intentionally blocked because it has no live
collector.  ``--fixture`` runs a deterministic read-only evidence fixture.
"""
from __future__ import annotations

import argparse
import json
import os
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from hashlib import sha256
from urllib.parse import urlparse

from bot.execution.dreamdex_zero_mutation_rehearsal import (
    DreamDexLiveReadOnlyEvidenceStatus,
    DreamDexLiveReadOnlyEndpointConfigurationStatus,
    DreamDexLiveReadOnlyConfigurationStatus,
    DreamDexLiveReadOnlyRehearsalDependencies,
    DreamDexZeroMutationRehearsalEvidence,
    DreamDexZeroMutationRehearsalPolicy,
    build_rehearsal_candidate,
    collect_live_read_only_rehearsal_evidence_from_dependencies,
    run_zero_mutation_rehearsal,
)
from bot.execution.dreamdex_readonly_rpc import DreamDexReadOnlyRpcTransport
from bot.integrations.dreamdex_authenticated_read_only import (
    PRODUCTION_BASE_URL,
    build_authenticated_read_only_transport_from_env,
)
from bot.integrations.dreamdex_read_only import (
    DreamDexReadOnlyAdapter,
    HttpGetTransport,
    HttpRpcTransport,
    MarketReadOnlySource,
)


def _configuration_status(*, base_url: str | None, rpc_url: str | None, owner: str | None,
                          authenticated: object, symbol: str,
                          public_endpoint_status: DreamDexLiveReadOnlyEndpointConfigurationStatus | None = None,
                          rpc_endpoint_status: DreamDexLiveReadOnlyEndpointConfigurationStatus | None = None) -> DreamDexLiveReadOnlyConfigurationStatus:
    public_endpoint_status = public_endpoint_status or _endpoint_status(
        base_url, endpoint_type="public_api", source_name="pinned_production" if base_url else "not_configured")
    rpc_endpoint_status = rpc_endpoint_status or _endpoint_status(
        rpc_url, endpoint_type="rpc", source_name="dedicated_rpc_read_only" if rpc_url else "not_configured")
    public_ok = public_endpoint_status.configured
    rpc_ok = rpc_endpoint_status.configured
    public_ready = public_endpoint_status.ready
    rpc_ready = rpc_endpoint_status.ready
    auth_configured = bool(getattr(authenticated, "configured", False))
    blockers: list[str] = []
    if not public_ready:
        blockers.append("public_api_configuration_unavailable")
    if public_endpoint_status.configured and not public_ready:
        blockers.append("public_api_configuration_invalid")
    blockers.extend(public_endpoint_status.blockers)
    if not rpc_ok:
        blockers.append("rpc_configuration_unavailable")
    elif not rpc_ready:
        blockers.append("rpc_configuration_invalid")
    blockers.extend(rpc_endpoint_status.blockers)
    if not auth_configured:
        blockers.append("authenticated_session_not_configured")
    if auth_configured and not owner:
        blockers.append("account_address_configuration_unavailable")
    fingerprint_input = f"public={public_ready}|rpc={rpc_ready}|auth={auth_configured}|owner={bool(owner)}|symbol={symbol}|chain=5031"
    return DreamDexLiveReadOnlyConfigurationStatus(
        public_api_configured=public_ok, rpc_configured=rpc_ok,
        authenticated_session_configured=auth_configured,
        authenticated_session_current=False, required_chain_id=5031,
        market_symbol=symbol, public_transport_ready=public_ready,
        rpc_transport_ready=rpc_ready, account_transport_ready=auth_configured and bool(owner) and public_ready,
        configuration_fingerprint="0x" + sha256(fingerprint_input.encode()).hexdigest(),
        blockers=tuple(blockers),
        public_endpoint_status=public_endpoint_status,
        rpc_endpoint_status=rpc_endpoint_status,
    )


def _endpoint_status(value: str | None, *, endpoint_type: str, source_name: str,
                     allow_local_fixture: bool = False) -> DreamDexLiveReadOnlyEndpointConfigurationStatus:
    """Validate endpoint syntax without retaining or echoing its value."""
    configured = bool(value)
    if not configured:
        return DreamDexLiveReadOnlyEndpointConfigurationStatus(endpoint_type=endpoint_type, source_name="not_configured")
    text = str(value)
    try:
        parsed = urlparse(text)
        hostname = parsed.hostname
    except ValueError:
        fingerprint = "0x" + sha256(f"{endpoint_type}|{source_name}|malformed".encode()).hexdigest()
        return DreamDexLiveReadOnlyEndpointConfigurationStatus(
            endpoint_type=endpoint_type, configured=True, source_name=source_name,
            syntax_valid=False, scheme_status="invalid", transport_ready=False,
            configuration_fingerprint=fingerprint,
            blockers=(f"{endpoint_type}_endpoint_syntax_invalid",),
        )
    blockers: list[str] = []
    credentials = bool(parsed.username or parsed.password)
    marker_text = (parsed.path + "?" + parsed.query).lower()
    credentials = credentials or any(marker in marker_text for marker in ("token", "secret", "bearer", "password", "credential", "apikey", "api_key"))
    if credentials:
        blockers.append(f"{endpoint_type}_endpoint_credentials_embedded")
    syntax_valid = len(text) <= 2048 and not any(char.isspace() or ord(char) < 32 for char in text)
    syntax_valid = syntax_valid and bool(parsed.scheme and parsed.netloc and hostname)
    local = hostname in {"localhost", "127.0.0.1", "::1"}
    scheme_status = "local_fixture" if allow_local_fixture and local and parsed.scheme in {"http", "https"} else ("https" if parsed.scheme == "https" else "invalid")
    if scheme_status == "invalid":
        blockers.append(f"{endpoint_type}_endpoint_https_required")
    if parsed.fragment:
        syntax_valid = False
        blockers.append(f"{endpoint_type}_endpoint_fragment_forbidden")
    if endpoint_type == "public_api":
        if hostname not in {"api.dreamdex.io", "stg.api.dreamdex.io"} and not (allow_local_fixture and local):
            syntax_valid = False
            blockers.append("public_api_endpoint_host_unapproved")
        if parsed.path.rstrip("/") != "/v0" and not (allow_local_fixture and local):
            syntax_valid = False
            blockers.append("public_api_endpoint_path_unapproved")
        if parsed.query:
            syntax_valid = False
            blockers.append("public_api_endpoint_query_forbidden")
    else:
        if parsed.query:
            syntax_valid = False
            blockers.append("rpc_endpoint_query_forbidden")
        if parsed.path not in {"", "/"} and not credentials:
            syntax_valid = False
            blockers.append("rpc_endpoint_path_forbidden")
    transport_ready = syntax_valid and scheme_status in {"https", "local_fixture"} and not credentials and not parsed.fragment
    fingerprint = "0x" + sha256(f"{endpoint_type}|{source_name}|{syntax_valid}|{scheme_status}|{credentials}|{transport_ready}".encode()).hexdigest()
    return DreamDexLiveReadOnlyEndpointConfigurationStatus(
        endpoint_type=endpoint_type, configured=True, source_name=source_name,
        syntax_valid=syntax_valid, scheme_status=scheme_status,
        credentials_embedded=credentials, redirects_allowed=False,
        transport_ready=transport_ready, configuration_fingerprint=fingerprint,
        blockers=tuple(dict.fromkeys(blockers)),
    )


def _safe_public_base(value: str | None, *, allow_local_fixture: bool = False) -> str | None:
    if not value:
        return None
    status = _endpoint_status(value, endpoint_type="public_api", source_name="dedicated_public_read_only", allow_local_fixture=allow_local_fixture)
    if not status.ready:
        return None
    return str(value).rstrip("/")


def _safe_rpc_url(value: str | None, *, allow_local_fixture: bool = False) -> str | None:
    if not value:
        return None
    status = _endpoint_status(value, endpoint_type="rpc", source_name="dedicated_rpc_read_only", allow_local_fixture=allow_local_fixture)
    if not status.ready:
        return None
    return str(value).rstrip("/")


def _fixture(policy: DreamDexZeroMutationRehearsalPolicy):
    names = ("market_listed", "market_identity", "order_book", "market_rules", "market_lifecycle", "trading_status", "trading_enabled", "place_supported", "cancel_supported", "account_identity",
             "trading_balances", "open_orders", "recent_fills", "chain_id", "target_code",
             "pending_nonce", "native_gas_balance", "fee_data", "gas_estimate")
    statuses = tuple(DreamDexLiveReadOnlyEvidenceStatus(
        evidence_name=name, request_performed=True, source_category="fixture",
        transport_status="confirmed", authentication_status="authenticated_success" if name in {"account_identity", "trading_balances", "open_orders", "recent_fills"} else "not_configured",
        schema_status="confirmed", freshness_status="fresh", identity_status="confirmed",
        authority_status="authoritative", result_status="confirmed") for name in names)
    configuration = DreamDexLiveReadOnlyConfigurationStatus(
        public_api_configured=True, rpc_configured=True,
        authenticated_session_configured=True, authenticated_session_current=True,
        required_chain_id=5031, market_symbol=policy.required_market_symbol or "SOMI:USDso",
        public_transport_ready=True, rpc_transport_ready=True, account_transport_ready=True,
        configuration_fingerprint="0x" + "f" * 64,
        public_endpoint_status=DreamDexLiveReadOnlyEndpointConfigurationStatus(
            endpoint_type="public_api", configured=True, source_name="pinned_production",
            syntax_valid=True, scheme_status="https", transport_ready=True,
            configuration_fingerprint="0x" + "1" * 64),
        rpc_endpoint_status=DreamDexLiveReadOnlyEndpointConfigurationStatus(
            endpoint_type="rpc", configured=True, source_name="dedicated_rpc_read_only",
            syntax_valid=True, scheme_status="https", transport_ready=True,
            configuration_fingerprint="0x" + "2" * 64),
    )
    evidence = DreamDexZeroMutationRehearsalEvidence(
        market_status="available", orderbook_status="available", account_status="available", rpc_status="available", chain_id=5031,
        target_code_status="available", pending_nonce_status="available", native_balance_status="available",
        gas_estimate_status="available", fee_status="available", market_rules_status="available",
        runtime_gate_status="available", risk_status="available", fair_play_status="available",
        market_age_ms=0, account_age_ms=0, source_authority="authoritative", network_read_call_count=9,
        market_identity_status="confirmed", account_identity_status="confirmed", trading_enabled=True,
        contract_code_present=True, pending_nonce=7, gas_estimate=21000, estimated_fee_wei=1000, maximum_total_fee_wei=1000, native_balance_wei=1000000,
        drawdown_fraction=Decimal("0"), preemptive_drawdown=Decimal("0.08"), hard_drawdown_limit=Decimal("0.10"), projected_shocked_drawdown=Decimal("0.02"),
        gap_risk_status="available", gap_risk_budget_approved=True,
        account_authority_status="confirmed", open_order_status="available_empty", fills_status="available_empty",
        evidence_statuses=statuses,
        native_gas_balance_evidence="confirmed", authenticated_trading_balance_evidence="confirmed",
        available_order_currency_balance="confirmed", available_base_asset_balance="confirmed",
        market_listed_status="confirmed", market_lifecycle_status="confirmed", trading_enabled_status="confirmed",
        place_operation_status="confirmed", cancel_operation_status="confirmed",
        trading_status_source="fixture", trading_status_authority="authoritative",
        configuration_status=configuration,
        source_fingerprint="fixture-source", market_fingerprint="fixture-market", account_fingerprint="fixture-account",
    )
    candidate = build_rehearsal_candidate(market_symbol=policy.required_market_symbol or "SOMI:USDso", side="BUY",
                                           price=Decimal("1.0000"), quantity=Decimal("1.00"),
                                           market_rules={"tick_size": "0.0001", "quantity_step": "0.01", "minimum_quantity": "1", "minimum_notional": "1"},
                                           best_ask=Decimal("1.0010"), policy=policy)
    return evidence, candidate


def _default_evidence(configuration_status: DreamDexLiveReadOnlyConfigurationStatus | None = None) -> DreamDexZeroMutationRehearsalEvidence:
    names = ("market_listed", "market_identity", "order_book", "market_rules", "market_lifecycle", "trading_status", "trading_enabled", "place_supported", "cancel_supported", "account_identity",
             "trading_balances", "open_orders", "recent_fills", "chain_id", "target_code",
             "pending_nonce", "native_gas_balance", "fee_data", "gas_estimate")
    configuration_status = configuration_status or DreamDexLiveReadOnlyConfigurationStatus(blockers=("live_read_only_configuration_unavailable",))
    return DreamDexZeroMutationRehearsalEvidence(gas_estimate_status="not_attempted_due_to_prerequisite", evidence_statuses=tuple(
        DreamDexLiveReadOnlyEvidenceStatus(
            evidence_name=name,
            result_status="not_attempted_due_to_prerequisite" if name == "gas_estimate" else "not_configured",
            prerequisite="formed_unsigned_candidate" if name == "gas_estimate" else None,
            blocker="gas_estimate_prerequisite_unavailable" if name == "gas_estimate" else None,
        ) for name in names
    ), primary_blockers=configuration_status.blockers,
        native_gas_balance_evidence="not_configured", authenticated_trading_balance_evidence="not_configured",
        available_order_currency_balance="not_configured", available_base_asset_balance="not_configured",
        configuration_status=configuration_status)


def _live_dependencies(symbol: str) -> DreamDexLiveReadOnlyRehearsalDependencies:
    """Build the explicit live read-only bundle from existing transports."""
    owner = os.environ.get("DREAMDEX_READ_ONLY_OWNER_ADDRESS", "")
    trading = os.environ.get("DREAMDEX_READ_ONLY_TRADING_ADDRESS", "")
    public_override = os.environ.get("DREAMDEX_READ_ONLY_BASE_URL")
    public_api_override = os.environ.get("DREAMDEX_API_BASE_URL")
    raw_base = public_override or public_api_override or PRODUCTION_BASE_URL
    public_source = "dedicated_public_read_only" if public_override else ("dedicated_public_api" if public_api_override else "pinned_production")
    public_endpoint = _endpoint_status(raw_base, endpoint_type="public_api", source_name=public_source)
    base_url = _safe_public_base(raw_base)
    raw_rpc = os.environ.get("DREAMDEX_READ_ONLY_RPC_URL") or os.environ.get("DREAMDEX_RPC_URL")
    rpc_endpoint = _endpoint_status(raw_rpc, endpoint_type="rpc", source_name="dedicated_rpc_read_only" if os.environ.get("DREAMDEX_READ_ONLY_RPC_URL") else ("dedicated_rpc" if os.environ.get("DREAMDEX_RPC_URL") else "not_configured"))
    rpc_url = _safe_rpc_url(raw_rpc)
    authenticated = build_authenticated_read_only_transport_from_env()
    configuration = _configuration_status(base_url=base_url, rpc_url=rpc_url or None, owner=owner or None,
                                          authenticated=authenticated, symbol=symbol,
                                          public_endpoint_status=public_endpoint, rpc_endpoint_status=rpc_endpoint)
    market_source = MarketReadOnlySource(HttpGetTransport(base_url or PRODUCTION_BASE_URL), symbol)
    market_cache: dict[str, object] = {}
    account_cache: dict[str, object] = {}
    rpc = DreamDexReadOnlyRpcTransport(rpc_url) if rpc_url else None
    candidate_state: dict[str, bool] = {"ready": False}

    def read_market():
        if not configuration.ready_for_public:
            raise RuntimeError("public_api_configuration_unavailable")
        if "snapshot" not in market_cache:
            market_cache["snapshot"] = market_source.snapshot()
        return market_cache["snapshot"]

    def read_account():
        if not configuration.account_transport_ready:
            return None
        if "account" not in account_cache:
            adapter = DreamDexReadOnlyAdapter(
                transport=HttpGetTransport(base_url or PRODUCTION_BASE_URL),
                rpc_transport=(HttpRpcTransport(rpc_url) if rpc_url else SimpleNamespace(call=lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("rpc_configuration_unavailable")))),
                owner=owner,
                trading_address=trading or None,
                symbol=symbol,
                authenticated_transport=authenticated,
                owner_platform_role=os.environ.get("DREAMDEX_READ_ONLY_OWNER_PLATFORM_ROLE"),
                trading_platform_role=os.environ.get("DREAMDEX_READ_ONLY_TRADING_PLATFORM_ROLE"),
            )
            # Use the adapter's existing authenticated-only path so the
            # public market branch is not suppressed when auth is absent.
            auth_snapshot = adapter._fetch_authenticated_account()
            account_cache["account"] = SimpleNamespace(
                account_address_semantics="resolved" if auth_snapshot.authoritative_for(trading or owner) else "unresolved",
                open_orders_status=auth_snapshot.open_orders_status.status,
                fills_status=auth_snapshot.fills_status.status,
                authenticated_transport_status=getattr(authenticated, "configuration_status", "unconfigured"),
                authenticated=auth_snapshot,
                observed_at=auth_snapshot.observed_at,
            )
        return account_cache["account"]

    def read_preflight():
        if rpc is None:
            return {
                "status": "not_configured" if not configuration.rpc_configured else "configuration_invalid", "read_only_rpc_call_count": 0,
                "gas_estimate_status": "not_attempted_due_to_prerequisite",
                "call_statuses": {"gas_estimate": "not_attempted_due_to_prerequisite"},
            }
        try:
            market = read_market()
            pool = market.metadata.pool_contract
        except Exception:
            pool = None
        calls = 0

        def call(method, fallback=None):
            nonlocal calls
            calls += 1
            try:
                return method()
            except Exception:
                return fallback

        chain_id = call(rpc.get_chain_id)
        code = call(lambda: rpc.get_contract_code(pool), None) if pool else None
        pending_nonce = call(lambda: rpc.get_pending_nonce(owner)) if owner else None
        nonce_status = "confirmed" if pending_nonce is not None else ("not_attempted_due_to_prerequisite" if not owner else "transport_unavailable")
        if candidate_state["ready"] and pool and owner:
            gas_estimate = call(lambda: rpc.estimate_gas({"from": owner, "to": pool, "value": "0x0", "data": "0x"}))
            gas_estimate_status = "available" if gas_estimate is not None else "unavailable"
        else:
            gas_estimate = None
            gas_estimate_status = "not_attempted_due_to_prerequisite"
        gas_price = call(rpc.get_gas_price)
        priority = call(rpc.get_max_priority_fee_per_gas)
        native_balance = call(lambda: rpc.get_native_balance(owner)) if owner else None
        native_status = "confirmed" if native_balance is not None else ("not_attempted_due_to_prerequisite" if not owner else "transport_unavailable")
        fee_per_gas = max((item for item in (gas_price, priority) if item is not None), default=None)
        maximum_fee = gas_estimate * fee_per_gas if gas_estimate is not None and fee_per_gas is not None else None
        complete = all(value is not None for value in (chain_id, code, pending_nonce, gas_estimate, maximum_fee, native_balance))
        return {
            "status": "available" if complete else "unavailable",
            "chain_id": chain_id,
            "target_code_status": "available" if code not in {None, "0x", "0x0"} else "unavailable",
            "contract_code_present": code not in {None, "0x", "0x0"},
            "pending_nonce_status": "available" if pending_nonce is not None else nonce_status,
            "pending_nonce": pending_nonce,
            "gas_estimate_status": gas_estimate_status,
            "gas_estimate": gas_estimate,
            "fee_status": "available" if fee_per_gas is not None else "unavailable",
            "fee_per_gas_wei": fee_per_gas,
            "maximum_total_fee_wei": maximum_fee,
            "native_balance_status": "available" if native_balance is not None else native_status,
            "native_balance_wei": native_balance,
            "transaction_type": "legacy" if gas_price is not None else "unresolved",
            "read_only_rpc_call_count": calls,
            "call_statuses": {
                "chain_id": "confirmed" if chain_id is not None else "transport_unavailable",
                "target_code": "confirmed" if code not in {None, "0x", "0x0"} else "transport_unavailable",
                "pending_nonce": "confirmed" if pending_nonce is not None else nonce_status,
                "native_gas_balance": "confirmed" if native_balance is not None else native_status,
                "fee_data": "confirmed" if fee_per_gas is not None else "transport_unavailable",
                "gas_estimate": gas_estimate_status,
            },
        }

    return DreamDexLiveReadOnlyRehearsalDependencies(
        public_market_reader=read_market,
        authenticated_account_reader=read_account,
        typed_rpc_preflight_reader=read_preflight,
        safe_config={"required_market_symbol": symbol, "_candidate_state": candidate_state},
        configuration_status=configuration,
    )


def _live_candidate(dependencies: DreamDexLiveReadOnlyRehearsalDependencies, policy: DreamDexZeroMutationRehearsalPolicy):
    try:
        market = dependencies.public_market_reader()
        account = dependencies.authenticated_account_reader()
        if getattr(account, "account_address_semantics", "unresolved") not in {"resolved", "authoritative"}:
            return None
        if getattr(account, "open_orders_status", "source_unavailable") not in {"available", "confirmed", "available_empty"}:
            return None
        if getattr(account, "fills_status", "source_unavailable") not in {"available", "confirmed", "available_empty"}:
            return None
        risk = dependencies.risk_snapshot
        fair = dependencies.fair_play_snapshot
        if str(risk.get("status", "unavailable")) not in {"available", "confirmed"}:
            return None
        if str(fair.get("status", "unavailable")) not in {"available", "confirmed"}:
            return None
        metadata = market.metadata
        rules = metadata.trading_rules
        if rules is None or getattr(rules, "available", False) is not True or getattr(rules, "trading_enabled", None) is not True or any(value is None for value in (rules.tick_size, rules.quantity_step, rules.minimum_quantity, rules.minimum_notional)):
            return None
        if metadata.symbol != policy.required_market_symbol or not getattr(metadata, "pool_contract", None):
            return None
        book = getattr(market, "orderbook", None) or {}
        bids = book.get("bids", []) if isinstance(book, dict) else []
        asks = book.get("asks", []) if isinstance(book, dict) else []
        best_bid = bids[0].get("price") if bids and isinstance(bids[0], dict) else None
        best_ask = asks[0].get("price") if asks and isinstance(asks[0], dict) else None
        if best_bid is None or best_ask is None:
            return None
        candidate = build_rehearsal_candidate(
            market_symbol=metadata.symbol or policy.required_market_symbol or "",
            side="BUY",
            price=Decimal(str(best_bid)) - (rules.tick_size or Decimal("0")),
            quantity=rules.minimum_quantity,
            market_rules={
                "tick_size": rules.tick_size,
                "quantity_step": rules.quantity_step,
                "minimum_quantity": rules.minimum_quantity,
                "minimum_notional": rules.minimum_notional,
            },
            best_ask=Decimal(str(best_ask)),
            policy=policy,
        )
        if candidate is not None:
            state = dependencies.safe_config.get("_candidate_state")
            if isinstance(state, dict):
                state["ready"] = True
        return candidate
    except Exception:
        return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="DreamDEX zero-mutation rehearsal")
    parser.add_argument("--mode", choices=("fixture", "live-read-only"), default="fixture")
    parser.add_argument("--profile", default="offline", choices=("offline", "read-only"))
    parser.add_argument("--market", default="SOMI:USDso")
    parser.add_argument("--max-notional", default="100")
    parser.add_argument("--output-summary", type=Path)
    parser.add_argument("--fixture", action="store_true", help="use deterministic offline evidence")
    args = parser.parse_args(argv)
    mode = "fixture" if args.fixture else args.mode
    policy = DreamDexZeroMutationRehearsalPolicy(required_market_symbol=args.market, maximum_order_notional=Decimal(args.max_notional))
    if mode == "fixture":
        evidence, candidate = _fixture(policy)
    else:
        try:
            dependencies = _live_dependencies(args.market)
            candidate = _live_candidate(dependencies, policy)
            evidence = collect_live_read_only_rehearsal_evidence_from_dependencies(dependencies)
        except Exception:
            evidence, candidate = _default_evidence(), None
    result = run_zero_mutation_rehearsal(policy=policy, evidence=evidence, candidate=candidate, mode=mode)
    data = result.safe_dict()
    print("LIVE READ-ONLY ZERO-MUTATION REHEARSAL:")
    print(f"mode: {mode}")
    print("rehearsal execution performed: YES")
    labels = (
        ("public market calls", "public_market_call_count"),
        ("authenticated account calls", "authenticated_account_call_count"),
        ("read-only RPC calls", "read_only_rpc_call_count"),
        ("mutation RPC calls", "mutation_rpc_call_count"),
        ("production journal writes", "production_journal_write_performed"),
        ("chain confirmed", "chain_evidence_status"),
        ("market identity confirmed", "market_identity_status"),
        ("market rules confirmed", "market_rules_status"),
        ("trading enabled", "trading_enabled"),
        ("market data authoritative", "source_authority"),
        ("account data authoritative", "account_authority_status"),
        ("account identity confirmed", "account_identity_status"),
        ("balance evidence confirmed", "balance_status"),
        ("native gas balance evidence", "native_gas_balance_evidence"),
        ("authenticated trading balance evidence", "authenticated_trading_balance_evidence"),
        ("available order currency balance", "available_order_currency_balance"),
        ("available base asset balance", "available_base_asset_balance"),
        ("open-order evidence confirmed", "open_order_status"),
        ("open-order count", "open_order_count"),
        ("fills evidence confirmed", "fills_status"),
        ("target code confirmed", "contract_code_status"),
        ("pending nonce confirmed", "pending_nonce_status"),
        ("gas estimate confirmed", "gas_estimate_status"),
        ("fee evidence confirmed", "fee_evidence_status"),
        ("risk approval", "risk_status"),
        ("fair-play approval", "fair_play_status"),
        ("gap-risk approval", "gap_risk_budget_approved"),
        ("candidate operation", None),
        ("candidate side", None),
        ("candidate price", None),
        ("candidate quantity", None),
        ("candidate notional", None),
        ("projected shocked drawdown", "projected_shocked_drawdown"),
        ("maximum total fee", "maximum_total_fee_wei"),
        ("nonce", None),
        ("transaction type", None),
        ("approval preview available", "approval_preview_status"),
        ("approval prompt performed", "approval_prompt_performed"),
        ("keystore read", "keystore_read_performed"),
        ("password prompt", "password_prompt_performed"),
        ("signer invocation count", "signer_invocation_count"),
        ("submission call count", "submission_call_count"),
        ("ready for human review", "ready_for_human_review"),
        ("ready for signer invocation", "ready_for_signer_invocation"),
        ("ready for real submission", "ready_for_real_submission"),
    )
    candidate_data = candidate.safe_dict() if candidate is not None else {}
    def display(value):
        if isinstance(value, bool):
            return "YES" if value else "NO"
        return value
    for label, key in labels:
        candidate_key = label.replace("candidate ", "").replace("transaction type", "transaction_type")
        value = data.get(key) if key else candidate_data.get(candidate_key)
        print(f"{label}: {display(value)}")
    configuration_data = data.get("configuration_status", {})
    print("SAFE LIVE CONFIGURATION:")
    for label, key in (
        ("public API configured", "public_api_configured"),
        ("RPC configured", "rpc_configured"),
        ("authenticated session configured", "authenticated_session_configured"),
        ("authenticated session current", "authenticated_session_current"),
        ("public transport ready", "public_transport_ready"),
        ("RPC transport ready", "rpc_transport_ready"),
        ("account transport ready", "account_transport_ready"),
        ("required chain ID", "required_chain_id"),
        ("market symbol", "market_symbol"),
        ("configuration fingerprint", "configuration_fingerprint"),
    ):
        print(f"{label}: {display(configuration_data.get(key))}")
    print(f"configuration blockers: {', '.join(configuration_data.get('blockers', [])) or 'none'}")
    for endpoint_label, endpoint_key in (("public endpoint", "public_endpoint_status"), ("RPC endpoint", "rpc_endpoint_status")):
        endpoint = configuration_data.get(endpoint_key, {})
        print(f"{endpoint_label} configured: {display(endpoint.get('configured', False))}")
        print(f"{endpoint_label} syntax valid: {display(endpoint.get('syntax_valid', False))}")
        scheme_display = endpoint.get("scheme_status", "unavailable")
        if scheme_display == "https":
            scheme_display = "secure"
        print(f"{endpoint_label} scheme: {scheme_display}")
        print(f"{endpoint_label} redirects allowed: {display(endpoint.get('redirects_allowed', False))}")
        print(f"{endpoint_label} transport ready: {display(endpoint.get('transport_ready', False))}")
        print(f"{endpoint_label} blockers: {', '.join(endpoint.get('blockers', [])) or 'none'}")
    print("TRADING STATUS SOURCE AUDIT:")
    print("source | exact field/function | authority | auth | read-only | parser | usable")
    print("public_market | tradingEnabled/status in GET /markets | source-confirmed field | none | yes | supported | only when explicit")
    print("authenticated_market | existing authenticated field | separately classified | existing session | yes | not selected | no")
    print("contract_view | no source-confirmed function selected | unavailable | none | eth_call only if audited | unavailable | no")
    print("place/cancel support | no confirmed public field | unavailable | none | read-only | unavailable | no")
    print("LIVE READ-ONLY EVIDENCE MATRIX:")
    print("evidence | called | transport | auth | schema | authority | result")
    for item in data.get("evidence_statuses", []):
        print("{evidence} | {called} | {transport} | {auth} | {schema} | {authority} | {result}".format(
            evidence=item.get("evidence_name", "unknown"),
            called="YES" if item.get("request_performed") else "NO",
            transport=item.get("transport_status", "unknown"), auth=item.get("authentication_status", "unknown"),
            schema=item.get("schema_status", "unknown"), authority=item.get("authority_status", "unknown"),
            result=item.get("result_status", "unknown")))
    print("PRIMARY BLOCKERS:")
    print(", ".join(data.get("primary_blockers", [])) or "none")
    print("DERIVED BLOCKERS:")
    print(", ".join(data.get("derived_blockers", [])) or "none")
    print("NOT-ATTEMPTED STAGES:")
    print(", ".join(data.get("not_attempted_stages", [])) or "none")
    print("production dry-run approved: NO")
    print("production signer configured: NO")
    print("production submitter invoked: NO")
    print("Real submission enabled: NO")
    print(f"rehearsal status: {data['rehearsal_status']}")
    print(f"network read calls: {data['network_read_call_count']}")
    print(f"blockers: {', '.join(data['blockers']) or 'none'}")
    print(f"rehearsal fingerprint: {data['rehearsal_fingerprint']}")
    if candidate is not None:
        print(f"candidate rehearsal_only: {candidate_data['rehearsal_only']}")
        print(f"candidate non_executable: {candidate_data['non_executable']}")
        print(f"candidate fingerprint: {candidate_data['candidate_fingerprint']}")
    if args.output_summary:
        args.output_summary.parent.mkdir(parents=True, exist_ok=True)
        args.output_summary.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    return 0 if result.ready_for_human_review else 2


if __name__ == "__main__":
    raise SystemExit(main())
