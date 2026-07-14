"""Run deterministic, offline execution fault-recovery scenarios."""
from __future__ import annotations

from bot.analytics.execution_fault_scenarios import run_all_fault_scenarios


def main() -> int:
    results = run_all_fault_scenarios()
    print("EXECUTION FAULT-RECOVERY SCENARIOS")
    print(f"{'Scenario':32} {'Result':6} {'Unresolved':10} {'Duplicate':9} {'Partial':8}")
    print("-" * 75)
    for result in results:
        print(f"{result.scenario:32} {'PASS' if result.passed else 'FAIL':6} {result.unresolved_orders:10d} {result.duplicate_fill_count:9d} {result.partial_fill_count:8d}")
        for note in result.notes:
            print(f"  note: {note}")
    overall = all(result.passed for result in results)
    print(f"Overall result: {'PASS' if overall else 'FAIL'}")
    return 0 if overall else 1


if __name__ == "__main__":
    raise SystemExit(main())
