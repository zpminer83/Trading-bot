"""Offline reconstruction of fair-play incidents from paper-run JSONL.

The incident analyzer is deliberately independent from the live guard.  It
only reads bounded, already-recorded decision telemetry and never makes a
network call or changes trading state.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Mapping, Sequence


class PaperBurnInFairPlayReason(str, Enum):
    RAPID_ROUND_TRIP = "rapid_round_trip"
    REPEATED_NEAR_FLAT_CYCLE = "repeated_near_flat_cycle"
    EXCESSIVE_CANCEL_REPLACE = "excessive_cancel_replace"
    REPETITIVE_SAME_PRICE_INTENT = "repetitive_same_price_intent"
    INTENT_FILL_RATIO = "intent_fill_ratio"
    INSUFFICIENT_FILL_EVIDENCE = "insufficient_fill_evidence"
    ARTIFICIAL_VOLUME_PATTERN = "artificial_volume_pattern"
    UNDISCLOSED_FILTER_DECISION = "undisclosed_filter_decision"
    CONSECUTIVE_REJECTION_LIMIT = "consecutive_rejection_limit"
    UNKNOWN_EXPLICIT_REASON = "unknown_explicit_reason"


FAIR_PLAY_REASON_VALUES = frozenset(item.value for item in PaperBurnInFairPlayReason)

# Explicit codes emitted by the current FairPlayGuard.  Cooldown is a local
# pre-trade rejection, not proof of a round trip, so it is retained as an
# unknown explicit code rather than being mislabeled.
FAIR_PLAY_REASON_MAP: dict[str, PaperBurnInFairPlayReason] = {
    "short_window_round_trip": PaperBurnInFairPlayReason.RAPID_ROUND_TRIP,
    "rapid_round_trip": PaperBurnInFairPlayReason.RAPID_ROUND_TRIP,
    "near_flat_cycle_limit": PaperBurnInFairPlayReason.REPEATED_NEAR_FLAT_CYCLE,
    "repeated_near_flat_cycle": PaperBurnInFairPlayReason.REPEATED_NEAR_FLAT_CYCLE,
    "excessive_cancel_replace": PaperBurnInFairPlayReason.EXCESSIVE_CANCEL_REPLACE,
    "repetitive_same_price_intent": PaperBurnInFairPlayReason.REPETITIVE_SAME_PRICE_INTENT,
    "intent_fill_ratio": PaperBurnInFairPlayReason.INTENT_FILL_RATIO,
    "insufficient_fill_evidence": PaperBurnInFairPlayReason.INSUFFICIENT_FILL_EVIDENCE,
    "artificial_volume_pattern": PaperBurnInFairPlayReason.ARTIFICIAL_VOLUME_PATTERN,
    "undisclosed_filter_decision": PaperBurnInFairPlayReason.UNDISCLOSED_FILTER_DECISION,
    "consecutive_rejection_limit": PaperBurnInFairPlayReason.CONSECUTIVE_REJECTION_LIMIT,
    "opposite_side_cooldown": PaperBurnInFairPlayReason.UNKNOWN_EXPLICIT_REASON,
    "unsupported_side": PaperBurnInFairPlayReason.UNKNOWN_EXPLICIT_REASON,
    "ok": PaperBurnInFairPlayReason.UNKNOWN_EXPLICIT_REASON,
}


def normalize_fair_play_reason(value: Any) -> PaperBurnInFairPlayReason:
    """Map a known explicit code to the stable, bounded taxonomy."""
    code = str(value or "").strip().lower()
    if code in FAIR_PLAY_REASON_MAP:
        return FAIR_PLAY_REASON_MAP[code]
    return PaperBurnInFairPlayReason.UNKNOWN_EXPLICIT_REASON


def safe_reason_code(value: Any) -> str:
    """Return a non-sensitive code category, never an arbitrary payload."""
    code = str(value or "").strip().lower()
    if code in FAIR_PLAY_REASON_MAP:
        return code
    if not code:
        return "missing"
    if re.fullmatch(r"[a-z0-9][a-z0-9_.:-]{0,79}", code):
        return f"unknown_code:{code[:32]}"
    return "unknown_code_hash:" + hashlib.sha256(code.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class FairPlayRejectionEvidence:
    sequence_number: int
    timestamp: datetime | None
    normalized_reason: PaperBurnInFairPlayReason
    reason_code: str
    intent_id: str | int | None = None
    order_id: str | int | None = None
    inventory: Decimal | None = None
    consecutive_rejections: int = 0


@dataclass(frozen=True)
class PaperBurnInFairPlayIncidentAnalysis:
    input_file: str
    network_access_used: bool
    integrity: str
    symbol: str | None
    run_fingerprint: str | None
    configuration_fingerprint: str | None
    first_rejection_sequence: int | None
    last_rejection_sequence: int | None
    halt_sequence: int | None
    first_rejection_timestamp: datetime | None
    last_rejection_timestamp: datetime | None
    halt_timestamp: datetime | None
    rejection_count: int
    maximum_consecutive_rejections: int
    normalized_reasons: tuple[tuple[str, int], ...]
    reason_code_counts: tuple[tuple[str, int], ...]
    dominant_reason: str | None
    dominant_reason_count: int
    halt_trigger: str | None
    halt_threshold: Decimal | None
    observed_trigger_value: Decimal | None
    paper_orders_open_before_halt: int | None
    paper_orders_cancelled_by_halt: int | None
    rejected_intents_creating_orders: int
    rejected_intents_creating_fills: int
    normal_intents_after_halt: int
    fills_before_halt: int
    ending_inventory: Decimal | None
    ending_open_orders: int | None
    enforcement: str
    strategy_compatibility: str
    evidence_sufficiency: str
    privacy_status: str
    missing_fields: tuple[str, ...] = ()
    blockers: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    rejection_events: tuple[FairPlayRejectionEvidence, ...] = ()
    consecutive_rejection_streaks: tuple[int, ...] = ()
    records_after_halt: int = 0
    result: str = "FAIL"

    @property
    def fair_play_enforcement(self) -> str:
        return self.enforcement

    @property
    def strategy_fair_play_compatibility(self) -> str:
        return self.strategy_compatibility

    @property
    def exit_code(self) -> int:
        return {"PASS": 0, "INSUFFICIENT_RECORDED_EVIDENCE": 3}.get(self.result, 1)


_SENSITIVE_PATTERNS = (
    re.compile(r"https?://", re.I),
    re.compile(r"authorization|bearer|cookie|\btoken\b|jwt|private[_ -]?key|seed|mnemonic|keystore", re.I),
    re.compile(r"0x[0-9a-f]{40}", re.I),
)


def _dec(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _timestamp(value: Any) -> datetime | None:
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


def _safe_path(path: str | Path, repository_root: str | Path | None) -> Path:
    root = Path(repository_root or Path.cwd()).resolve()
    raw = Path(str(path))
    if raw.is_absolute() and repository_root is None:
        raise ValueError("input_path_must_be_repository_relative")
    if not raw.is_absolute() and ".." in raw.parts:
        raise ValueError("input_path_must_be_repository_relative")
    candidate = (raw if raw.is_absolute() else root / raw).resolve(strict=False)
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError("input_path_outside_repository") from exc
    if candidate.is_symlink() or not candidate.exists() or not candidate.is_file():
        raise ValueError("input_file_unavailable")
    if candidate.stat().st_size > 16 * 1024 * 1024:
        raise ValueError("input_file_too_large")
    return candidate


def _load(path: Path) -> tuple[list[dict[str, Any]], list[str], str]:
    raw = path.read_text(encoding="utf-8")
    privacy = [pattern for pattern in _SENSITIVE_PATTERNS if pattern.search(raw)]
    errors: list[str] = []
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(raw.splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except (ValueError, json.JSONDecodeError):
            errors.append(f"line_{line_number}_invalid_json")
            continue
        if not isinstance(value, dict):
            errors.append(f"line_{line_number}_record_not_object")
            continue
        records.append(value)
    if not records:
        errors.append("no_records")
    return records, errors, "FAIL" if privacy else "PASS"


def _sequence(row: Mapping[str, Any], fallback: int) -> int:
    value = row.get("sequence_number")
    return value if isinstance(value, int) and not isinstance(value, bool) else fallback


def _safe_identifier(value: Any) -> str | int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}", value):
        return value
    return None


def _int_field(row: Mapping[str, Any], name: str) -> int | None:
    value = row.get(name)
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def analyze_paper_burn_in_fair_play_incident(
    path: str | Path,
    *,
    repository_root: str | Path | None = None,
) -> PaperBurnInFairPlayIncidentAnalysis:
    safe = _safe_path(path, repository_root)
    root = Path(repository_root or Path.cwd()).resolve()
    relative = safe.relative_to(root).as_posix()
    records, parse_errors, privacy_scan = _load(safe)
    ordered = sorted(enumerate(records), key=lambda pair: _sequence(pair[1], pair[0] + 1))
    symbol_values = {str(row.get("symbol")) for _, row in ordered if row.get("symbol")}
    symbol = next(iter(symbol_values), None) if len(symbol_values) <= 1 else None
    fingerprints = {str(row.get("run_fingerprint")) for _, row in ordered if row.get("run_fingerprint")}
    config_fingerprints = {str(row.get("configuration_fingerprint")) for _, row in ordered if row.get("configuration_fingerprint")}
    run_fingerprint = next(iter(fingerprints), None) if len(fingerprints) == 1 else None
    configuration_fingerprint = next(iter(config_fingerprints), None) if len(config_fingerprints) == 1 else None
    if len(symbol_values) > 1:
        parse_errors.append("symbol_mismatch")
    if len(fingerprints) > 1:
        parse_errors.append("run_fingerprint_mismatch")
    sequence_values = [_sequence(row, physical + 1) for physical, row in ordered]
    if any(current <= previous for previous, current in zip(sequence_values, sequence_values[1:])):
        parse_errors.append("sequence_not_strictly_increasing")
    rejection_events: list[FairPlayRejectionEvidence] = []
    reasons: dict[str, int] = {}
    reason_codes: dict[str, int] = {}
    unknown_reason = False
    missing_reason = False
    for physical, row in ordered:
        if row.get("record_type") != "strategy_intent":
            continue
        events = row.get("trade_intent_events")
        if not isinstance(events, list):
            continue
        for event in events:
            if not isinstance(event, Mapping) or event.get("fair_play_allowed") is not False:
                continue
            raw_reason = event.get("fair_play_reason")
            if not str(raw_reason or "").strip():
                missing_reason = True
            normalized = normalize_fair_play_reason(raw_reason)
            if normalized is PaperBurnInFairPlayReason.UNKNOWN_EXPLICIT_REASON:
                unknown_reason = True
            code = safe_reason_code(raw_reason)
            reason_codes[code] = reason_codes.get(code, 0) + 1
            reason = normalized.value
            reasons[reason] = reasons.get(reason, 0) + 1
            rejection_events.append(
                FairPlayRejectionEvidence(
                    sequence_number=_sequence(row, physical + 1),
                    timestamp=_timestamp(row.get("timestamp")),
                    normalized_reason=normalized,
                    reason_code=code,
                    intent_id=_safe_identifier(event.get("sequence_number")),
                    order_id=_safe_identifier(event.get("resulting_order_id")),
                    inventory=_dec(row.get("base_position")),
                    consecutive_rejections=_int_field(row, "fair_play_consecutive_rejections") or 0,
                )
            )
    # A row-level fair-play decision is the authoritative recorded latch point.
    halt_row: Mapping[str, Any] | None = next(
        (row for _, row in ordered if row.get("fair_play_latched") is True), None
    )
    halt_sequence = _sequence(halt_row, 0) if halt_row is not None else None
    halt_timestamp = _timestamp(halt_row.get("timestamp")) if halt_row else None
    halt_code = (
        (halt_row.get("fair_play_reason") or halt_row.get("fair_play_reason_code"))
        if halt_row
        else None
    )
    halt_trigger = normalize_fair_play_reason(halt_code).value if halt_code else None
    observed: Decimal | None = None
    threshold: Decimal | None = None
    if halt_row is not None:
        observed = _dec(halt_row.get("fair_play_trigger_metric"))
        threshold = _dec(halt_row.get("fair_play_trigger_threshold"))
        if halt_trigger == PaperBurnInFairPlayReason.REPEATED_NEAR_FLAT_CYCLE.value:
            observed = observed or _dec(halt_row.get("near_flat_cycle_count"))
        elif halt_trigger == PaperBurnInFairPlayReason.RAPID_ROUND_TRIP.value:
            observed = observed or _dec(halt_row.get("short_window_round_trip_count"))
        elif halt_trigger == PaperBurnInFairPlayReason.CONSECUTIVE_REJECTION_LIMIT.value:
            observed = observed or _dec(halt_row.get("fair_play_consecutive_rejections"))
    first = rejection_events[0] if rejection_events else None
    last = rejection_events[-1] if rejection_events else None
    max_streak = 0
    current_streak = 0
    streaks: list[int] = []
    for row_index, row in ordered:
        rejected = 0
        events = row.get("trade_intent_events")
        if row.get("record_type") == "strategy_intent" and isinstance(events, list):
            rejected = sum(isinstance(item, Mapping) and item.get("fair_play_allowed") is False for item in events)
        if rejected:
            current_streak += rejected
        else:
            if current_streak:
                streaks.append(current_streak)
            current_streak = 0
        max_streak = max(max_streak, current_streak)
    if current_streak:
        streaks.append(current_streak)
    before_open: int | None = None
    ending_open: int | None = None
    ending_inventory: Decimal | None = None
    fills_before = 0
    rejected_orders = 0
    rejected_fills = 0
    normal_after = 0
    records_after_halt = 0
    for physical, row in ordered:
        seq = _sequence(row, physical + 1)
        if halt_sequence is not None and seq > halt_sequence:
            records_after_halt += 1
        if halt_sequence is None or seq < halt_sequence:
            value = _int_field(row, "open_orders_count")
            if value is not None:
                before_open = value
        if row.get("record_type") == "paper_fill" and (halt_sequence is None or seq < halt_sequence):
            events = row.get("confirmed_fill_events")
            fills_before += len(events) if isinstance(events, list) else int(row.get("fills_count", 0) or 0)
        if halt_sequence is not None and seq > halt_sequence and row.get("record_type") == "strategy_intent":
            events = row.get("trade_intent_events")
            if isinstance(events, list):
                normal_after += sum(isinstance(item, Mapping) and item.get("fair_play_allowed") is not False for item in events)
        if row.get("record_type") == "strategy_intent" and isinstance(row.get("trade_intent_events"), list):
            for event in row["trade_intent_events"]:
                if isinstance(event, Mapping) and event.get("fair_play_allowed") is False:
                    if event.get("resulting_order_id") is not None or event.get("submitted") is True:
                        rejected_orders += 1
                    if (
                        event.get("resulting_fill_id") is not None
                        or event.get("fill_id") is not None
                        or event.get("rejected_intent_created_fill") is True
                    ):
                        rejected_fills += 1
            explicit_row_fills = row.get("rejected_intent_fill_count")
            if isinstance(explicit_row_fills, int) and explicit_row_fills > 0:
                rejected_fills += explicit_row_fills
        if row.get("record_type") in {"run_summary", "portfolio_snapshot"}:
            if row.get("record_type") == "run_summary" or ending_open is None:
                ending_open = _int_field(row, "open_orders_count")
                ending_inventory = _dec(row.get("base_position"))
    cancelled = None
    if before_open is not None and halt_row is not None:
        after = _int_field(halt_row, "open_orders_count")
        cancelled = max(before_open - (after or 0), 0) if after is not None else None
    missing: list[str] = []
    if run_fingerprint is None:
        missing.append("run_fingerprint")
    if rejection_events and missing_reason:
        missing.append("fair_play_reason_code")
    if halt_row is not None and halt_trigger is None:
        missing.append("fair_play_reason")
    if halt_row is not None and threshold is None:
        missing.append("fair_play_trigger_threshold")
    blockers: list[str] = list(parse_errors)
    warnings: list[str] = []
    if unknown_reason:
        warnings.append("unknown_fair_play_reason_code")
    if missing_reason:
        blockers.append("missing_fair_play_reason_code")
    if privacy_scan == "FAIL":
        blockers.append("privacy_scan_failed")
    if missing:
        blockers.append("insufficient_recorded_evidence")
    enforcement = "PASS"
    if rejected_orders or rejected_fills or normal_after or (ending_open not in (None, 0)):
        enforcement = "FAIL"
        if rejected_orders:
            blockers.append("rejected_intent_created_order")
        if rejected_fills:
            blockers.append("rejected_intent_created_fill")
        if normal_after:
            blockers.append("normal_intent_after_halt")
        if ending_open not in (None, 0):
            blockers.append("open_orders_after_shutdown")
    compatibility = "FAIL" if halt_trigger is not None else "PASS"
    if halt_trigger in {
        PaperBurnInFairPlayReason.UNKNOWN_EXPLICIT_REASON.value,
        PaperBurnInFairPlayReason.UNDISCLOSED_FILTER_DECISION.value,
    }:
        compatibility = "FAIL"
    evidence = "INSUFFICIENT_RECORDED_EVIDENCE" if missing or parse_errors else "SUFFICIENT"
    if privacy_scan == "FAIL":
        result = "FAIL"
    elif evidence != "SUFFICIENT":
        result = "INSUFFICIENT_RECORDED_EVIDENCE"
    elif enforcement == "FAIL" or compatibility == "FAIL":
        result = "FAIL"
    else:
        result = "PASS"
    relative = safe.relative_to(root).as_posix()
    return PaperBurnInFairPlayIncidentAnalysis(
        input_file=relative,
        network_access_used=False,
        integrity="PASS" if not parse_errors else "FAIL",
        symbol=symbol,
        run_fingerprint=run_fingerprint,
        configuration_fingerprint=configuration_fingerprint,
        first_rejection_sequence=first.sequence_number if first else None,
        last_rejection_sequence=last.sequence_number if last else None,
        halt_sequence=halt_sequence,
        first_rejection_timestamp=first.timestamp if first else None,
        last_rejection_timestamp=last.timestamp if last else None,
        halt_timestamp=halt_timestamp,
        rejection_count=len(rejection_events),
        maximum_consecutive_rejections=max_streak,
        normalized_reasons=tuple(sorted(reasons.items())),
        reason_code_counts=tuple(sorted(reason_codes.items())),
        dominant_reason=max(reasons, key=reasons.get) if reasons else None,
        dominant_reason_count=max(reasons.values(), default=0),
        halt_trigger=halt_trigger,
        halt_threshold=threshold,
        observed_trigger_value=observed,
        paper_orders_open_before_halt=before_open,
        paper_orders_cancelled_by_halt=cancelled,
        rejected_intents_creating_orders=rejected_orders,
        rejected_intents_creating_fills=rejected_fills,
        normal_intents_after_halt=normal_after,
        fills_before_halt=fills_before,
        ending_inventory=ending_inventory,
        ending_open_orders=ending_open,
        enforcement=enforcement,
        strategy_compatibility=compatibility,
        evidence_sufficiency=evidence,
        privacy_status=privacy_scan,
        missing_fields=tuple(dict.fromkeys(missing)),
        blockers=tuple(dict.fromkeys(blockers)),
        warnings=tuple(dict.fromkeys(warnings)),
        rejection_events=tuple(rejection_events),
        consecutive_rejection_streaks=tuple(streaks),
        records_after_halt=records_after_halt,
        result=result,
    )


analyze_incident = analyze_paper_burn_in_fair_play_incident
analyze_fair_play_incident = analyze_paper_burn_in_fair_play_incident
normalize_reason = normalize_fair_play_reason
