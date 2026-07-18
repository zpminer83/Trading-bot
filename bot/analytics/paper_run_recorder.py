import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any


def serialize_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)

    if isinstance(value, datetime):
        return value.isoformat()

    if isinstance(value, list):
        return [serialize_value(item) for item in value]

    if isinstance(value, dict):
        return {
            str(key): serialize_value(item)
            for key, item in value.items()
        }

    return value


@dataclass(frozen=True)
class PaperRunRecord:
    timestamp: datetime
    symbol: str

    iteration_index: int = 0
    iteration_ok: bool = True
    error_type: str | None = None
    error_message: str | None = None
    consecutive_failures: int = 0

    best_bid: Decimal | None = None
    best_ask: Decimal | None = None
    mid_price: Decimal | None = None
    spread: Decimal | None = None

    market_safe: bool | None = None
    market_safety_reason: str | None = None

    market_fresh: bool | None = None
    market_freshness_reason: str | None = None
    exchange_age_seconds: Decimal | None = None
    clock_skew_seconds: Decimal | None = None
    unchanged_seconds: Decimal | None = None

    portfolio_risk_allowed: bool | None = None
    portfolio_risk_reason: str | None = None
    portfolio_risk_latched: bool | None = None
    risk_drawdown: Decimal | None = None
    risk_max_drawdown: Decimal | None = None

    evaluated_open_orders_count: int = 0
    orders_at_touch_count: int = 0
    crossed_order_count: int = 0
    level_quantity_decreased_count: int = 0
    level_disappeared_count: int = 0
    max_open_order_age_seconds: Decimal | None = None

    confirmed_fill_events: list[dict[str, Any]] = field(default_factory=list)
    fair_play_allowed: bool | None = None
    fair_play_reason: str | None = None
    fair_play_latched: bool | None = None
    fair_play_blocked_intents_count: int = 0
    short_window_round_trip_count: int = 0
    near_flat_cycle_count: int = 0

    risk_exit_enabled: bool | None = None
    risk_exit_intents_count: int = 0
    risk_exit_fills_count: int = 0
    risk_exit_reason: str | None = None

    trade_intent_events: list[dict[str, Any]] = field(default_factory=list)
    generated_intent_purpose_counts: dict[str, int] = field(default_factory=dict)
    confirmed_fill_purpose_counts: dict[str, int] = field(default_factory=dict)

    signal_state: str | None = None
    signal_reason: str | None = None
    signal_sample_count: int = 0
    signal_spread_bps: Decimal | None = None
    signal_bid_depth: Decimal | None = None
    signal_ask_depth: Decimal | None = None
    signal_depth_imbalance: Decimal | None = None
    signal_microprice: Decimal | None = None
    signal_microprice_edge_bps: Decimal | None = None
    signal_one_step_return_bps: Decimal | None = None
    signal_rolling_momentum_bps: Decimal | None = None
    signal_confidence: Decimal | None = None

    depth_imbalance_l1: Decimal | None = None
    depth_imbalance_l2: Decimal | None = None
    depth_imbalance_l3: Decimal | None = None
    depth_imbalance_l5: Decimal | None = None
    depth_imbalance_l10: Decimal | None = None
    depth_bid_l1: Decimal | None = None
    depth_ask_l1: Decimal | None = None
    depth_bid_l5: Decimal | None = None
    depth_ask_l5: Decimal | None = None
    l1_edge_sign_consistent: bool | None = None
    ask_depth_concentration_l2_to_l5: Decimal | None = None
    bid_depth_concentration_l2_to_l5: Decimal | None = None

    intents_count: int = 0
    decisions_count: int = 0
    fills_count: int = 0
    submitted_orders_count: int = 0
    open_orders_count: int = 0

    cash_balance: Decimal = Decimal("0")
    base_position: Decimal = Decimal("0")
    equity: Decimal = Decimal("0")
    realized_pnl: Decimal = Decimal("0")
    unrealized_pnl: Decimal = Decimal("0")
    drawdown: Decimal = Decimal("0")
    total_volume: Decimal = Decimal("0")

    weekly_volume: Decimal = Decimal("0")
    estimated_score: Decimal = Decimal("0")
    raffle_tickets: int = 0

    notes: list[str] = field(default_factory=list)
    # Kept after the historical fields so positional construction remains
    # compatible with older paper-run callers.
    record_type: str = "iteration"
    sequence_number: int = 0
    run_fingerprint: str | None = None
    peak_equity: Decimal | None = None
    fees_paid: Decimal | None = None
    reserved_exposure: Decimal | None = None
    projected_shocked_drawdown: Decimal | None = None
    preemptive_drawdown: Decimal | None = None
    preemptive_halt_latched: bool | None = None
    hard_kill_latched: bool | None = None
    gap_risk_assumptions_available: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        return serialize_value(
            {
                "timestamp": self.timestamp,
                "symbol": self.symbol,
                "record_type": self.record_type,
                "iteration_index": self.iteration_index,
                "iteration_ok": self.iteration_ok,
                "error_type": self.error_type,
                "error_message": self.error_message,
                "consecutive_failures": self.consecutive_failures,
                "best_bid": self.best_bid,
                "best_ask": self.best_ask,
                "mid_price": self.mid_price,
                "spread": self.spread,
                "market_safe": self.market_safe,
                "market_safety_reason": self.market_safety_reason,
                "market_fresh": self.market_fresh,
                "market_freshness_reason": self.market_freshness_reason,
                "exchange_age_seconds": self.exchange_age_seconds,
                "clock_skew_seconds": self.clock_skew_seconds,
                "unchanged_seconds": self.unchanged_seconds,
                "portfolio_risk_allowed": self.portfolio_risk_allowed,
                "portfolio_risk_reason": self.portfolio_risk_reason,
                "portfolio_risk_latched": self.portfolio_risk_latched,
                "risk_drawdown": self.risk_drawdown,
                "risk_max_drawdown": self.risk_max_drawdown,
                "evaluated_open_orders_count": self.evaluated_open_orders_count,
                "orders_at_touch_count": self.orders_at_touch_count,
                "crossed_order_count": self.crossed_order_count,
                "level_quantity_decreased_count": (
                    self.level_quantity_decreased_count
                ),
                "level_disappeared_count": self.level_disappeared_count,
                "max_open_order_age_seconds": self.max_open_order_age_seconds,
                "confirmed_fill_events": self.confirmed_fill_events,
                "fair_play_allowed": self.fair_play_allowed,
                "fair_play_reason": self.fair_play_reason,
                "fair_play_latched": self.fair_play_latched,
                "fair_play_blocked_intents_count": (
                    self.fair_play_blocked_intents_count
                ),
                "short_window_round_trip_count": (
                    self.short_window_round_trip_count
                ),
                "near_flat_cycle_count": self.near_flat_cycle_count,
                "risk_exit_enabled": self.risk_exit_enabled,
                "risk_exit_intents_count": self.risk_exit_intents_count,
                "risk_exit_fills_count": self.risk_exit_fills_count,
                "risk_exit_reason": self.risk_exit_reason,
                "trade_intent_events": self.trade_intent_events,
                "generated_intent_purpose_counts": (
                    self.generated_intent_purpose_counts
                ),
                "confirmed_fill_purpose_counts": (
                    self.confirmed_fill_purpose_counts
                ),
                "signal_state": self.signal_state,
                "signal_reason": self.signal_reason,
                "signal_sample_count": self.signal_sample_count,
                "signal_spread_bps": self.signal_spread_bps,
                "signal_bid_depth": self.signal_bid_depth,
                "signal_ask_depth": self.signal_ask_depth,
                "signal_depth_imbalance": self.signal_depth_imbalance,
                "signal_microprice": self.signal_microprice,
                "signal_microprice_edge_bps": self.signal_microprice_edge_bps,
                "signal_one_step_return_bps": self.signal_one_step_return_bps,
                "signal_rolling_momentum_bps": self.signal_rolling_momentum_bps,
                "signal_confidence": self.signal_confidence,
                "depth_imbalance_l1": self.depth_imbalance_l1,
                "depth_imbalance_l2": self.depth_imbalance_l2,
                "depth_imbalance_l3": self.depth_imbalance_l3,
                "depth_imbalance_l5": self.depth_imbalance_l5,
                "depth_imbalance_l10": self.depth_imbalance_l10,
                "depth_bid_l1": self.depth_bid_l1,
                "depth_ask_l1": self.depth_ask_l1,
                "depth_bid_l5": self.depth_bid_l5,
                "depth_ask_l5": self.depth_ask_l5,
                "l1_edge_sign_consistent": self.l1_edge_sign_consistent,
                "ask_depth_concentration_l2_to_l5": (
                    self.ask_depth_concentration_l2_to_l5
                ),
                "bid_depth_concentration_l2_to_l5": (
                    self.bid_depth_concentration_l2_to_l5
                ),
                "intents_count": self.intents_count,
                "decisions_count": self.decisions_count,
                "fills_count": self.fills_count,
                "submitted_orders_count": self.submitted_orders_count,
                "open_orders_count": self.open_orders_count,
                "cash_balance": self.cash_balance,
                "base_position": self.base_position,
                "equity": self.equity,
                "peak_equity": self.peak_equity,
                "realized_pnl": self.realized_pnl,
                "unrealized_pnl": self.unrealized_pnl,
                "drawdown": self.drawdown,
                "total_volume": self.total_volume,
                "weekly_volume": self.weekly_volume,
                "estimated_score": self.estimated_score,
                "raffle_tickets": self.raffle_tickets,
                "notes": self.notes,
                "sequence_number": self.sequence_number,
                "run_fingerprint": self.run_fingerprint,
                "fees_paid": self.fees_paid,
                "reserved_exposure": self.reserved_exposure,
                "projected_shocked_drawdown": self.projected_shocked_drawdown,
                "preemptive_drawdown": self.preemptive_drawdown,
                "preemptive_halt_latched": self.preemptive_halt_latched,
                "hard_kill_latched": self.hard_kill_latched,
                "gap_risk_assumptions_available": self.gap_risk_assumptions_available,
            }
        )


class PaperRunRecorder:
    """
    Stores paper trading loop observations.

    JSONL format is used because:
    - one line = one loop iteration
    - easy to append
    - easy to inspect manually
    - easy to analyze later with Python/pandas
    """

    def __init__(self):
        self.records: list[PaperRunRecord] = []

    def append(self, record: PaperRunRecord) -> None:
        self.records.append(record)

    def append_jsonl(
        self,
        path: str | Path,
        record: PaperRunRecord,
        *,
        sync_to_disk: bool = False,
    ) -> Path:
        output_path = Path(path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        with output_path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(record.to_dict(), ensure_ascii=False))
            file.write("\n")
            file.flush()

            if sync_to_disk:
                os.fsync(file.fileno())

        self.records.append(record)

        return output_path

    def write_jsonl(self, path: str | Path) -> Path:
        output_path = Path(path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        with output_path.open("w", encoding="utf-8") as file:
            for record in self.records:
                file.write(json.dumps(record.to_dict(), ensure_ascii=False))
                file.write("\n")

        return output_path

    @property
    def count(self) -> int:
        return len(self.records)

    @property
    def latest(self) -> PaperRunRecord | None:
        if not self.records:
            return None

        return self.records[-1]
