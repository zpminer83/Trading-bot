"""Bounded public-data paper burn-in for SOMI:USDso.

This command is intentionally separate from the REST paper execution scripts.
It has no authenticated-account, live-broker, transaction, signer, submitter,
journal, nonce, or mutation-RPC dependency.  Public order-book data is fed to
the existing conservative paper engine only.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
import os
from pathlib import Path
import time
from typing import Any, Callable, Mapping

from bot.analytics.paper_run_recorder import PaperRunRecord, PaperRunRecorder
from bot.market.market_cache import MarketCache
from bot.market.market_data_service import MarketDataService
from bot.risk.market_freshness import MarketFreshnessLimits
from bot.risk.portfolio_risk_guard import PortfolioRiskLimits
from bot.competition.fair_play_guard import FairPlayLimits
from bot.execution.passive_fill_evidence import PassiveFillEvidenceTracker
from scripts.check_dreamdex_orderbook_rest import (
    DEFAULT_BASE_URL,
    DEFAULT_DEPTH,
    DEFAULT_SYMBOL,
    build_orderbook_url,
    extract_orderbook_payload,
    fetch_json,
)
from scripts.run_rest_paper_loop import (
    build_engine,
    build_record,
    fmt_decimal,
    shutdown_paper_orders,
)


MAX_CONSECUTIVE_FAILURES = 3
MAX_PRICE_STEP = Decimal("0.25")
MAX_ALLOWED_SNAPSHOTS = 10_000


@dataclass(frozen=True)
class BurnInConfiguration:
    symbol: str = DEFAULT_SYMBOL
    duration_minutes: Decimal = Decimal("15")
    sample_interval_seconds: Decimal = Decimal("5")
    initial_equity: Decimal = Decimal("150")
    output_dir: Path = Path("data") / "paper_runs"
    max_snapshots: int = 180
    depth: int = DEFAULT_DEPTH

    def __post_init__(self) -> None:
        if not isinstance(self.symbol, str) or not self.symbol or any(
            char.isspace() for char in self.symbol
        ):
            raise ValueError("symbol must be a non-empty exact market symbol")
        for name in ("duration_minutes", "sample_interval_seconds", "initial_equity"):
            try:
                value = Decimal(str(getattr(self, name)))
            except (InvalidOperation, ValueError, TypeError) as exc:
                raise ValueError(f"{name} must be a finite Decimal") from exc
            if not value.is_finite() or value <= 0:
                raise ValueError(f"{name} must be > 0")
        if self.max_snapshots < 1 or self.max_snapshots > MAX_ALLOWED_SNAPSHOTS:
            raise ValueError("max_snapshots must be between 1 and 10000")
        if self.depth < 1 or self.depth > 100:
            raise ValueError("depth must be between 1 and 100")

    @property
    def duration_seconds(self) -> Decimal:
        return Decimal(str(self.duration_minutes)) * Decimal("60")

    @property
    def requested_snapshots(self) -> int:
        estimated = int(
            (self.duration_seconds / Decimal(str(self.sample_interval_seconds))).to_integral_value(
                rounding="ROUND_CEILING"
            )
        )
        return max(1, min(self.max_snapshots, estimated))


@dataclass
class BurnInResult:
    symbol: str
    duration_requested_seconds: Decimal
    duration_completed_seconds: Decimal
    paper_only: bool = True
    public_market_data_only: bool = True
    authenticated_account_data_used: bool = False
    executable_candidate_created: bool = False
    live_order_created: bool = False
    production_approval: bool = False
    signer_invoked: bool = False
    submission_invoked: bool = False
    real_submission_enabled: bool = False
    snapshots_requested: int = 0
    snapshots_accepted: int = 0
    snapshots_rejected: int = 0
    stale_snapshots: int = 0
    crossed_books: int = 0
    malformed_snapshots: int = 0
    duplicate_snapshots: int = 0
    extreme_jump_rejections: int = 0
    largest_observed_price_step: Decimal | None = None
    minimum_spread: Decimal | None = None
    median_spread: Decimal | None = None
    maximum_spread: Decimal | None = None
    strategy_intents: int = 0
    risk_approved_intents: int = 0
    risk_rejected_intents: int = 0
    fair_play_rejected_intents: int = 0
    paper_orders_created: int = 0
    paper_cancels: int = 0
    paper_replacements: int = 0
    simulated_partial_fills: int = 0
    simulated_full_fills: int = 0
    ending_cash: Decimal = Decimal("0")
    ending_inventory: Decimal = Decimal("0")
    ending_marked_equity: Decimal = Decimal("0")
    peak_equity: Decimal = Decimal("0")
    maximum_drawdown: Decimal = Decimal("0")
    gap_risk_maximum_projected_drawdown: Decimal | None = None
    preemptive_halt_triggered: bool = False
    hard_kill_triggered: bool = False
    fair_play_halt_triggered: bool = False
    open_paper_orders: int = 0
    public_logical_snapshots: int = 0
    public_http_requests: int = 0
    authenticated_http_requests: int = 0
    read_only_rpc_requests: int = 0
    total_network_read_requests: int = 0
    live_order_calls: int = 0
    mutation_rpc_calls: int = 0
    production_journal_writes: int = 0
    blockers: list[str] | None = None
    result: str = "FAIL"
    output_file: str = ""
    run_fingerprint: str = ""

    def safe_dict(self) -> dict[str, Any]:
        payload = dict(self.__dict__)
        payload["duration_requested_seconds"] = str(self.duration_requested_seconds)
        payload["duration_completed_seconds"] = str(self.duration_completed_seconds)
        for key, value in list(payload.items()):
            if isinstance(value, Decimal):
                payload[key] = str(value)
        payload["blockers"] = list(self.blockers or [])
        return payload


def _safe_failure_reason(exc: BaseException) -> str:
    text = str(exc).lower()
    for reason in (
        "crossed_orderbook", "non_positive_depth", "missing_bid_or_ask",
        "malformed_snapshot", "extreme_price_jump", "symbol_mismatch", "symbol_missing",
    ):
        if reason in text:
            return reason
    if "http" in text or "url" in text or "network" in text:
        return "public_transport_failure"
    if "json" in text or "decode" in text:
        return "malformed_json"
    if "symbol" in text:
        return "symbol_mismatch"
    return "validation_failure"


def _decimal(value: Any, name: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"{name}_malformed") from exc
    if not parsed.is_finite():
        raise ValueError(f"{name}_malformed")
    return parsed


def _raw_levels(payload: Mapping[str, Any], side: str) -> list[tuple[Any, Any]]:
    raw = payload.get(side, [])
    if isinstance(raw, Mapping):
        return list(raw.items())
    if not isinstance(raw, (list, tuple)):
        raise ValueError(f"{side}_levels_malformed")
    values: list[tuple[Any, Any]] = []
    for level in raw:
        if isinstance(level, Mapping):
            price = level.get("price", level.get("p"))
            quantity = level.get("quantity", level.get("qty", level.get("size", level.get("amount", level.get("q")))))
        elif isinstance(level, (list, tuple)) and len(level) >= 2:
            price, quantity = level[0], level[1]
        else:
            raise ValueError(f"{side}_level_malformed")
        values.append((price, quantity))
    return values


def validate_public_orderbook_payload(
    payload: Mapping[str, Any],
    *,
    symbol: str,
    previous_mid: Decimal | None = None,
) -> tuple[str | None, Decimal | None]:
    """Validate raw values before the permissive shared parser can skip them."""
    if not isinstance(payload, Mapping):
        return "malformed_snapshot", None
    declared_symbol = payload.get("symbol", payload.get("market", payload.get("pair")))
    if declared_symbol is None:
        return "symbol_missing", None
    if str(declared_symbol) != symbol:
        return "symbol_mismatch", None
    try:
        bids = _raw_levels(payload, "bids")
        asks = _raw_levels(payload, "asks")
        if not bids or not asks:
            return "missing_bid_or_ask", None
        parsed_bids = [(_decimal(price, "price"), _decimal(quantity, "quantity")) for price, quantity in bids]
        parsed_asks = [(_decimal(price, "price"), _decimal(quantity, "quantity")) for price, quantity in asks]
        if any(price <= 0 or quantity <= 0 for price, quantity in (*parsed_bids, *parsed_asks)):
            return "non_positive_depth", None
        best_bid = max(price for price, _ in parsed_bids)
        best_ask = min(price for price, _ in parsed_asks)
        if best_bid >= best_ask:
            return "crossed_orderbook", None
        mid = (best_bid + best_ask) / Decimal("2")
        if previous_mid is not None:
            step = abs(mid - previous_mid) / previous_mid if previous_mid > 0 else Decimal("0")
            if step > MAX_PRICE_STEP:
                return "extreme_price_jump", mid
        return None, mid
    except ValueError:
        return "malformed_snapshot", None


def _safe_filename_symbol(symbol: str) -> str:
    return "".join(char if char.isalnum() else "_" for char in symbol)


def _run_fingerprint(config: BurnInConfiguration) -> str:
    payload = {
        "symbol": config.symbol,
        "duration_minutes": str(config.duration_minutes),
        "sample_interval_seconds": str(config.sample_interval_seconds),
        "initial_equity": str(config.initial_equity),
        "max_snapshots": config.max_snapshots,
    }
    return sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _relative_output_path(path: Path) -> str:
    try:
        return path.resolve().relative_to(Path.cwd().resolve()).as_posix()
    except ValueError:
        return f"{path.parent.name}/{path.name}"


def _append_event(
    recorder: PaperRunRecorder,
    path: Path,
    *,
    record_type: str,
    timestamp: datetime,
    symbol: str,
    iteration_index: int = 0,
    base: PaperRunRecord | None = None,
    notes: list[str] | None = None,
) -> None:
    record = base or PaperRunRecord(timestamp=timestamp, symbol=symbol)
    # Burn-in files carry a physical sequence independent of iteration index:
    # one iteration can intentionally emit several typed audit records.
    sequence_number = recorder.count + 1
    fingerprint = getattr(base, "run_fingerprint", None)
    if fingerprint is None:
        fingerprint = path.stem.rsplit("_", 1)[-1]
    record = replace(
        record,
        timestamp=timestamp,
        symbol=symbol,
        record_type=record_type,
        iteration_index=iteration_index,
        notes=list(notes or record.notes),
        sequence_number=sequence_number,
        run_fingerprint=fingerprint,
    )
    recorder.append_jsonl(path, record, sync_to_disk=True)


def run_burn_in(
    config: BurnInConfiguration,
    *,
    fetcher: Callable[[str], Mapping[str, Any]] | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
    clock: Callable[[], datetime] | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    output_path: Path | None = None,
) -> BurnInResult:
    """Run a bounded public-data paper loop; injectable readers keep tests offline."""
    now = clock or (lambda: datetime.now(timezone.utc))
    fetch = fetcher or (lambda url: fetch_json(url))
    started_at = now().astimezone(timezone.utc)
    fingerprint = _run_fingerprint(config)
    path = output_path or (
        config.output_dir
        / f"paper_run_burn_in_{started_at.strftime('%Y%m%d_%H%M%S')}_{fingerprint[:12]}.jsonl"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    recorder = PaperRunRecorder()
    _append_event(
        recorder,
        path,
        record_type="run_start",
        timestamp=started_at,
        symbol=config.symbol,
        notes=["paper_only", "public_market_data_only"],
    )

    market_cache = MarketCache()
    market_data = MarketDataService(market_cache=market_cache)
    risk_limits = PortfolioRiskLimits(
        max_drawdown=Decimal("0.10"),
        preemptive_drawdown=Decimal("0.08"),
        adverse_move_fraction_long=Decimal("0.12"),
        adverse_move_fraction_short=Decimal("0.12"),
        minimum_drawdown_reserve_fraction=Decimal("0.01"),
        emergency_exit_slippage_fraction=Decimal("0.02"),
        emergency_exit_fee_fraction=Decimal("0.002"),
        maximum_gap_risk_position_fraction=Decimal("0.15"),
    )
    freshness_limits = MarketFreshnessLimits(
        max_exchange_age_seconds=Decimal("30"),
        max_unchanged_seconds=Decimal("30"),
        max_future_skew_seconds=Decimal("5"),
    )
    fair_play_limits = FairPlayLimits()
    engine = build_engine(
        symbol=config.symbol,
        market_cache=market_cache,
        initial_cash=config.initial_equity,
        order_size_usd=Decimal("5"),
        max_open_orders=2,
        pair_boost=Decimal("1"),
        max_spread_percent=Decimal("0.02"),
        min_best_bid_quantity=Decimal("1"),
        min_best_ask_quantity=Decimal("1"),
        market_freshness_limits=freshness_limits,
        portfolio_risk_limits=risk_limits,
        fair_play_limits=fair_play_limits,
        signal_limits=None,
        paper_risk_exit_enabled=False,
    )
    fill_evidence_tracker = PassiveFillEvidenceTracker()
    # The public URL is deliberately never persisted or printed.  A caller can
    # still provide the repository's existing public base through the same
    # environment convention used by the read-only market script.
    url = build_orderbook_url(os.getenv("DREAMDEX_API_BASE_URL", DEFAULT_BASE_URL), config.symbol, config.depth)

    result = BurnInResult(
        symbol=config.symbol,
        duration_requested_seconds=config.duration_seconds,
        duration_completed_seconds=Decimal("0"),
        snapshots_requested=config.requested_snapshots,
        blockers=[],
        output_file=_relative_output_path(path),
        run_fingerprint=fingerprint,
    )
    spreads: list[Decimal] = []
    consecutive_failures = 0
    previous_mid: Decimal | None = None
    previous_fingerprint: tuple[Any, ...] | None = None
    next_iteration = 1
    try:
        start_mono = monotonic()
    except Exception:
        start_mono = 0.0
    last_mono = start_mono

    def elapsed_seconds() -> Decimal:
        nonlocal last_mono
        try:
            last_mono = monotonic()
        except Exception:
            pass
        return Decimal(str(max(0.0, last_mono - start_mono)))

    try:
        while next_iteration <= config.max_snapshots:
            elapsed = elapsed_seconds()
            if next_iteration > 1 and elapsed >= config.duration_seconds:
                break
            timestamp = now().astimezone(timezone.utc)
            before_open = len(engine.broker.open_orders)
            try:
                response = fetch(url)
                payload = extract_orderbook_payload(response=response, symbol=config.symbol)
                reason, candidate_mid = validate_public_orderbook_payload(
                    payload, symbol=config.symbol, previous_mid=previous_mid
                )
                if reason is not None:
                    raise ValueError(reason)
                snapshot = market_data.handle_orderbook_payload(payload, default_symbol=config.symbol)
                if snapshot.symbol != config.symbol:
                    raise ValueError("symbol_mismatch")
                raw_fingerprint = (
                    snapshot.orderbook.timestamp,
                    snapshot.orderbook.nonce,
                    tuple((level.price, level.quantity) for level in snapshot.orderbook.bids),
                    tuple((level.price, level.quantity) for level in snapshot.orderbook.asks),
                )
                if raw_fingerprint == previous_fingerprint:
                    result.duplicate_snapshots += 1
                previous_fingerprint = raw_fingerprint
                step = (
                    abs(snapshot.mid_price - previous_mid) / previous_mid
                    if snapshot.mid_price is not None and previous_mid is not None and previous_mid > 0
                    else Decimal("0")
                )
                if step > (result.largest_observed_price_step or Decimal("0")):
                    result.largest_observed_price_step = step
                previous_mid = snapshot.mid_price
                spreads.append(snapshot.spread or Decimal("0"))
                result.minimum_spread = min(spreads)
                result.maximum_spread = max(spreads)

                open_orders_before_step = tuple(engine.broker.open_orders)
                passive_fill_evidence = fill_evidence_tracker.observe(
                    orders=open_orders_before_step,
                    orderbook=snapshot.orderbook,
                    observed_at=timestamp,
                )
                result_step = engine.step(timestamp=timestamp)
                fill_evidence_tracker.synchronize(
                    orders=tuple(engine.broker.open_orders),
                    orderbook=snapshot.orderbook,
                    observed_at=timestamp,
                )
                freshness = result_step.market_freshness_decision
                safety = result_step.market_safety_decision
                accepted = (freshness is None or freshness.fresh) and (safety is None or safety.safe)
                if not accepted:
                    result.snapshots_rejected += 1
                    reason_text = freshness.reason if freshness is not None and not freshness.fresh else safety.reason if safety is not None else "market_rejected"
                    if freshness is not None and not freshness.fresh:
                        result.stale_snapshots += 1
                    if reason_text == "crossed_orderbook":
                        result.crossed_books += 1
                    _append_event(
                        recorder, path, record_type="market_reject", timestamp=timestamp,
                        symbol=config.symbol, iteration_index=next_iteration,
                        notes=[reason_text],
                    )
                    consecutive_failures += 1
                else:
                    result.snapshots_accepted += 1
                    consecutive_failures = 0
                    record = build_record(
                        timestamp=timestamp, symbol=config.symbol, snapshot=snapshot,
                        result=result_step, engine=engine, iteration_index=next_iteration,
                        passive_fill_evidence=passive_fill_evidence,
                    )
                    _append_event(recorder, path, record_type="market_snapshot", timestamp=timestamp, symbol=config.symbol, iteration_index=next_iteration, base=record)
                    if result_step.intents:
                        _append_event(recorder, path, record_type="strategy_intent", timestamp=timestamp, symbol=config.symbol, iteration_index=next_iteration, base=record)
                    if result_step.portfolio_risk_decision is not None:
                        _append_event(recorder, path, record_type="risk_decision", timestamp=timestamp, symbol=config.symbol, iteration_index=next_iteration, base=record)
                    if result_step.fair_play_decisions or result_step.fair_play_latched is not None:
                        _append_event(recorder, path, record_type="fair_play_decision", timestamp=timestamp, symbol=config.symbol, iteration_index=next_iteration, base=record)
                    if result_step.submitted_orders:
                        _append_event(recorder, path, record_type="paper_order", timestamp=timestamp, symbol=config.symbol, iteration_index=next_iteration, base=record)
                    if result_step.fills:
                        _append_event(recorder, path, record_type="paper_fill", timestamp=timestamp, symbol=config.symbol, iteration_index=next_iteration, base=record)
                    _append_event(recorder, path, record_type="portfolio_snapshot", timestamp=timestamp, symbol=config.symbol, iteration_index=next_iteration, base=record)

                    result.strategy_intents += len(result_step.intents)
                    result.risk_approved_intents += sum(decision.approved for decision in result_step.decisions)
                    result.risk_rejected_intents += sum(not decision.approved for decision in result_step.decisions)
                    result.fair_play_rejected_intents += result_step.fair_play_blocked_intents_count
                    result.paper_orders_created += len(result_step.submitted_orders)
                    result.paper_replacements += int(before_open > 0 and bool(result_step.submitted_orders))
                    cancelled = max(before_open - len(result_step.fills) - len(engine.broker.open_orders), 0)
                    result.paper_cancels += cancelled
                    for fill in result_step.fills:
                        source = engine.broker.source_order_for_fill(fill)
                        if source is not None and fill.quantity < source.intent.quantity:
                            result.simulated_partial_fills += 1
                        else:
                            result.simulated_full_fills += 1
                    if result_step.portfolio_risk_decision is not None:
                        result.preemptive_halt_triggered |= result_step.portfolio_risk_decision.entry_halt_latched
                        result.hard_kill_triggered |= result_step.portfolio_risk_decision.latched
                    result.fair_play_halt_triggered |= bool(result_step.fair_play_latched)
                    projected = [budget.projected_shocked_drawdown for budget in result_step.gap_risk_budgets if budget.projected_shocked_drawdown is not None]
                    if projected:
                        result.gap_risk_maximum_projected_drawdown = max(projected + ([result.gap_risk_maximum_projected_drawdown] if result.gap_risk_maximum_projected_drawdown is not None else []))
            except Exception as exc:
                reason = _safe_failure_reason(exc)
                result.snapshots_rejected += 1
                if reason == "malformed_json" or reason in {"malformed_snapshot", "non_positive_depth", "missing_bid_or_ask"}:
                    result.malformed_snapshots += 1
                if reason == "crossed_orderbook":
                    result.crossed_books += 1
                if reason == "extreme_price_jump":
                    result.extreme_jump_rejections += 1
                consecutive_failures += 1
                _append_event(recorder, path, record_type="market_reject", timestamp=timestamp, symbol=config.symbol, iteration_index=next_iteration, notes=[reason])
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                result.blockers.append("consecutive_market_failures")
                _append_event(recorder, path, record_type="halt", timestamp=timestamp, symbol=config.symbol, iteration_index=next_iteration, notes=["consecutive_market_failures"])
                break
            next_iteration += 1
            if next_iteration <= config.max_snapshots:
                sleep_fn(float(config.sample_interval_seconds))
    except Exception:
        result.blockers.append("unhandled_exception")
    finally:
        remaining = len(engine.broker.open_orders)
        if remaining:
            result.paper_cancels += remaining
        shutdown_paper_orders(engine, force=True)
        result.open_paper_orders = len(engine.broker.open_orders)

    if spreads:
        ordered = sorted(spreads)
        middle = len(ordered) // 2
        result.median_spread = ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / Decimal("2")
    portfolio = engine.portfolio
    result.ending_cash = portfolio.cash_balance
    result.ending_inventory = portfolio.base_position
    result.ending_marked_equity = portfolio.equity
    result.peak_equity = portfolio.peak_equity
    result.maximum_drawdown = portfolio.drawdown
    result.duration_completed_seconds = elapsed_seconds()
    result.public_logical_snapshots = result.snapshots_accepted + result.snapshots_rejected
    result.public_http_requests = result.public_logical_snapshots
    result.total_network_read_requests = result.public_http_requests
    if result.snapshots_accepted == 0:
        result.blockers.append("no_accepted_public_snapshots")
    if result.open_paper_orders != 0:
        result.blockers.append("open_paper_orders_after_shutdown")
    if result.hard_kill_triggered:
        result.blockers.append("hard_risk_kill_triggered")
    if result.fair_play_halt_triggered:
        result.blockers.append("fair_play_halted")
    result.blockers = list(dict.fromkeys(result.blockers))
    result.result = "PASS" if not result.blockers else "FAIL"
    summary_metrics = {
        "market_snapshots": result.public_logical_snapshots,
        "accepted_snapshots": result.snapshots_accepted,
        "rejected_snapshots": result.snapshots_rejected,
        "stale_snapshots": result.stale_snapshots,
        "crossed_books": result.crossed_books,
        "malformed_snapshots": result.malformed_snapshots,
        "strategy_intents": result.strategy_intents,
        "risk_approved_intents": result.risk_approved_intents,
        "risk_rejected_intents": result.risk_rejected_intents,
        "fair_play_rejected_intents": result.fair_play_rejected_intents,
        "paper_orders_created": result.paper_orders_created,
        "paper_cancels": result.paper_cancels,
        "paper_replacements": result.paper_replacements,
        "partial_fills": result.simulated_partial_fills,
        "full_fills": result.simulated_full_fills,
        "halts": int(bool(result.blockers)),
        "open_orders_after_shutdown": result.open_paper_orders,
        "live_order_calls": result.live_order_calls,
        "authenticated_http_requests": result.authenticated_http_requests,
        "read_only_rpc_requests": result.read_only_rpc_requests,
        "mutation_rpc_calls": result.mutation_rpc_calls,
        "production_journal_writes": result.production_journal_writes,
        "signer_calls": int(result.signer_invoked),
        "submission_calls": int(result.submission_invoked),
    }
    summary_notes = [
        f"result={result.result}",
        *result.blockers,
        *(f"audit.{key}={value}" for key, value in summary_metrics.items()),
    ]
    summary = PaperRunRecord(
        timestamp=now().astimezone(timezone.utc), symbol=config.symbol, record_type="run_summary",
        iteration_index=next_iteration, iteration_ok=result.result == "PASS",
        notes=summary_notes,
        cash_balance=result.ending_cash, base_position=result.ending_inventory,
        equity=result.ending_marked_equity, peak_equity=result.peak_equity,
        drawdown=result.maximum_drawdown,
        risk_max_drawdown=Decimal("0.10"),
        preemptive_drawdown=Decimal("0.08"),
        open_orders_count=result.open_paper_orders,
        fees_paid=getattr(portfolio, "fees_paid", None),
        projected_shocked_drawdown=result.gap_risk_maximum_projected_drawdown,
        preemptive_halt_latched=result.preemptive_halt_triggered,
        hard_kill_latched=result.hard_kill_triggered,
        gap_risk_assumptions_available=result.gap_risk_maximum_projected_drawdown is not None,
    )
    _append_event(
        recorder, path, record_type="run_summary", timestamp=summary.timestamp,
        symbol=config.symbol, iteration_index=next_iteration, base=summary,
    )
    return result


def print_burn_in_summary(result: BurnInResult) -> None:
    print("LIVE PUBLIC PAPER BURN-IN:")
    for label, value in (
        ("symbol", result.symbol),
        ("duration requested", f"{result.duration_requested_seconds}s"),
        ("duration completed", f"{result.duration_completed_seconds}s"),
        ("paper only", "YES"),
        ("public market data only", "YES"),
        ("authenticated account data used", "NO"),
        ("executable candidate created", "NO"),
        ("live order created", "NO"),
        ("production approval", "NO"),
        ("signer invoked", "NO"),
        ("submission invoked", "NO"),
        ("snapshots requested", result.snapshots_requested),
        ("snapshots accepted", result.snapshots_accepted),
        ("snapshots rejected", result.snapshots_rejected),
        ("stale snapshots", result.stale_snapshots),
        ("crossed books", result.crossed_books),
        ("malformed snapshots", result.malformed_snapshots),
        ("largest observed price step", result.largest_observed_price_step),
        ("minimum spread", result.minimum_spread),
        ("median spread", result.median_spread),
        ("maximum spread", result.maximum_spread),
        ("strategy intents", result.strategy_intents),
        ("risk-approved intents", result.risk_approved_intents),
        ("risk-rejected intents", result.risk_rejected_intents),
        ("fair-play-rejected intents", result.fair_play_rejected_intents),
        ("paper orders created", result.paper_orders_created),
        ("paper cancels", result.paper_cancels),
        ("paper replacements", result.paper_replacements),
        ("simulated partial fills", result.simulated_partial_fills),
        ("simulated full fills", result.simulated_full_fills),
        ("ending cash", result.ending_cash),
        ("ending inventory", result.ending_inventory),
        ("ending marked equity", result.ending_marked_equity),
        ("peak equity", result.peak_equity),
        ("maximum drawdown", result.maximum_drawdown),
        ("gap-risk maximum projected drawdown", result.gap_risk_maximum_projected_drawdown),
        ("preemptive halt triggered", "YES" if result.preemptive_halt_triggered else "NO"),
        ("hard kill triggered", "YES" if result.hard_kill_triggered else "NO"),
        ("fair-play halt triggered", "YES" if result.fair_play_halt_triggered else "NO"),
        ("open paper orders", result.open_paper_orders),
        ("public logical snapshots", result.public_logical_snapshots),
        ("public HTTP requests", result.public_http_requests),
        ("authenticated HTTP requests", result.authenticated_http_requests),
        ("read-only RPC requests", result.read_only_rpc_requests),
        ("total network read requests", result.total_network_read_requests),
        ("live order calls", result.live_order_calls),
        ("mutation RPC calls", result.mutation_rpc_calls),
        ("production journal writes", result.production_journal_writes),
        ("signer calls", "YES" if result.signer_invoked else "NO"),
        ("submission calls", "YES" if result.submission_invoked else "NO"),
        ("authoritative live trading status available", "NO"),
        ("usable for production readiness", "NO"),
        ("Real submission enabled", "NO"),
        ("result", result.result),
        ("blockers", ", ".join(result.blockers or []) or "none"),
        ("output file", result.output_file),
        ("run fingerprint", result.run_fingerprint[:16]),
    ):
        print(f"  {label}: {fmt_decimal(value) if isinstance(value, Decimal) else value}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Bounded public-data paper burn-in")
    parser.add_argument("--symbol", default=DEFAULT_SYMBOL)
    parser.add_argument("--duration-minutes", type=Decimal, default=Decimal("15"))
    parser.add_argument("--sample-interval-seconds", type=Decimal, default=Decimal("5"))
    parser.add_argument("--initial-equity", type=Decimal, default=Decimal("150"))
    parser.add_argument("--output-dir", type=Path, default=Path("data") / "paper_runs")
    parser.add_argument("--max-snapshots", type=int, default=180)
    args = parser.parse_args(argv)
    config = BurnInConfiguration(
        symbol=args.symbol, duration_minutes=args.duration_minutes,
        sample_interval_seconds=args.sample_interval_seconds,
        initial_equity=args.initial_equity, output_dir=args.output_dir,
        max_snapshots=args.max_snapshots,
    )
    result = run_burn_in(config)
    print_burn_in_summary(result)
    return 0 if result.result == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
