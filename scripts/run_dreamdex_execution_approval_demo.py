"""Deterministic, offline-only demonstration of the approval ceremony."""
from __future__ import annotations

import argparse

from bot.execution.dreamdex_execution_approval import (
    InteractiveDreamDexExecutionApprovalProvider,
    build_execution_approval_preview,
    create_execution_approval_challenge,
    evaluate_execution_approval,
    revalidate_execution_approval,
)

MARKET = "0x" + "1" * 40
SIGNER = "0x" + "2" * 40


class _DeterministicProvider:
    provider_type = "deterministic_test_only"
    def __init__(self, answer: str) -> None: self.answer = answer
    def request_approval(self, preview, challenge_display_value, expiry_status): return self.answer if expiry_status == "current" else None


def _preview(*, nonce: int = 7, fee: int = 3, journal: str = "j" ):
    return build_execution_approval_preview(
        operation="place_order", chain_id=5031, market_symbol="SOMI:USDso", market_address=MARKET, signer_address=SIGNER,
        transaction_type="eip1559", side="BUY", order_type="LIMIT", normalized_price="1.25", normalized_quantity="2", order_notional="2.50",
        value_wei=0, nonce=nonce, gas_limit=21000, max_fee_per_gas_wei=fee, max_priority_fee_per_gas_wei=1, maximum_total_fee_wei=63000,
        calldata="0x12345678", unsigned_request_fingerprint="a" * 64, envelope_fingerprint="b" * 64, preflight_fingerprint="c" * 64,
        intent_fingerprint="d" * 64, reservation_fingerprint="e" * 64, lease_fingerprint="f" * 64, journal_snapshot_fingerprint=journal,
        market_evidence_fingerprint="g" * 64, account_evidence_fingerprint="h" * 64, risk_decision_fingerprint="i" * 64, fair_play_decision_fingerprint="j" * 64,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Offline synthetic execution-approval demo")
    parser.add_argument("--scenario", choices=("approved", "expired", "nonce-changed", "fees-changed", "journal-changed"), default="approved")
    parser.add_argument("--interactive", action="store_true")
    args = parser.parse_args(argv)
    preview = _preview()
    challenge = create_execution_approval_challenge(preview, issued_monotonic_ms=1000, maximum_age_ms=1000, challenge_generator=lambda: "APPROVE-TEST-0001")
    provider = InteractiveDreamDexExecutionApprovalProvider() if args.interactive else _DeterministicProvider(challenge.challenge_display_value)
    now = 2501 if args.scenario == "expired" else 1500
    approval = evaluate_execution_approval(challenge=challenge, preview=preview, provider=provider, now_monotonic_ms=now)
    changed = {"nonce-changed": _preview(nonce=8), "fees-changed": _preview(fee=4), "journal-changed": _preview(journal="changed")}.get(args.scenario, preview)
    revalidation = revalidate_execution_approval(approval=approval, original_preview=preview, current_preview=changed, now_monotonic_ms=now)
    print("SYNTHETIC EXECUTION APPROVAL DEMO")
    print(f"Scenario: {args.scenario}")
    print("Economic preview:")
    print(f"  Operation: {preview.operation}")
    print(f"  Side: {preview.side}")
    print(f"  Price: {preview.normalized_price}")
    print(f"  Quantity: {preview.normalized_quantity}")
    print(f"  Notional: {preview.order_notional}")
    print(f"  Native value: {preview.value_wei}")
    print(f"  Maximum transaction fee: {preview.maximum_total_fee_wei}")
    print(f"  Nonce: {preview.nonce}")
    print(f"  Transaction type: {preview.transaction_type}")
    print(f"Approval granted: {'YES' if approval.approved else 'NO'}")
    print(f"Post-approval revalidation: {revalidation.revalidation_status}")
    print("Signer invocation performed: NO")
    print("Submission call performed: NO")
    print("Production network used: NO")
    print("Production secret used: NO")
    print("Real submission enabled: NO")
    expected = args.scenario == "approved"
    actual = approval.approved and revalidation.revalidation_status == "confirmed"
    return 0 if expected == actual else 1


if __name__ == "__main__":
    raise SystemExit(main())
