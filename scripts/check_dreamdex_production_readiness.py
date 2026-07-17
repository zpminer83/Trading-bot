"""Offline-only DreamDEX production readiness checklist.

No environment, RPC URL, journal, keystore, prompt, signer, or submitter is
consulted here.  A non-zero result is intentional while production remains
disarmed.
"""
from __future__ import annotations

from bot.execution.dreamdex_production_readiness import (
    DreamDexProductionReadinessEvidence,
    DreamDexProductionReadinessPolicy,
    evaluate_production_readiness,
)
from bot.execution.dreamdex_execution_primitives import mask_hex_hash


def _yes(value: bool) -> str:
    return "YES" if value else "NO"


def main() -> int:
    decision = evaluate_production_readiness(DreamDexProductionReadinessPolicy(), DreamDexProductionReadinessEvidence())
    print("PRODUCTION EXECUTION READINESS:")
    print(f"  architecture ready: {_yes(decision.architecture_ready)}")
    print(f"  configuration ready: {_yes(decision.configuration_ready)}")
    print(f"  market evidence ready: {_yes(decision.market_ready)}")
    print(f"  account evidence ready: {_yes(decision.account_ready)}")
    print(f"  journal ready: {_yes(decision.journal_ready)}")
    print(f"  signer implementation: {'available_offline' if decision.architecture_ready else 'unavailable'}")
    print("  signer configured: NO")
    print("  secret provider ready: NO")
    print(f"  RPC policy ready: {_yes(decision.RPC_ready)}")
    print("  RPC chain confirmed: NO")
    print(f"  preflight ready: {_yes(decision.transaction_pipeline_ready)}")
    print("  nonce revalidation ready: NO")
    print("  signing lease ready: NO")
    print(f"  submission boundary ready: {_yes(decision.transaction_pipeline_ready)}")
    print(f"  receipt confirmation ready: {_yes(decision.confirmation_ready)}")
    print("  contract event confirmation ready: NO")
    print(f"  reconciliation ready: {_yes(decision.reconciliation_ready)}")
    print("  human approval capability: available_offline")
    print("  post-approval revalidation: unavailable")
    print("  production signer invocation allowed: NO")
    print("  real submission allowed: NO")
    print(f"  readiness blocker count: {len(decision.blockers)}")
    print(f"  readiness fingerprint: {mask_hex_hash(decision.readiness_fingerprint)}")
    return 0 if decision.readiness_status == "ready" and decision.allowed_to_submit_real_transaction else 2


if __name__ == "__main__":
    raise SystemExit(main())
