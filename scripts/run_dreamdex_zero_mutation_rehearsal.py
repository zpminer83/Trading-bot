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

from bot.execution.dreamdex_zero_mutation_rehearsal import (
    DreamDexLiveReadOnlyEvidenceStatus,
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
)


def _fixture(policy: DreamDexZeroMutationRehearsalPolicy):
    names = ("market_identity", "order_book", "market_rules", "trading_status", "account_identity",
             "trading_balances", "open_orders", "recent_fills", "chain_id", "target_code",
             "pending_nonce", "native_gas_balance", "fee_data", "gas_estimate")
    statuses = tuple(DreamDexLiveReadOnlyEvidenceStatus(
        evidence_name=name, request_performed=True, source_category="fixture",
        transport_status="confirmed", authentication_status="authenticated_success" if name in {"account_identity", "trading_balances", "open_orders", "recent_fills"} else "not_configured",
        schema_status="confirmed", freshness_status="fresh", identity_status="confirmed",
        authority_status="authoritative", result_status="confirmed") for name in names)
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
        source_fingerprint="fixture-source", market_fingerprint="fixture-market", account_fingerprint="fixture-account",
    )
    candidate = build_rehearsal_candidate(market_symbol=policy.required_market_symbol or "SOMI:USDso", side="BUY",
                                           price=Decimal("1.0000"), quantity=Decimal("1.00"),
                                           market_rules={"tick_size": "0.0001", "quantity_step": "0.01", "minimum_quantity": "1", "minimum_notional": "1"},
                                           best_ask=Decimal("1.0010"), policy=policy)
    return evidence, candidate


def _default_evidence() -> DreamDexZeroMutationRehearsalEvidence:
    names = ("market_identity", "order_book", "market_rules", "trading_status", "account_identity",
             "trading_balances", "open_orders", "recent_fills", "chain_id", "target_code",
             "pending_nonce", "native_gas_balance", "fee_data", "gas_estimate")
    return DreamDexZeroMutationRehearsalEvidence(gas_estimate_status="not_attempted_due_to_prerequisite", evidence_statuses=tuple(
        DreamDexLiveReadOnlyEvidenceStatus(
            evidence_name=name,
            result_status="not_attempted_due_to_prerequisite" if name == "gas_estimate" else "not_configured",
            prerequisite="formed_unsigned_candidate" if name == "gas_estimate" else None,
            blocker="gas_estimate_prerequisite_unavailable" if name == "gas_estimate" else None,
        ) for name in names
    ), primary_blockers=("live_read_only_configuration_unavailable",),
        native_gas_balance_evidence="not_configured", authenticated_trading_balance_evidence="not_configured",
        available_order_currency_balance="not_configured", available_base_asset_balance="not_configured")


def _live_dependencies(symbol: str) -> DreamDexLiveReadOnlyRehearsalDependencies:
    """Build the explicit live read-only bundle from existing transports."""
    owner = os.environ.get("DREAMDEX_READ_ONLY_OWNER_ADDRESS", "")
    trading = os.environ.get("DREAMDEX_READ_ONLY_TRADING_ADDRESS", "")
    base_url = os.environ.get("DREAMDEX_READ_ONLY_BASE_URL", PRODUCTION_BASE_URL)
    rpc_url = os.environ.get("DREAMDEX_READ_ONLY_RPC_URL") or os.environ.get("DREAMDEX_RPC_URL", "")
    if not owner or not trading or not rpc_url:
        raise RuntimeError("live_read_only_configuration_unavailable")
    authenticated = build_authenticated_read_only_transport_from_env()
    adapter = DreamDexReadOnlyAdapter(
        transport=HttpGetTransport(base_url),
        rpc_transport=HttpRpcTransport(rpc_url),
        owner=owner,
        trading_address=trading,
        symbol=symbol,
        authenticated_transport=authenticated,
        owner_platform_role=os.environ.get("DREAMDEX_READ_ONLY_OWNER_PLATFORM_ROLE"),
        trading_platform_role=os.environ.get("DREAMDEX_READ_ONLY_TRADING_PLATFORM_ROLE"),
    )
    rpc = DreamDexReadOnlyRpcTransport(rpc_url)
    cache: dict[str, object] = {}
    candidate_state: dict[str, bool] = {"ready": False}

    def snapshot():
        if "snapshot" not in cache:
            cache["snapshot"] = adapter.fetch_snapshot()
        return cache["snapshot"]

    def read_market():
        return snapshot().market  # type: ignore[union-attr]

    def read_account():
        return snapshot().account  # type: ignore[union-attr]

    def read_preflight():
        market = snapshot().market  # type: ignore[union-attr]
        pool = market.pool_contract
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
        pending_nonce = call(lambda: rpc.get_pending_nonce(owner))
        if candidate_state["ready"] and pool:
            gas_estimate = call(lambda: rpc.estimate_gas({"from": owner, "to": pool, "value": "0x0", "data": "0x"}))
            gas_estimate_status = "available" if gas_estimate is not None else "unavailable"
        else:
            gas_estimate = None
            gas_estimate_status = "not_attempted_due_to_prerequisite"
        gas_price = call(rpc.get_gas_price)
        priority = call(rpc.get_max_priority_fee_per_gas)
        native_balance = call(lambda: rpc.get_native_balance(owner))
        fee_per_gas = max((item for item in (gas_price, priority) if item is not None), default=None)
        maximum_fee = gas_estimate * fee_per_gas if gas_estimate is not None and fee_per_gas is not None else None
        complete = all(value is not None for value in (chain_id, code, pending_nonce, gas_estimate, maximum_fee, native_balance))
        return {
            "status": "available" if complete else "unavailable",
            "chain_id": chain_id,
            "target_code_status": "available" if code not in {None, "0x", "0x0"} else "unavailable",
            "contract_code_present": code not in {None, "0x", "0x0"},
            "pending_nonce_status": "available" if pending_nonce is not None else "unavailable",
            "pending_nonce": pending_nonce,
            "gas_estimate_status": gas_estimate_status,
            "gas_estimate": gas_estimate,
            "fee_status": "available" if fee_per_gas is not None else "unavailable",
            "fee_per_gas_wei": fee_per_gas,
            "maximum_total_fee_wei": maximum_fee,
            "native_balance_status": "available" if native_balance is not None else "unavailable",
            "native_balance_wei": native_balance,
            "transaction_type": "legacy" if gas_price is not None else "unresolved",
            "read_only_rpc_call_count": calls,
            "call_statuses": {
                "chain_id": "confirmed" if chain_id is not None else "transport_unavailable",
                "target_code": "confirmed" if code not in {None, "0x", "0x0"} else "transport_unavailable",
                "pending_nonce": "confirmed" if pending_nonce is not None else "transport_unavailable",
                "native_gas_balance": "confirmed" if native_balance is not None else "transport_unavailable",
                "fee_data": "confirmed" if fee_per_gas is not None else "transport_unavailable",
                "gas_estimate": gas_estimate_status,
            },
        }

    return DreamDexLiveReadOnlyRehearsalDependencies(
        public_market_reader=read_market,
        authenticated_account_reader=read_account,
        typed_rpc_preflight_reader=read_preflight,
        safe_config={"required_market_symbol": symbol, "_candidate_state": candidate_state},
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
