"""Offline aggregation of several independently audited paper burn-in runs.

Each input is first passed through :mod:`paper_burn_in_analyzer`.  This module
only aggregates the typed results and a bounded, read-only event projection;
it does not reimplement the single-run integrity rules and never stores raw
JSONL records in its result models.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
from statistics import median
from typing import Any, Mapping, Sequence

from bot.analytics import paper_burn_in_analyzer as single
from bot.analytics.paper_burn_in_fair_play_incident_analyzer import (
    PaperBurnInFairPlayIncidentAnalysis,
    PaperBurnInFairPlayReason,
    analyze_paper_burn_in_fair_play_incident,
    normalize_fair_play_reason,
)


MIN_RUN_DURATION_SECONDS = Decimal("1500")
MIN_QUALIFYING_RUNS = 3
MIN_ACCEPTED_SNAPSHOTS = 900
MIN_ACCEPTED_RATIO = Decimal("0.95")
HARD_DRAWdown_LIMIT = Decimal("0.10")
CONFIGURED_ADVERSE_MOVE = Decimal("0.12")

FAIR_PLAY_REASONS = frozenset({
    "rapid_round_trip", "repeated_near_flat_cycle", "excessive_cancel_replace",
    "repetitive_same_price_intent", "intent_fill_ratio", "insufficient_fill_evidence",
    "artificial_volume_pattern", "undisclosed_filter_decision",
    "consecutive_rejection_limit", "unknown_explicit_reason",
})


def _dec(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _utc(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _records(path: Path) -> list[dict[str, Any]]:
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return []
    values: list[dict[str, Any]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if isinstance(value, dict):
            values.append(value)
    return values


def _safe_input(path: str | Path, repository_root: str | Path | None) -> Path:
    # Calling the single-run analyzer is the source of truth for traversal,
    # symlink, size, and UTF-8 policy.  This second call only obtains the
    # resolved path for the bounded event projection.
    root = Path(repository_root or Path.cwd()).resolve()
    raw = Path(str(path))
    if repository_root is None and raw.is_absolute():
        raise ValueError("input_path_must_be_repository_relative")
    if not raw.is_absolute() and ".." in raw.parts:
        raise ValueError("input_path_must_be_repository_relative")
    candidate = (raw if raw.is_absolute() else root / raw).resolve(strict=False)
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError("input_path_outside_repository") from exc
    return candidate


@dataclass(frozen=True)
class PaperBurnInCampaignRunResult:
    input_file: str
    analysis: single.PaperBurnInAnalysisSummary
    qualifying: bool
    declared_result: str | None = None
    run_start: datetime | None = None
    run_end: datetime | None = None
    initial_equity: Decimal | None = None
    ending_equity: Decimal | None = None
    absolute_pnl: Decimal | None = None
    percentage_pnl: Decimal | None = None
    fees: Decimal | None = None
    ending_inventory: Decimal | None = None
    maximum_abs_inventory: Decimal | None = None
    fair_play_rejection_reasons: tuple[tuple[str, int], ...] = ()
    fair_play_max_consecutive_rejections: int = 0
    fill_timestamps: tuple[str, ...] = ()
    executed_prices: tuple[Decimal, ...] = ()
    configuration_fingerprint: str | None = None
    fair_play_incident: PaperBurnInFairPlayIncidentAnalysis | None = None
    fair_play_enforcement: str = "PASS"
    fair_play_compatibility: str = "PASS"

    @property
    def result(self) -> str:
        return self.declared_result or self.analysis.result

    @property
    def symbol(self) -> str | None:
        return self.analysis.integrity.symbol

    @property
    def fingerprint(self) -> str | None:
        return self.analysis.integrity.run_fingerprint


@dataclass(frozen=True)
class PaperBurnInCampaignMarketResult:
    status: str
    input_runs: int = 0
    qualifying_runs: int = 0
    total_duration_seconds: Decimal = Decimal("0")
    total_records: int = 0
    total_accepted_snapshots: int = 0
    total_rejected_snapshots: int = 0
    accepted_ratio: Decimal | None = None
    stale_count: int = 0
    crossed_count: int = 0
    malformed_count: int = 0
    maximum_consecutive_failures: int = 0
    maximum_timestamp_gap_seconds: Decimal | None = None
    median_run_median_gap_seconds: Decimal | None = None
    minimum_spread: Decimal | None = None
    median_spread: Decimal | None = None
    maximum_spread: Decimal | None = None
    maximum_mid_price_step: Decimal | None = None
    distinct_mid_price_count: int = 0
    distinct_bid_level_count: int = 0
    distinct_ask_level_count: int = 0
    healthy_runs: int = 0
    degraded_runs: int = 0
    invalid_runs: int = 0
    insufficient_duration_runs: int = 0
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def healthy_market_runs(self) -> int:
        return self.healthy_runs


@dataclass(frozen=True)
class PaperBurnInCampaignExecutionResult:
    status: str
    total_strategy_intents: int = 0
    total_risk_approved_intents: int = 0
    total_risk_rejected_intents: int = 0
    total_fair_play_approved_intents: int = 0
    total_fair_play_rejected_intents: int = 0
    total_paper_orders: int = 0
    total_cancels: int = 0
    total_replacements: int = 0
    total_partial_fills: int = 0
    total_full_fills: int = 0
    total_fills: int = 0
    fills_per_run: tuple[int, ...] = ()
    fills_per_hour: Decimal | None = None
    runs_with_fills: int = 0
    distinct_fill_timestamps: int = 0
    distinct_executed_price_levels: int = 0
    inventory_transitions: int = 0
    maximum_abs_inventory: Decimal | None = None
    final_inventory_per_run: tuple[Decimal | None, ...] = ()
    intent_to_fill_ratio: Decimal | None = None
    order_to_fill_ratio: Decimal | None = None
    strategy_activity: str = "NO_ACTIVITY"
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class PaperBurnInCampaignRiskResult:
    status: str
    maximum_observed_drawdown: Decimal | None = None
    maximum_projected_shocked_drawdown: Decimal | None = None
    maximum_reserved_exposure: Decimal | None = None
    maximum_abs_inventory: Decimal | None = None
    maximum_open_paper_orders: int = 0
    preemptive_halt_count: int = 0
    hard_kill_count: int = 0
    fair_play_halt_count: int = 0
    risk_rejection_count: int = 0
    approved_intents_missing_gap_evidence: int = 0
    normal_intents_after_latch: int = 0
    final_open_orders_per_run: tuple[int | None, ...] = ()
    configured_adverse_move: Decimal = CONFIGURED_ADVERSE_MOVE
    guarantee_beyond_configured_gap: bool = False
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class PaperBurnInCampaignFairPlayResult:
    status: str
    reason_counts: dict[str, int]
    reason_ratio_to_intents: Decimal | None = None
    reason_ratio_to_decisions: Decimal | None = None
    affected_runs: int = 0
    maximum_consecutive_rejections: int = 0
    rejection_clustering: bool = False
    fair_play_rejection_count: int = 0
    fair_play_halt_count: int = 0
    enforcement_status: str = "PASS"
    compatibility_status: str = "PASS"
    enforcement_fail_runs: int = 0
    compatibility_fail_runs: int = 0
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class PaperBurnInCampaignSummary:
    input_files: tuple[str, ...]
    runs: tuple[PaperBurnInCampaignRunResult, ...]
    market: PaperBurnInCampaignMarketResult
    execution: PaperBurnInCampaignExecutionResult
    risk: PaperBurnInCampaignRiskResult
    fair_play: PaperBurnInCampaignFairPlayResult
    symbol: str | None = None
    qualifying_run_count: int = 0
    failed_run_count: int = 0
    insufficient_evidence_run_count: int = 0
    total_net_pnl: Decimal | None = None
    total_fees: Decimal | None = None
    mean_fees: Decimal | None = None
    mean_run_pnl: Decimal | None = None
    median_run_pnl: Decimal | None = None
    minimum_run_pnl: Decimal | None = None
    maximum_run_pnl: Decimal | None = None
    positive_run_count: int = 0
    negative_run_count: int = 0
    flat_run_count: int = 0
    mean_drawdown: Decimal | None = None
    maximum_drawdown: Decimal | None = None
    maximum_marked_equity: Decimal | None = None
    privacy_status: str = "FAIL"
    integrity_status: str = "FAIL"
    market_quality_status: str = "FAIL"
    risk_status: str = "FAIL"
    fair_play_status: str = "FAIL"
    live_order_calls: int = 0
    authenticated_calls: int = 0
    rpc_calls: int = 0
    mutation_rpc_calls: int = 0
    journal_writes: int = 0
    signer_calls: int = 0
    submission_calls: int = 0
    real_submission_enabled: bool = False
    authoritative_trading_status_available: bool = False
    usable_for_production_readiness: bool = False
    blockers: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    result: str = "FAIL"

    @property
    def exit_code(self) -> int:
        return {"PASS": 0, "FAIL": 1, "INSUFFICIENT_EVIDENCE": 3, "PASS_WITH_WARNINGS": 4}.get(self.result, 1)

    @property
    def qualifying_runs(self) -> int:
        return self.qualifying_run_count


def _normalize_reason(value: Any) -> str:
    return normalize_fair_play_reason(value).value


def _run_projection(path: Path, analysis: single.PaperBurnInAnalysisSummary, root: str | Path | None) -> PaperBurnInCampaignRunResult:
    records = _records(path)
    try:
        incident = analyze_paper_burn_in_fair_play_incident(path, repository_root=root)
    except (OSError, ValueError):
        incident = None
    times = [_utc(row.get("timestamp")) for row in records]
    times = [value for value in times if value is not None]
    portfolios = [row for row in records if row.get("record_type") == "portfolio_snapshot"]
    start = times[0] if times else None
    end = times[-1] if times else None
    initial = _dec(portfolios[0].get("equity")) if portfolios else None
    summary_row = next((row for row in records if row.get("record_type") == "run_summary"), None)
    declared_result = None
    if summary_row:
        for note in summary_row.get("notes", []) if isinstance(summary_row.get("notes"), list) else []:
            text = str(note)
            if text.startswith("result="):
                declared_result = text.split("=", 1)[1].strip().upper()
                break
    ending = _dec(summary_row.get("equity")) if summary_row else (_dec(portfolios[-1].get("equity")) if portfolios else None)
    pnl = ending - initial if ending is not None and initial is not None else None
    pct = pnl / initial if pnl is not None and initial not in (None, Decimal("0")) else None
    fees = _dec(summary_row.get("fees_paid")) if summary_row else None
    inventories = [_dec(row.get("base_position")) for row in portfolios]
    inventories = [value for value in inventories if value is not None]
    reasons: dict[str, int] = {}
    reject_sequence: list[bool] = []
    fill_timestamps: set[str] = set()
    executed_prices: set[Decimal] = set()
    for row in records:
        if row.get("record_type") != "paper_fill":
            continue
        events = row.get("confirmed_fill_events", []) if isinstance(row.get("confirmed_fill_events"), list) else []
        for event in events:
            if not isinstance(event, Mapping):
                continue
            if event.get("timestamp") is not None:
                fill_timestamps.add(str(event.get("timestamp")))
            price = _dec(event.get("price"))
            if price is not None:
                executed_prices.add(price)
    for row in records:
        if row.get("record_type") != "strategy_intent":
            continue
        for intent in row.get("trade_intent_events", []) if isinstance(row.get("trade_intent_events"), list) else []:
            if not isinstance(intent, Mapping):
                continue
            rejected = intent.get("fair_play_allowed") is False
            reject_sequence.append(rejected)
            if rejected:
                reason = _normalize_reason(intent.get("fair_play_reason"))
                reasons[reason] = reasons.get(reason, 0) + 1
    max_consecutive = 0
    current = 0
    for value in reject_sequence:
        current = current + 1 if value else 0
        max_consecutive = max(max_consecutive, current)
    qualifying = (
        declared_result == "PASS"
        and analysis.result == "PASS"
        and analysis.market_quality.status == "HEALTHY"
        and analysis.market_quality.duration_seconds is not None
        and analysis.market_quality.duration_seconds >= MIN_RUN_DURATION_SECONDS
        and analysis.privacy_status == "PASS"
        and analysis.integrity.status == "PASS"
        and analysis.risk.status == "PASS"
        and analysis.fair_play.status == "PASS"
        and analysis.summary_counters_match is True
        and (incident is None or incident.enforcement == "PASS")
        and (incident is None or incident.strategy_compatibility == "PASS")
        and not (incident is not None and incident.halt_sequence is not None)
    )
    rel = path.resolve().relative_to(Path(root or Path.cwd()).resolve()).as_posix()
    return PaperBurnInCampaignRunResult(
        input_file=rel, analysis=analysis, qualifying=qualifying, declared_result=declared_result,
        run_start=start, run_end=end, initial_equity=initial, ending_equity=ending,
        absolute_pnl=pnl, percentage_pnl=pct, fees=fees,
        ending_inventory=inventories[-1] if inventories else None,
        maximum_abs_inventory=max((abs(value) for value in inventories), default=None),
        fair_play_rejection_reasons=tuple(sorted(reasons.items())),
        fair_play_max_consecutive_rejections=max_consecutive,
        fill_timestamps=tuple(sorted(fill_timestamps)),
        executed_prices=tuple(sorted(executed_prices)),
        configuration_fingerprint=(incident.configuration_fingerprint if incident else None),
        fair_play_incident=incident,
        fair_play_enforcement=(incident.enforcement if incident else "PASS"),
        fair_play_compatibility=(incident.strategy_compatibility if incident else "PASS"),
    )


def _market(runs: Sequence[PaperBurnInCampaignRunResult]) -> PaperBurnInCampaignMarketResult:
    qualifying = [run for run in runs if run.qualifying]
    accepted = sum(run.analysis.market_quality.accepted_snapshots for run in runs)
    rejected = sum(run.analysis.market_quality.rejected_snapshots for run in runs)
    total = accepted + rejected
    gaps = [run.analysis.market_quality.largest_timestamp_gap_seconds for run in runs]
    med_gaps = [run.analysis.market_quality.median_timestamp_gap_seconds for run in runs]
    mins = [run.analysis.market_quality.minimum_spread for run in runs]
    medians = [run.analysis.market_quality.median_spread for run in runs]
    maxes = [run.analysis.market_quality.maximum_spread for run in runs]
    steps = [run.analysis.market_quality.largest_mid_price_step for run in runs]
    statuses = [run.analysis.market_quality.status for run in runs]
    errors: list[str] = []
    if len(qualifying) < MIN_QUALIFYING_RUNS:
        errors.append("qualifying_run_count_below_three")
    if total and Decimal(accepted) / Decimal(total) < MIN_ACCEPTED_RATIO:
        errors.append("accepted_ratio_below_95_percent")
    if accepted < MIN_ACCEPTED_SNAPSHOTS:
        errors.append("accepted_snapshot_count_below_900")
    if any(run.analysis.market_quality.crossed_count for run in runs):
        errors.append("crossed_books_present")
    if any(run.analysis.market_quality.malformed_count for run in runs):
        errors.append("malformed_books_present")
    evidence_errors = {"qualifying_run_count_below_three", "accepted_snapshot_count_below_900"}
    return PaperBurnInCampaignMarketResult(
        status="PASS" if not errors else ("INSUFFICIENT_EVIDENCE" if set(errors).issubset(evidence_errors) else "FAIL"),
        input_runs=len(runs), qualifying_runs=len(qualifying),
        total_duration_seconds=sum((run.analysis.market_quality.duration_seconds or Decimal("0") for run in runs), Decimal("0")),
        total_records=sum(run.analysis.integrity.record_count for run in runs),
        total_accepted_snapshots=accepted, total_rejected_snapshots=rejected,
        accepted_ratio=(Decimal(accepted) / Decimal(total) if total else None),
        stale_count=sum(run.analysis.market_quality.stale_count for run in runs),
        crossed_count=sum(run.analysis.market_quality.crossed_count for run in runs),
        malformed_count=sum(run.analysis.market_quality.malformed_count for run in runs),
        maximum_consecutive_failures=max((run.analysis.market_quality.maximum_consecutive_failures for run in runs), default=0),
        maximum_timestamp_gap_seconds=max((value for value in gaps if value is not None), default=None),
        median_run_median_gap_seconds=median([value for value in med_gaps if value is not None]) if any(value is not None for value in med_gaps) else None,
        minimum_spread=min((value for value in mins if value is not None), default=None),
        median_spread=median([value for value in medians if value is not None]) if any(value is not None for value in medians) else None,
        maximum_spread=max((value for value in maxes if value is not None), default=None),
        maximum_mid_price_step=max((value for value in steps if value is not None), default=None),
        distinct_mid_price_count=sum(run.analysis.market_quality.distinct_mid_prices for run in runs),
        distinct_bid_level_count=sum(run.analysis.market_quality.distinct_best_bids for run in runs),
        distinct_ask_level_count=sum(run.analysis.market_quality.distinct_best_asks for run in runs),
        healthy_runs=statuses.count("HEALTHY"), degraded_runs=statuses.count("DEGRADED"),
        invalid_runs=statuses.count("INVALID"), insufficient_duration_runs=statuses.count("INSUFFICIENT_DURATION"),
        errors=tuple(errors),
    )


def _execution(runs: Sequence[PaperBurnInCampaignRunResult], market: PaperBurnInCampaignMarketResult) -> PaperBurnInCampaignExecutionResult:
    ex = [run.analysis.execution for run in runs]
    fills_per_run = tuple(value.paper_fills for value in ex)
    total_fills = sum(fills_per_run)
    total_intents = sum(value.strategy_intents for value in ex)
    total_orders = sum(value.paper_orders for value in ex)
    durations = market.total_duration_seconds / Decimal("3600") if market.total_duration_seconds else Decimal("0")
    timestamps = set(timestamp for run in runs for timestamp in run.fill_timestamps)
    prices = set(price for run in runs for price in run.executed_prices)
    transitions = sum(value.inventory_transitions for value in ex)
    if total_fills == 0:
        activity = "NO_ACTIVITY"
    elif len([value for value in runs if value.qualifying and value.analysis.execution.paper_fills > 0]) >= 3 and total_fills >= 20 and sum(1 for run in runs if run.analysis.execution.paper_fills > 0) >= 3 and len(prices) >= 3 and transitions >= 4:
        activity = "ADEQUATE_ACTIVITY"
    elif total_fills >= 10 and sum(1 for value in fills_per_run if value > 0) >= 2:
        activity = "LIMITED_ACTIVITY"
    else:
        activity = "SPARSE_ACTIVITY"
    return PaperBurnInCampaignExecutionResult(
        status="PASS" if all(value.status == "PASS" for value in ex) else "FAIL",
        total_strategy_intents=sum(value.strategy_intents for value in ex),
        total_risk_approved_intents=sum(value.risk_approved_intents for value in ex),
        total_risk_rejected_intents=sum(value.risk_rejected_intents for value in ex),
        total_fair_play_approved_intents=sum(value.fair_play_approved_intents for value in ex),
        total_fair_play_rejected_intents=sum(value.fair_play_rejected_intents for value in ex),
        total_paper_orders=total_orders, total_cancels=sum(value.paper_cancels for value in ex),
        total_replacements=sum(value.paper_replacements for value in ex),
        total_partial_fills=sum(value.partial_fills for value in ex),
        total_full_fills=sum(value.full_fills for value in ex), total_fills=total_fills,
        fills_per_run=fills_per_run,
        fills_per_hour=(Decimal(total_fills) / durations if durations else None),
        runs_with_fills=sum(value > 0 for value in fills_per_run),
        distinct_fill_timestamps=len(timestamps), distinct_executed_price_levels=len(prices),
        inventory_transitions=transitions,
        maximum_abs_inventory=max((run.maximum_abs_inventory for run in runs if run.maximum_abs_inventory is not None), default=None),
        final_inventory_per_run=tuple(run.ending_inventory for run in runs),
        intent_to_fill_ratio=(Decimal(total_fills) / Decimal(total_intents) if total_intents else None),
        order_to_fill_ratio=(Decimal(total_fills) / Decimal(total_orders) if total_orders else None),
        strategy_activity=activity,
    )


def _fair(runs: Sequence[PaperBurnInCampaignRunResult], execution: PaperBurnInCampaignExecutionResult) -> PaperBurnInCampaignFairPlayResult:
    counts: dict[str, int] = {}
    for run in runs:
        for reason, count in run.fair_play_rejection_reasons:
            counts[reason] = counts.get(reason, 0) + count
    total = sum(counts.values())
    maximum = max((run.fair_play_max_consecutive_rejections for run in runs), default=0)
    affected = sum(bool(run.fair_play_rejection_reasons) for run in runs)
    warnings: list[str] = []
    if execution.total_strategy_intents and Decimal(total) / Decimal(execution.total_strategy_intents) > Decimal("0.25"):
        warnings.append("fair_play_rejection_ratio_above_25_percent")
    if total and max(counts.values()) / total > 0.8:
        warnings.append("dominant_fair_play_reason_above_80_percent")
    if maximum > 10:
        warnings.append("more_than_10_consecutive_fair_play_rejections")
    if any(run.analysis.fair_play.fair_play_halt for run in runs):
        warnings.append("fair_play_halt_present")
    enforcement_fail_runs = sum(run.fair_play_enforcement != "PASS" for run in runs)
    compatibility_fail_runs = sum(run.fair_play_compatibility != "PASS" for run in runs)
    # A correctly enforced halt is not itself an enforcement failure.  It is a
    # separate strategy-compatibility/non-qualification result.
    errors = ["fair_play_enforcement_failed"] if enforcement_fail_runs else []
    if compatibility_fail_runs:
        warnings.append("strategy_fair_play_compatibility_failed")
    return PaperBurnInCampaignFairPlayResult(
        status="FAIL" if errors else "PASS", reason_counts=counts,
        reason_ratio_to_intents=(Decimal(total) / Decimal(execution.total_strategy_intents) if execution.total_strategy_intents else None),
        reason_ratio_to_decisions=(Decimal(total) / Decimal(execution.total_fair_play_approved_intents + total) if execution.total_fair_play_approved_intents + total else None),
        affected_runs=affected, maximum_consecutive_rejections=maximum,
        rejection_clustering=maximum > 1, fair_play_rejection_count=total,
        fair_play_halt_count=sum(run.analysis.fair_play.fair_play_halt for run in runs),
        enforcement_status="FAIL" if enforcement_fail_runs else "PASS",
        compatibility_status="FAIL" if compatibility_fail_runs else "PASS",
        enforcement_fail_runs=enforcement_fail_runs,
        compatibility_fail_runs=compatibility_fail_runs,
        warnings=tuple(warnings), errors=tuple(errors),
    )


def _risk(runs: Sequence[PaperBurnInCampaignRunResult], repository_root: str | Path | None = None) -> PaperBurnInCampaignRiskResult:
    risks = [run.analysis.risk for run in runs]
    max_dd = max((value.maximum_drawdown for value in risks if value.maximum_drawdown is not None), default=None)
    max_projected = max((value.maximum_projected_shocked_drawdown for value in risks if value.maximum_projected_shocked_drawdown is not None), default=None)
    reserved: list[Decimal] = []
    maximum_orders = 0
    missing_gap = 0
    for run in runs:
        records = _records(_safe_input(run.input_file, repository_root))
        for row in records:
            value = _dec(row.get("reserved_exposure"))
            if value is not None:
                reserved.append(value)
            maximum_orders = max(maximum_orders, int(row.get("open_orders_count", 0) or 0))
            if row.get("record_type") == "strategy_intent":
                events = row.get("trade_intent_events", []) if isinstance(row.get("trade_intent_events"), list) else []
                if row.get("portfolio_risk_allowed") is True and row.get("gap_risk_assumptions_available") is not True:
                    missing_gap += sum(bool(isinstance(event, Mapping) and event.get("execution_approved") is True) for event in events)
    errors: list[str] = []
    if any(value.status != "PASS" for value in risks):
        errors.append("risk_run_failed")
    if max_dd is not None and max_dd >= HARD_DRAWdown_LIMIT:
        errors.append("hard_drawdown_limit_reached")
    if max_projected is not None and max_projected >= HARD_DRAWdown_LIMIT:
        errors.append("projected_drawdown_limit_reached")
    if missing_gap:
        errors.append("approved_intents_missing_gap_evidence")
    if any(value.normal_intents_after_latch for value in risks):
        errors.append("normal_intents_after_latch")
    if any(value.final_open_orders not in (0, None, "0") for value in risks):
        errors.append("open_orders_after_shutdown")
    return PaperBurnInCampaignRiskResult(
        status="FAIL" if errors else "PASS", maximum_observed_drawdown=max_dd,
        maximum_projected_shocked_drawdown=max_projected,
        maximum_reserved_exposure=max(reserved) if reserved else None,
        maximum_abs_inventory=max((run.maximum_abs_inventory for run in runs if run.maximum_abs_inventory is not None), default=None),
        maximum_open_paper_orders=maximum_orders,
        preemptive_halt_count=sum(value.preemptive_halt for value in risks), hard_kill_count=sum(value.hard_kill for value in risks),
        fair_play_halt_count=sum(run.analysis.fair_play.fair_play_halt for run in runs),
        risk_rejection_count=sum(value.risk_rejected_intents for value in risks),
        approved_intents_missing_gap_evidence=missing_gap,
        normal_intents_after_latch=sum(value.normal_intents_after_latch for value in risks),
        final_open_orders_per_run=tuple(value.final_open_orders for value in risks),
        errors=tuple(errors),
    )


def analyze_paper_burn_in_campaign(paths: Sequence[str | Path], *, repository_root: str | Path | None = None) -> PaperBurnInCampaignSummary:
    if not paths:
        raise ValueError("at_least_one_input_required")
    root = Path(repository_root or Path.cwd()).resolve()
    normalized: list[str] = []
    safe_paths: list[Path] = []
    for path in paths:
        safe = _safe_input(path, repository_root)
        rel = safe.relative_to(root).as_posix()
        if rel in normalized:
            raise ValueError("duplicate_input_path")
        normalized.append(rel)
        safe_paths.append(safe)
    analyses = [single.analyze_paper_burn_in(path, repository_root=repository_root) for path in normalized]
    runs = tuple(_run_projection(path, analysis, root) for path, analysis in zip(safe_paths, analyses))
    fingerprints = [run.fingerprint for run in runs if run.fingerprint]
    symbols = {run.symbol for run in runs if run.symbol}
    identity_errors: list[str] = []
    if len(fingerprints) != len(set(fingerprints)):
        identity_errors.append("duplicate_run_fingerprint")
        # The first burn-in implementation used a short filename suffix as a
        # fingerprint.  Preserve those files for audit, but never treat a
        # repeated short identity as independent campaign evidence.
        short_duplicates = {
            fingerprint for fingerprint in fingerprints
            if len(fingerprint) < 32 and all(char in "0123456789abcdef" for char in fingerprint.lower())
        }
        if short_duplicates:
            identity_errors.append("legacy_non_unique_run_fingerprint")
    if any(run.fingerprint is None for run in runs):
        identity_errors.append("missing_run_fingerprint")
    if len(symbols) > 1:
        identity_errors.append("symbol_mismatch")
    duplicate_fingerprints = {
        fingerprint for fingerprint in fingerprints
        if fingerprints.count(fingerprint) > 1
    }
    if duplicate_fingerprints or any(run.fingerprint is None for run in runs):
        runs = tuple(
            replace(
                run,
                qualifying=False
                if run.fingerprint is None or run.fingerprint in duplicate_fingerprints
                else run.qualifying,
            )
            for run in runs
        )
    qualifying = [run for run in runs if run.qualifying]
    windows = [(run.run_start, run.run_end) for run in qualifying if run.run_start and run.run_end]
    for index, (start, end) in enumerate(windows):
        for other_start, other_end in windows[index + 1:]:
            if start < other_end and other_start < end:
                identity_errors.append("overlapping_run_windows")
                identity_errors.append("overlapping_run_window")
    initial_values = {run.initial_equity for run in qualifying if run.initial_equity is not None}
    if len(initial_values) > 1:
        identity_errors.append("initial_equity_policy_mismatch")
    med_intervals = [run.analysis.market_quality.median_timestamp_gap_seconds for run in qualifying if run.analysis.market_quality.median_timestamp_gap_seconds]
    if med_intervals and max(med_intervals) > min(med_intervals) * Decimal("2"):
        identity_errors.append("sampling_policy_incompatible")
    market = _market(runs)
    execution = _execution(runs, market)
    fair = _fair(runs, execution)
    risk = _risk(runs, root)
    for run in runs:
        if run.analysis.integrity.status == "FAIL":
            identity_errors.append("run_integrity_failed")
        if run.analysis.result == "FAIL":
            identity_errors.append("run_analysis_failed")
        if run.result == "FAIL":
            identity_errors.append("run_declared_failure")
        if run.fair_play_incident is not None:
            if run.fair_play_incident.halt_sequence is not None:
                identity_errors.append("fair_play_halt_non_qualifying")
            if run.fair_play_incident.evidence_sufficiency != "SUFFICIENT":
                identity_errors.append("fair_play_evidence_insufficient")
        if run.analysis.privacy_status == "FAIL":
            identity_errors.append("run_privacy_failed")
        if any((run.analysis.live_order_calls, run.analysis.authenticated_calls, run.analysis.rpc_calls, run.analysis.mutation_rpc_calls, run.analysis.journal_writes, run.analysis.signer_calls, run.analysis.submission_calls)) or run.analysis.real_submission_enabled:
            identity_errors.append("unsafe_counter_nonzero")
    pnl_values = [run.absolute_pnl for run in qualifying if run.absolute_pnl is not None]
    fee_values = [run.fees for run in qualifying if run.fees is not None]
    dd_values = [run.analysis.maximum_drawdown for run in qualifying if run.analysis.maximum_drawdown is not None]
    maximum_equity: list[Decimal] = []
    for run in qualifying:
        records = _records(_safe_input(run.input_file, root))
        maximum_equity.extend(value for value in (_dec(row.get("equity")) for row in records if row.get("record_type") == "portfolio_snapshot") if value is not None)
    market_blockers = [error for error in market.errors if error not in {"qualifying_run_count_below_three", "accepted_snapshot_count_below_900"}]
    blockers = list(dict.fromkeys(identity_errors + market_blockers + list(risk.errors) + list(fair.errors)))
    insufficient = len(qualifying) < MIN_QUALIFYING_RUNS or any(run.result == "INSUFFICIENT_EVIDENCE" for run in runs)
    warnings = list(fair.warnings)
    for run in runs:
        if run.fair_play_incident is not None:
            warnings.extend(run.fair_play_incident.warnings)
    if execution.strategy_activity in {"SPARSE_ACTIVITY", "LIMITED_ACTIVITY"}:
        warnings.append("strategy_activity_below_adequate")
    if blockers:
        result = "FAIL"
    elif insufficient:
        result = "INSUFFICIENT_EVIDENCE"
    elif warnings:
        result = "PASS_WITH_WARNINGS"
    else:
        result = "PASS"
    return PaperBurnInCampaignSummary(
        input_files=tuple(normalized), runs=runs, market=market, execution=execution, risk=risk, fair_play=fair,
        symbol=next(iter(symbols), None), qualifying_run_count=len(qualifying),
        failed_run_count=sum(run.result == "FAIL" for run in runs),
        insufficient_evidence_run_count=sum(run.result == "INSUFFICIENT_EVIDENCE" for run in runs),
        total_net_pnl=sum(pnl_values, Decimal("0")) if pnl_values else None,
        total_fees=sum(fee_values, Decimal("0")) if fee_values else None,
        mean_fees=(sum(fee_values, Decimal("0")) / Decimal(len(fee_values)) if fee_values else None),
        mean_run_pnl=(sum(pnl_values, Decimal("0")) / Decimal(len(pnl_values)) if pnl_values else None),
        median_run_pnl=median(pnl_values) if pnl_values else None,
        minimum_run_pnl=min(pnl_values) if pnl_values else None, maximum_run_pnl=max(pnl_values) if pnl_values else None,
        positive_run_count=sum(value > 0 for value in pnl_values), negative_run_count=sum(value < 0 for value in pnl_values), flat_run_count=sum(value == 0 for value in pnl_values),
        mean_drawdown=(sum(dd_values, Decimal("0")) / Decimal(len(dd_values)) if dd_values else None), maximum_drawdown=max(dd_values) if dd_values else None,
        maximum_marked_equity=max(maximum_equity) if maximum_equity else None,
        privacy_status="PASS" if all(run.analysis.privacy_status == "PASS" for run in runs) else "FAIL",
        integrity_status="PASS" if all(run.analysis.integrity.status == "PASS" for run in runs) else "FAIL",
        market_quality_status=market.status, risk_status=risk.status, fair_play_status=fair.status,
        live_order_calls=sum(run.analysis.live_order_calls for run in runs), authenticated_calls=sum(run.analysis.authenticated_calls for run in runs), rpc_calls=sum(run.analysis.rpc_calls for run in runs), mutation_rpc_calls=sum(run.analysis.mutation_rpc_calls for run in runs), journal_writes=sum(run.analysis.journal_writes for run in runs), signer_calls=sum(run.analysis.signer_calls for run in runs), submission_calls=sum(run.analysis.submission_calls for run in runs),
        real_submission_enabled=any(run.analysis.real_submission_enabled for run in runs), blockers=tuple(blockers), warnings=tuple(dict.fromkeys(warnings)), result=result,
    )


def analyze_campaign(paths: Sequence[str | Path], *, repository_root: str | Path | None = None) -> PaperBurnInCampaignSummary:
    return analyze_paper_burn_in_campaign(paths, repository_root=repository_root)
