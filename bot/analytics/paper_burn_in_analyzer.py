"""Offline integrity and safety audit for public paper burn-in JSONL files.

The analyzer deliberately has no market, RPC, authenticated, broker, signer,
or execution imports.  It consumes only a bounded repository-local JSONL file
and reports structural evidence without changing that file or trading state.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import re
from statistics import median
from typing import Any, Iterable, Mapping


ALLOWED_RECORD_TYPES = frozenset({
    "run_start", "market_snapshot", "market_reject", "strategy_intent",
    "risk_decision", "fair_play_decision", "paper_order", "paper_fill",
    "portfolio_snapshot", "halt", "run_summary",
})
MAX_FILE_BYTES = 64 * 1024 * 1024
DECIMAL_FIELDS = frozenset({
    "best_bid", "best_ask", "mid_price", "spread", "exchange_age_seconds",
    "clock_skew_seconds", "unchanged_seconds", "risk_drawdown",
    "risk_max_drawdown", "max_open_order_age_seconds", "signal_spread_bps",
    "signal_bid_depth", "signal_ask_depth", "signal_depth_imbalance",
    "signal_microprice", "signal_microprice_edge_bps",
    "signal_one_step_return_bps", "signal_rolling_momentum_bps",
    "signal_confidence", "depth_imbalance_l1", "depth_imbalance_l2",
    "depth_imbalance_l3", "depth_imbalance_l5", "depth_imbalance_l10",
    "depth_bid_l1", "depth_ask_l1", "depth_bid_l5", "depth_ask_l5",
    "cash_balance", "base_position", "equity", "peak_equity",
    "unrealized_pnl", "drawdown", "total_volume", "weekly_volume",
    "estimated_score", "fees_paid", "reserved_exposure",
    "projected_shocked_drawdown", "preemptive_drawdown",
})
COUNT_FIELDS = frozenset({
    "iteration_index", "consecutive_failures", "evaluated_open_orders_count",
    "orders_at_touch_count", "crossed_order_count",
    "level_quantity_decreased_count", "level_disappeared_count",
    "signal_sample_count", "intents_count", "decisions_count", "fills_count",
    "submitted_orders_count", "open_orders_count", "raffle_tickets",
    "sequence_number",
})
NON_NEGATIVE_FIELDS = frozenset({
    "best_bid", "best_ask", "mid_price", "spread", "exchange_age_seconds",
    "unchanged_seconds", "risk_drawdown", "risk_max_drawdown",
    "max_open_order_age_seconds", "signal_spread_bps", "signal_bid_depth",
    "signal_ask_depth", "signal_microprice", "signal_confidence",
    "depth_bid_l1", "depth_ask_l1", "depth_bid_l5", "depth_ask_l5",
    "cash_balance", "base_position", "equity", "peak_equity",
    "total_volume", "weekly_volume", "estimated_score", "fees_paid",
    "reserved_exposure", "projected_shocked_drawdown", "preemptive_drawdown",
}) | COUNT_FIELDS
SENSITIVE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("url", re.compile(r"https?://", re.IGNORECASE)),
    ("authorization", re.compile(r"authorization", re.IGNORECASE)),
    ("bearer", re.compile(r"\bbearer\b", re.IGNORECASE)),
    ("cookie", re.compile(r"\bcookie\b", re.IGNORECASE)),
    ("private_key", re.compile(r"private[_ -]?key", re.IGNORECASE)),
    ("jwt_token", re.compile(r"jwt[_ -]?token", re.IGNORECASE)),
    ("mnemonic", re.compile(r"mnemonic", re.IGNORECASE)),
    ("seed_phrase", re.compile(r"seed[ _-]?phrase", re.IGNORECASE)),
    ("keystore", re.compile(r"keystore", re.IGNORECASE)),
    ("raw_transaction", re.compile(r"raw[ _-]?transaction", re.IGNORECASE)),
    ("calldata", re.compile(r"calldata", re.IGNORECASE)),
    ("headers", re.compile(r"headers", re.IGNORECASE)),
    ("rpc_payload", re.compile(r"rpc[ _-]?(request|body|payload)(?:\b|_)", re.IGNORECASE)),
    ("evm_address", re.compile(r"0x[0-9a-f]{40}", re.IGNORECASE)),
)


def _dec(value: Any, field: str) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(field)
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(field) from exc
    if not result.is_finite():
        raise ValueError(field)
    return result


def _utc(value: Any) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("timestamp")
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        result = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError("timestamp") from exc
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("timestamp")
    return result.astimezone(timezone.utc)


def _safe_path(path: str | Path, repository_root: str | Path | None) -> Path:
    raw = str(path)
    if "://" in raw or raw.lower().startswith(("http:", "https:")):
        raise ValueError("input_path_must_be_repository_relative")
    root = Path(repository_root or Path.cwd()).resolve()
    candidate_input = Path(raw)
    if candidate_input.is_absolute() and repository_root is None:
        raise ValueError("input_path_must_be_repository_relative")
    if not candidate_input.is_absolute() and ".." in candidate_input.parts:
        raise ValueError("input_path_must_be_repository_relative")
    candidate = candidate_input.resolve(strict=False) if candidate_input.is_absolute() else (root / candidate_input).resolve(strict=False)
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError("input_path_outside_repository") from exc
    if candidate.is_symlink():
        raise ValueError("symlink_input_not_allowed")
    if not candidate.exists() or not candidate.is_file():
        raise FileNotFoundError("input_file_unavailable")
    if candidate.stat().st_size > MAX_FILE_BYTES:
        raise ValueError("input_file_too_large")
    return candidate


def _reject_constant(value: str) -> None:
    raise ValueError(f"non_finite_json:{value}")


def _privacy_categories(raw: str) -> tuple[tuple[str, int], ...]:
    found: list[tuple[str, int]] = []
    lines = raw.splitlines()
    for number, line in enumerate(lines, 1):
        for category, pattern in SENSITIVE_PATTERNS:
            if pattern.search(line):
                found.append((category, number))
    return tuple(found)


def _notes(record: Mapping[str, Any]) -> tuple[str, ...]:
    value = record.get("notes", [])
    if isinstance(value, list):
        return tuple(str(item) for item in value if isinstance(item, (str, int, Decimal)))
    return ()


def _audit_notes(summary: Mapping[str, Any]) -> dict[str, str]:
    values: dict[str, str] = {}
    for note in _notes(summary):
        if note.startswith("audit.") and "=" in note:
            key, value = note[6:].split("=", 1)
            if key and key.replace("_", "").isalnum():
                values[key] = value
    return values


def _event_intents(record: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    values = record.get("trade_intent_events", [])
    if not isinstance(values, list):
        return []
    return [item for item in values if isinstance(item, Mapping)]


def _fill_events(record: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    values = record.get("confirmed_fill_events", [])
    if not isinstance(values, list):
        return []
    return [item for item in values if isinstance(item, Mapping)]


@dataclass(frozen=True)
class PaperBurnInIntegrityResult:
    status: str
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    record_count: int = 0
    meaningful_record_count: int = 0
    run_fingerprint: str | None = None
    symbol: str | None = None
    sequence_valid: bool = False
    timestamps_valid: bool = False


@dataclass(frozen=True)
class PaperBurnInMarketQualityResult:
    status: str
    duration_seconds: Decimal | None = None
    expected_sampling_opportunities: int = 0
    accepted_snapshots: int = 0
    rejected_snapshots: int = 0
    accepted_ratio: Decimal | None = None
    rejected_ratio: Decimal | None = None
    maximum_consecutive_failures: int = 0
    duplicate_snapshots: int = 0
    stale_count: int = 0
    crossed_count: int = 0
    malformed_count: int = 0
    largest_timestamp_gap_seconds: Decimal | None = None
    median_timestamp_gap_seconds: Decimal | None = None
    minimum_spread: Decimal | None = None
    median_spread: Decimal | None = None
    maximum_spread: Decimal | None = None
    largest_mid_price_step: Decimal | None = None
    distinct_mid_prices: int = 0
    distinct_best_bids: int = 0
    distinct_best_asks: int = 0
    book_activity: str = "NO_ACTIVITY"
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class PaperBurnInExecutionResult:
    status: str
    strategy_intents: int = 0
    risk_approved_intents: int = 0
    risk_rejected_intents: int = 0
    fair_play_approved_intents: int = 0
    fair_play_rejected_intents: int = 0
    paper_orders: int = 0
    paper_fills: int = 0
    partial_fills: int = 0
    full_fills: int = 0
    paper_cancels: int = 0
    paper_replacements: int = 0
    halts: int = 0
    inventory_transitions: int = 0
    distinct_executed_prices: int = 0
    final_open_orders: int | None = None
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class PaperBurnInRiskResult:
    status: str
    maximum_drawdown: Decimal | None = None
    hard_drawdown_limit: Decimal | None = None
    preemptive_drawdown: Decimal | None = None
    maximum_projected_shocked_drawdown: Decimal | None = None
    risk_rejected_intents: int = 0
    preemptive_halt: bool = False
    hard_kill: bool = False
    normal_intents_after_latch: int = 0
    final_open_orders: int | None = None
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class PaperBurnInFairPlayResult:
    status: str
    fair_play_rejections: int = 0
    missing_rejection_reasons: int = 0
    rapid_round_trips: int = 0
    near_flat_cycles: int = 0
    excessive_replacements: int = 0
    intent_to_fill_ratio: Decimal | None = None
    fills_without_evidence: int = 0
    fair_play_halt: bool = False
    intents_after_halt: int = 0
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class PaperBurnInAnalysisSummary:
    input_file: str
    integrity: PaperBurnInIntegrityResult
    market_quality: PaperBurnInMarketQualityResult
    execution: PaperBurnInExecutionResult
    risk: PaperBurnInRiskResult
    fair_play: PaperBurnInFairPlayResult
    portfolio_reconstruction: str
    ending_equity_match: bool | None
    summary_counters_match: bool | None
    maximum_drawdown: Decimal | None
    maximum_projected_shocked_drawdown: Decimal | None
    privacy_status: str
    privacy_findings: tuple[tuple[str, int], ...] = ()
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
        if self.result == "PASS":
            return 0
        if self.result == "INSUFFICIENT_EVIDENCE":
            return 3
        return 1


def _validate_records(raw: str) -> tuple[list[dict[str, Any]], PaperBurnInIntegrityResult]:
    records: list[dict[str, Any]] = []
    errors: list[str] = []
    warnings: list[str] = []
    for line_number, line in enumerate(raw.splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line, parse_constant=_reject_constant)
        except (ValueError, json.JSONDecodeError):
            errors.append(f"line_{line_number}_invalid_json")
            continue
        if not isinstance(value, dict):
            errors.append(f"line_{line_number}_record_not_object")
            continue
        record_type = value.get("record_type")
        if record_type not in ALLOWED_RECORD_TYPES:
            errors.append(f"line_{line_number}_unknown_record_type")
        for field_name in DECIMAL_FIELDS:
            if field_name in value and value[field_name] is not None:
                try:
                    parsed = _dec(value[field_name], field_name)
                    if parsed is not None and parsed < 0 and field_name in NON_NEGATIVE_FIELDS:
                        errors.append(field_name)
                except ValueError:
                    errors.append(field_name)
        for field_name in COUNT_FIELDS:
            if field_name in value:
                number = value[field_name]
                if isinstance(number, bool) or not isinstance(number, int) or number < 0:
                    errors.append(field_name)
        if "timestamp" not in value or "symbol" not in value or "record_type" not in value:
            errors.append(f"line_{line_number}_required_field")
        records.append(value)
    if not records:
        errors.append("no_records")
    meaningful = [record for record in records if record.get("record_type") in ALLOWED_RECORD_TYPES]
    run_starts = [record for record in meaningful if record.get("record_type") == "run_start"]
    summaries = [record for record in meaningful if record.get("record_type") == "run_summary"]
    if len(run_starts) != 1:
        errors.append("run_start_count")
    if len(summaries) != 1:
        errors.append("run_summary_count")
    if meaningful and meaningful[0].get("record_type") != "run_start":
        errors.append("run_start_not_first")
    if meaningful and meaningful[-1].get("record_type") != "run_summary":
        errors.append("run_summary_not_last")
    for index, record in enumerate(meaningful):
        if record.get("record_type") == "run_summary" and index != len(meaningful) - 1:
            errors.append("records_after_summary")
        if record.get("record_type") == "halt" and any(
            later.get("record_type") not in {"run_summary"} for later in meaningful[index + 1:]
        ):
            errors.append("records_after_halt")
    timestamps: list[datetime] = []
    for record in meaningful:
        try:
            timestamps.append(_utc(record.get("timestamp")))
        except ValueError:
            errors.append("timestamp")
    timestamps_valid = len(timestamps) == len(meaningful) and all(
        current >= previous for previous, current in zip(timestamps, timestamps[1:])
    )
    if timestamps and not timestamps_valid:
        errors.append("timestamps_not_monotonic")
    sequences: list[int] = []
    missing_sequence = False
    for physical, record in enumerate(meaningful, 1):
        value = record.get("sequence_number")
        if value is None:
            missing_sequence = True
            sequences.append(physical)
        elif isinstance(value, int) and not isinstance(value, bool):
            sequences.append(value)
        else:
            errors.append("sequence_number")
    sequence_valid = len(sequences) == len(set(sequences)) and all(
        current > previous for previous, current in zip(sequences, sequences[1:])
    )
    if not sequence_valid:
        errors.append("sequence_not_strictly_increasing")
    if missing_sequence:
        warnings.append("sequence_number_unavailable_legacy_physical_order_used")
    symbols = {str(record.get("symbol")) for record in meaningful if record.get("symbol") is not None}
    if len(symbols) != 1:
        errors.append("symbol_mismatch")
    fingerprints = {str(record.get("run_fingerprint")) for record in meaningful if record.get("run_fingerprint")}
    if len(fingerprints) > 1:
        errors.append("run_fingerprint_mismatch")
    if not fingerprints:
        warnings.append("run_fingerprint_unavailable")
    status = "PASS" if not errors else "FAIL"
    return records, PaperBurnInIntegrityResult(
        status=status,
        errors=tuple(dict.fromkeys(errors)),
        warnings=tuple(dict.fromkeys(warnings)),
        record_count=len(records),
        meaningful_record_count=len(meaningful),
        run_fingerprint=next(iter(fingerprints), None),
        symbol=next(iter(symbols), None),
        sequence_valid=sequence_valid,
        timestamps_valid=timestamps_valid,
    )


def _market_quality(records: list[Mapping[str, Any]]) -> PaperBurnInMarketQualityResult:
    snapshots = [record for record in records if record.get("record_type") == "market_snapshot"]
    rejects = [record for record in records if record.get("record_type") == "market_reject"]
    accepted = len(snapshots)
    rejected = len(rejects)
    total = accepted + rejected
    notes = [note.lower() for record in rejects for note in _notes(record)]
    stale = sum(any(token in note for token in ("stale", "freshness")) for note in notes)
    crossed = sum("cross" in note for note in notes)
    malformed = sum(any(token in note for token in ("malformed", "missing", "non_positive")) for note in notes)
    timestamps: list[datetime] = []
    for record in snapshots:
        try:
            timestamps.append(_utc(record.get("timestamp")))
        except ValueError:
            pass
    gaps = [Decimal(str((current - previous).total_seconds())) for previous, current in zip(timestamps, timestamps[1:])]
    positive_gaps = [gap for gap in gaps if gap >= 0]
    def _values(field: str) -> list[Decimal]:
        values: list[Decimal] = []
        for record in snapshots:
            try:
                value = _dec(record.get(field), field)
            except ValueError:
                continue
            if value is not None:
                values.append(value)
        return values
    spreads = _values("spread")
    mids = _values("mid_price")
    bids = _values("best_bid")
    asks = _values("best_ask")
    steps = [abs(current - previous) / previous for previous, current in zip(mids, mids[1:]) if previous > 0]
    duplicate = sum(
        current == previous for previous, current in zip(
            [(record.get("mid_price"), record.get("best_bid"), record.get("best_ask")) for record in snapshots],
            [(record.get("mid_price"), record.get("best_bid"), record.get("best_ask")) for record in snapshots][1:],
        )
    )
    max_consecutive = 0
    current_failures = 0
    for record in records:
        if record.get("record_type") == "market_reject":
            current_failures += 1
            max_consecutive = max(max_consecutive, current_failures)
        elif record.get("record_type") == "market_snapshot":
            current_failures = 0
    duration = Decimal(str((timestamps[-1] - timestamps[0]).total_seconds())) if len(timestamps) >= 2 else Decimal("0")
    inferred_interval = median(positive_gaps) if positive_gaps else Decimal("0")
    expected = int((duration / inferred_interval).to_integral_value(rounding="ROUND_FLOOR")) + 1 if inferred_interval > 0 else max(total, 1)
    ratio = Decimal(accepted) / Decimal(total) if total else None
    warnings: list[str] = []
    if accepted < 2:
        warnings.append("insufficient_market_activity")
    if inferred_interval > 0 and duration < Decimal("1500"):
        warnings.append("duration_below_25_minutes")
    if accepted and len(mids) < accepted:
        warnings.append("mid_price_evidence_incomplete")
    errors: list[str] = []
    if crossed or malformed:
        errors.append("invalid_market_records")
    if ratio is not None and ratio < Decimal("0.95"):
        errors.append("accepted_snapshot_ratio_below_95_percent")
    if positive_gaps and inferred_interval > 0 and max(positive_gaps) > inferred_interval * Decimal("3"):
        warnings.append("timestamp_gap_exceeds_three_sampling_intervals")
    if duration < Decimal("1500"):
        status = "INSUFFICIENT_DURATION" if not errors else "INVALID"
    elif errors:
        status = "INVALID"
    elif warnings:
        status = "DEGRADED"
    else:
        status = "HEALTHY"
    activity = "NO_ACTIVITY" if not snapshots else ("ADEQUATE_ACTIVITY" if len(snapshots) >= 10 else "SPARSE_ACTIVITY")
    return PaperBurnInMarketQualityResult(
        status=status, duration_seconds=duration, expected_sampling_opportunities=expected,
        accepted_snapshots=accepted, rejected_snapshots=rejected,
        accepted_ratio=ratio, rejected_ratio=(Decimal(rejected) / Decimal(total) if total else None),
        maximum_consecutive_failures=max_consecutive, duplicate_snapshots=duplicate,
        stale_count=stale, crossed_count=crossed, malformed_count=malformed,
        largest_timestamp_gap_seconds=max(positive_gaps) if positive_gaps else None,
        median_timestamp_gap_seconds=median(positive_gaps) if positive_gaps else None,
        minimum_spread=min(spreads) if spreads else None,
        median_spread=median(spreads) if spreads else None,
        maximum_spread=max(spreads) if spreads else None,
        largest_mid_price_step=max(steps) if steps else None,
        distinct_mid_prices=len(set(mids)), distinct_best_bids=len(set(value for value in bids if value is not None)),
        distinct_best_asks=len(set(value for value in asks if value is not None)),
        book_activity=activity, errors=tuple(errors), warnings=tuple(warnings),
    )


def _portfolio(records: list[Mapping[str, Any]]) -> tuple[str, bool | None, Decimal | None, tuple[str, ...], tuple[str, ...], int]:
    snapshots = [record for record in records if record.get("record_type") == "portfolio_snapshot"]
    summary = next((record for record in records if record.get("record_type") == "run_summary"), None)
    if not snapshots:
        return "INSUFFICIENT_RECORDED_EVIDENCE", None, None, (), ("portfolio_snapshots_missing",), 0
    errors: list[str] = []
    warnings: list[str] = []
    previous_peak: Decimal | None = None
    previous_fees: Decimal | None = None
    previous_inventory: Decimal | None = None
    transitions = 0
    max_drawdown: Decimal | None = None
    for record in snapshots:
        cash = _dec(record.get("cash_balance"), "cash_balance")
        inventory = _dec(record.get("base_position"), "base_position")
        equity = _dec(record.get("equity"), "equity")
        mark = _dec(record.get("mid_price"), "mid_price")
        peak = _dec(record.get("peak_equity"), "peak_equity")
        drawdown = _dec(record.get("drawdown"), "drawdown")
        if peak is None and equity is not None:
            peak = max(previous_peak or equity, equity)
            warnings.append("peak_equity_reconstructed_from_equity_history")
        if None in (cash, inventory, equity, mark, peak, drawdown):
            warnings.append("portfolio_fields_incomplete")
            continue
        expected_equity = cash + inventory * mark
        # The recorder stores the public mid while the portfolio may be marked
        # at the executable fill/mark price.  Use a bounded relative tolerance
        # for that documented mark-source difference, while still rejecting
        # material inconsistencies.
        equity_tolerance = max(Decimal("1e-8"), abs(equity) * Decimal("0.0001"))
        if abs(expected_equity - equity) > equity_tolerance:
            errors.append("equity_mismatch")
        expected_drawdown = (peak - equity) / peak if peak > 0 and equity < peak else Decimal("0")
        if drawdown < 0 or abs(expected_drawdown - drawdown) > Decimal("1e-8"):
            errors.append("drawdown_mismatch")
        if previous_peak is not None and peak < previous_peak:
            errors.append("peak_equity_decreased")
        fees = _dec(record.get("fees_paid"), "fees_paid")
        if fees is not None and previous_fees is not None and fees < previous_fees:
            errors.append("cumulative_fees_decreased")
        if previous_inventory is not None and inventory != previous_inventory:
            transitions += 1
        previous_peak, previous_fees, previous_inventory = peak, fees, inventory
        max_drawdown = drawdown if max_drawdown is None else max(max_drawdown, drawdown)
    ending_match: bool | None = None
    if summary is not None:
        last = snapshots[-1]
        economic_match = all(
            summary.get(field) == last.get(field)
            for field in ("cash_balance", "base_position", "equity", "drawdown")
        )
        summary_open = summary.get("open_orders_count")
        last_open = last.get("open_orders_count")
        # Shutdown intentionally cancels the last open paper order after the
        # final portfolio snapshot.  A transition from positive to zero is
        # therefore an independently valid ending state.
        open_match = summary_open == last_open or summary_open in (0, "0")
        ending_match = economic_match and open_match
        if not ending_match:
            errors.append("summary_ending_state_mismatch")
    else:
        warnings.append("run_summary_missing")
    return ("FAIL" if errors else "PASS"), ending_match, max_drawdown, tuple(dict.fromkeys(errors)), tuple(dict.fromkeys(warnings)), transitions


def _execution_and_fair_play(records: list[Mapping[str, Any]]) -> tuple[PaperBurnInExecutionResult, PaperBurnInFairPlayResult]:
    strategy = [record for record in records if record.get("record_type") == "strategy_intent"]
    orders = [record for record in records if record.get("record_type") == "paper_order"]
    fills = [record for record in records if record.get("record_type") == "paper_fill"]
    halts = [record for record in records if record.get("record_type") == "halt"]
    intents = [intent for record in strategy for intent in _event_intents(record)]
    risk_approved = sum(intent.get("execution_approved") is True for intent in intents)
    risk_rejected = sum(intent.get("execution_approved") is False for intent in intents)
    fair_allowed = sum(intent.get("fair_play_allowed") is True for intent in intents)
    fair_rejected = sum(intent.get("fair_play_allowed") is False for intent in intents)
    missing_reasons = sum(
        intent.get("fair_play_allowed") is False and not str(intent.get("fair_play_reason") or "").strip()
        for intent in intents
    )
    fill_events = [event for record in fills for event in _fill_events(record)]
    rapid = sum(bool(event.get("short_window_round_trip")) for event in fill_events)
    near_flat = max((int(event.get("near_flat_cycle_count", 0) or 0) for event in fill_events), default=0)
    prices = {_dec(event.get("price"), "price") for event in fill_events if _dec(event.get("price"), "price") is not None}
    partial = sum(bool(event.get("partial") or event.get("remaining_quantity") is not None) for event in fill_events)
    full = len(fill_events) - partial
    submitted_by_iteration: dict[int, int] = {}
    post_open_by_iteration: dict[int, int] = {}
    for record in orders:
        index = int(record.get("iteration_index", 0) or 0)
        submitted_by_iteration[index] = submitted_by_iteration.get(index, 0) + int(record.get("submitted_orders_count", 0) or 0)
    for record in records:
        if record.get("record_type") == "portfolio_snapshot":
            index = int(record.get("iteration_index", 0) or 0)
            post_open_by_iteration[index] = int(record.get("open_orders_count", 0) or 0)
    replacements = 0
    previous_open = 0
    paper_orders_count = 0
    for index in sorted(set(submitted_by_iteration) | set(post_open_by_iteration)):
        submitted = submitted_by_iteration.get(index, 0)
        paper_orders_count += submitted
        if submitted and previous_open:
            replacements += 1
        previous_open = post_open_by_iteration.get(index, previous_open)
    final_open = next((record.get("open_orders_count") for record in reversed(records) if record.get("record_type") == "run_summary"), None)
    cancels = int(bool(orders and final_open == 0 and orders[-1].get("open_orders_count", 0)))
    fills_without_evidence = sum(
        1 for record in fills if _fill_events(record) and int(record.get("evaluated_open_orders_count", 0) or 0) == 0
    )
    fair_latched = any(record.get("fair_play_latched") is True for record in records)
    latch_seen = False
    intents_after_halt = 0
    for record in records:
        if record.get("fair_play_latched") is True:
            latch_seen = True
        if latch_seen and record.get("record_type") == "strategy_intent":
            intents_after_halt += len(_event_intents(record))
    errors: list[str] = []
    warnings: list[str] = []
    if missing_reasons:
        errors.append("fair_play_rejection_reason_missing")
    if any(intent.get("fair_play_allowed") is False and intent.get("submitted") is True for intent in intents):
        errors.append("fair_play_rejected_intent_submitted")
    if intents_after_halt:
        errors.append("intents_after_fair_play_halt")
    if fills_without_evidence:
        warnings.append("fills_without_recorded_book_evidence")
    if replacements > max(10, len(intents) * 9 // 10):
        warnings.append("excessive_cancel_replace_activity")
    fair_status = "FAIL" if errors else ("INSUFFICIENT_EVIDENCE" if fills_without_evidence else "PASS")
    execution_status = "PASS" if final_open in (0, "0", None) and not errors else ("FAIL" if errors else "INSUFFICIENT_EVIDENCE")
    execution = PaperBurnInExecutionResult(
        status=execution_status, strategy_intents=len(intents), risk_approved_intents=risk_approved,
        risk_rejected_intents=risk_rejected, fair_play_approved_intents=fair_allowed,
        fair_play_rejected_intents=fair_rejected, paper_orders=paper_orders_count, paper_fills=len(fill_events),
        partial_fills=partial, full_fills=full, paper_cancels=cancels, paper_replacements=replacements,
        halts=len(halts), inventory_transitions=0, distinct_executed_prices=len(prices), final_open_orders=final_open,
        errors=tuple(errors), warnings=tuple(warnings),
    )
    fair = PaperBurnInFairPlayResult(
        status=fair_status, fair_play_rejections=fair_rejected, missing_rejection_reasons=missing_reasons,
        rapid_round_trips=rapid, near_flat_cycles=near_flat, excessive_replacements=replacements,
        intent_to_fill_ratio=(Decimal(len(fill_events)) / Decimal(len(intents)) if intents else None),
        fills_without_evidence=fills_without_evidence, fair_play_halt=fair_latched,
        intents_after_halt=intents_after_halt, errors=tuple(errors), warnings=tuple(warnings),
    )
    return execution, fair


def _risk(records: list[Mapping[str, Any]], execution: PaperBurnInExecutionResult) -> PaperBurnInRiskResult:
    snapshots = [record for record in records if record.get("record_type") == "portfolio_snapshot"]
    risk_records = [record for record in records if record.get("portfolio_risk_allowed") is not None]
    if not snapshots or not risk_records:
        return PaperBurnInRiskResult(status="INSUFFICIENT_EVIDENCE", warnings=("risk_fields_missing",))
    drawdowns = [_dec(record.get("drawdown"), "drawdown") for record in snapshots]
    drawdowns = [value for value in drawdowns if value is not None]
    max_dd = max(drawdowns) if drawdowns else None
    hard = next((_dec(record.get("risk_max_drawdown"), "risk_max_drawdown") for record in risk_records if record.get("risk_max_drawdown") is not None), None)
    projected_values = [_dec(record.get("projected_shocked_drawdown"), "projected_shocked_drawdown") for record in risk_records]
    projected_values = [value for value in projected_values if value is not None]
    preemptive = None
    preemptive_limit = None
    for record in risk_records:
        if record.get("preemptive_halt_latched") is True:
            preemptive = _dec(record.get("risk_drawdown"), "risk_drawdown")
            preemptive_limit = _dec(record.get("preemptive_drawdown"), "preemptive_drawdown")
            break
    hard_latched = any(record.get("hard_kill_latched") is True or record.get("portfolio_risk_latched") is True for record in risk_records)
    errors: list[str] = []
    warnings: list[str] = []
    if hard is not None and max_dd is not None and max_dd >= hard:
        errors.append("hard_drawdown_limit_reached")
    if hard is not None and projected_values and max(projected_values) >= hard:
        errors.append("projected_drawdown_limit_reached")
    latch_seen = False
    after_latch = 0
    for record in records:
        if record.get("portfolio_risk_latched") is True or record.get("hard_kill_latched") is True:
            latch_seen = True
        if latch_seen and record.get("record_type") == "strategy_intent":
            after_latch += len(_event_intents(record))
    if after_latch:
        errors.append("normal_intents_after_risk_latch")
    if execution.final_open_orders not in (0, "0", None):
        errors.append("open_orders_after_shutdown")
    status = "FAIL" if errors else "PASS"
    return PaperBurnInRiskResult(
        status=status, maximum_drawdown=max_dd, hard_drawdown_limit=hard,
        preemptive_drawdown=preemptive_limit,
        maximum_projected_shocked_drawdown=max(projected_values) if projected_values else None,
        risk_rejected_intents=execution.risk_rejected_intents, preemptive_halt=preemptive is not None,
        hard_kill=hard_latched, normal_intents_after_latch=after_latch,
        final_open_orders=execution.final_open_orders, errors=tuple(errors), warnings=tuple(warnings),
    )


def _compare_summary(records: list[Mapping[str, Any]], execution: PaperBurnInExecutionResult, market: PaperBurnInMarketQualityResult, risk: PaperBurnInRiskResult) -> tuple[bool | None, tuple[str, ...]]:
    summary = next((record for record in records if record.get("record_type") == "run_summary"), None)
    if summary is None:
        return None, ("run_summary_missing",)
    expected = _audit_notes(summary)
    if not expected:
        return None, ("summary_counters_unavailable",)
    actual = {
        "market_snapshots": market.accepted_snapshots + market.rejected_snapshots,
        "accepted_snapshots": market.accepted_snapshots,
        "rejected_snapshots": market.rejected_snapshots,
        "stale_snapshots": market.stale_count,
        "crossed_books": market.crossed_count,
        "malformed_snapshots": market.malformed_count,
        "strategy_intents": execution.strategy_intents,
        "risk_approved_intents": execution.risk_approved_intents,
        "risk_rejected_intents": execution.risk_rejected_intents,
        "fair_play_rejected_intents": execution.fair_play_rejected_intents,
        "paper_orders_created": execution.paper_orders,
        "paper_replacements": execution.paper_replacements,
        "partial_fills": execution.partial_fills,
        "full_fills": execution.full_fills,
        "open_orders_after_shutdown": execution.final_open_orders,
    }
    mismatches = [key for key, value in actual.items() if key in expected and str(value) != expected[key]]
    return not mismatches, tuple(f"summary_counter_{key}_mismatch" for key in mismatches)


def analyze_paper_burn_in(path: str | Path, *, repository_root: str | Path | None = None) -> PaperBurnInAnalysisSummary:
    safe = _safe_path(path, repository_root)
    try:
        raw = safe.read_bytes().decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("input_file_not_utf8") from exc
    findings = _privacy_categories(raw)
    records, integrity = _validate_records(raw)
    market = _market_quality(records)
    portfolio_status, ending_match, portfolio_max_dd, portfolio_errors, portfolio_warnings, transitions = _portfolio(records)
    execution, fair = _execution_and_fair_play(records)
    execution = PaperBurnInExecutionResult(**{**execution.__dict__, "inventory_transitions": transitions})
    risk = _risk(records, execution)
    summary_match, summary_errors = _compare_summary(records, execution, market, risk)
    summary = next((record for record in records if record.get("record_type") == "run_summary"), {})
    metric_fields = ("live_order_calls", "authenticated_http_requests", "read_only_rpc_requests", "mutation_rpc_calls", "production_journal_writes", "signer_calls", "submission_calls")
    audit_values = _audit_notes(summary)
    counts = {field: int(audit_values.get(field, "0")) for field in metric_fields}
    blockers: list[str] = []
    warnings = list(integrity.warnings) + list(market.warnings) + list(portfolio_warnings) + list(execution.warnings) + list(fair.warnings) + list(risk.warnings)
    errors = list(integrity.errors) + list(market.errors) + list(portfolio_errors) + list(execution.errors) + list(fair.errors) + list(risk.errors) + list(summary_errors)
    if findings:
        blockers.append("privacy_scan_failed")
    if integrity.status != "PASS":
        blockers.append("integrity_failed")
    if portfolio_status == "FAIL":
        blockers.append("portfolio_reconstruction_failed")
    if risk.status == "FAIL":
        blockers.append("risk_audit_failed")
    if fair.status == "FAIL":
        blockers.append("fair_play_audit_failed")
    if market.status == "INVALID":
        blockers.append("market_quality_invalid")
    if summary_match is False:
        blockers.append("summary_consistency_failed")
    if any(counts.values()):
        blockers.append("live_or_mutation_counter_nonzero")
    insufficient = portfolio_status == "INSUFFICIENT_RECORDED_EVIDENCE" or risk.status == "INSUFFICIENT_EVIDENCE" or fair.status == "INSUFFICIENT_EVIDENCE" or summary_match is None
    result = "FAIL" if blockers else ("INSUFFICIENT_EVIDENCE" if insufficient else "PASS")
    return PaperBurnInAnalysisSummary(
        input_file=_relative_safe_path(safe, repository_root), integrity=integrity, market_quality=market,
        execution=execution, risk=risk, fair_play=fair, portfolio_reconstruction=portfolio_status,
        ending_equity_match=ending_match, summary_counters_match=summary_match,
        maximum_drawdown=portfolio_max_dd, maximum_projected_shocked_drawdown=risk.maximum_projected_shocked_drawdown,
        privacy_status="FAIL" if findings else "PASS", privacy_findings=findings,
        live_order_calls=counts["live_order_calls"], authenticated_calls=counts["authenticated_http_requests"],
        rpc_calls=counts["read_only_rpc_requests"], mutation_rpc_calls=counts["mutation_rpc_calls"],
        journal_writes=counts["production_journal_writes"], signer_calls=counts["signer_calls"],
        submission_calls=counts["submission_calls"], blockers=tuple(dict.fromkeys(blockers)),
        warnings=tuple(dict.fromkeys(warnings + errors)), result=result,
    )


def _relative_safe_path(path: Path, repository_root: str | Path | None) -> str:
    root = Path(repository_root or Path.cwd()).resolve()
    try:
        return path.resolve().relative_to(root).as_posix()
    except ValueError:
        return path.name


def analyze_file(path: str | Path, *, repository_root: str | Path | None = None) -> PaperBurnInAnalysisSummary:
    return analyze_paper_burn_in(path, repository_root=repository_root)


__all__ = [
    "ALLOWED_RECORD_TYPES", "MAX_FILE_BYTES", "PaperBurnInIntegrityResult",
    "PaperBurnInMarketQualityResult", "PaperBurnInExecutionResult",
    "PaperBurnInRiskResult", "PaperBurnInFairPlayResult",
    "PaperBurnInAnalysisSummary", "analyze_paper_burn_in", "analyze_file",
]
