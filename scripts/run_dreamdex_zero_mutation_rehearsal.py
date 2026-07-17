"""Explicit offline zero-mutation production rehearsal.

The default invocation is intentionally blocked because it has no live
collector.  ``--fixture`` runs a deterministic read-only evidence fixture.
"""
from __future__ import annotations

import argparse
import json
from decimal import Decimal
from pathlib import Path

from bot.execution.dreamdex_zero_mutation_rehearsal import (
    DreamDexZeroMutationRehearsalEvidence,
    DreamDexZeroMutationRehearsalPolicy,
    build_rehearsal_candidate,
    run_zero_mutation_rehearsal,
)


def _fixture(policy: DreamDexZeroMutationRehearsalPolicy):
    evidence = DreamDexZeroMutationRehearsalEvidence(
        market_status="available", account_status="available", rpc_status="available", chain_id=5031,
        target_code_status="available", pending_nonce_status="available", native_balance_status="available",
        gas_estimate_status="available", fee_status="available", market_rules_status="available",
        runtime_gate_status="available", risk_status="available", fair_play_status="available",
        market_age_ms=0, account_age_ms=0, source_authority="authoritative", network_read_call_count=9,
        market_identity_status="confirmed", account_identity_status="confirmed", trading_enabled=True,
        contract_code_present=True, pending_nonce=7, gas_estimate=21000, estimated_fee_wei=1000, native_balance_wei=1000000,
        source_fingerprint="fixture-source", market_fingerprint="fixture-market", account_fingerprint="fixture-account",
    )
    candidate = build_rehearsal_candidate(market_symbol=policy.required_market_symbol or "SOMI:USDso", side="BUY",
                                           price=Decimal("1.0000"), quantity=Decimal("1.00"),
                                           market_rules={"tick_size": "0.0001", "quantity_step": "0.01", "minimum_quantity": "1", "minimum_notional": "1"},
                                           best_ask=Decimal("1.0010"), policy=policy)
    return evidence, candidate


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Offline DreamDEX zero-mutation rehearsal")
    parser.add_argument("--profile", default="offline", choices=("offline", "read-only"))
    parser.add_argument("--market", default="SOMI:USDso")
    parser.add_argument("--max-notional", default="100")
    parser.add_argument("--output-summary", type=Path)
    parser.add_argument("--fixture", action="store_true", help="use deterministic offline evidence")
    args = parser.parse_args(argv)
    policy = DreamDexZeroMutationRehearsalPolicy(required_market_symbol=args.market, maximum_order_notional=Decimal(args.max_notional))
    evidence, candidate = _fixture(policy) if args.fixture else (DreamDexZeroMutationRehearsalEvidence(), None)
    result = run_zero_mutation_rehearsal(policy=policy, evidence=evidence, candidate=candidate)
    data = result.safe_dict()
    print("ZERO-MUTATION PRODUCTION REHEARSAL")
    print(f"rehearsal execution performed: {'YES' if args.fixture else 'NO'}")
    for key in ("rehearsal_status", "network_read_call_count", "mutation_rpc_call_count", "production_journal_write_performed", "chain_evidence_status", "market_evidence_status", "market_rules_status", "trading_status", "account_evidence_status", "balance_status", "contract_code_status", "pending_nonce_status", "gas_estimate_status", "fee_evidence_status", "risk_status", "fair_play_status", "unsigned_request_status", "envelope_status", "preflight_status", "approval_preview_status", "approval_binding_status", "approval_prompt_performed", "keystore_read_performed", "password_prompt_performed", "signer_invocation_count", "submission_call_count", "ready_for_human_review", "ready_for_signer_invocation", "ready_for_real_submission"):
        print(f"{key}: {data[key]}")
    if candidate is not None:
        candidate_data = candidate.safe_dict()
        for key in ("operation", "side", "price", "quantity", "notional", "native_value", "maximum_transaction_fee", "nonce", "transaction_type", "candidate_fingerprint"):
            print(f"candidate {key}: {candidate_data[key]}")
    print(f"blockers: {', '.join(data['blockers']) or 'none'}")
    if args.output_summary:
        args.output_summary.parent.mkdir(parents=True, exist_ok=True)
        args.output_summary.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    return 0 if result.ready_for_human_review else 2


if __name__ == "__main__":
    raise SystemExit(main())
