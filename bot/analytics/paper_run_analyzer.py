import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class PaperRunSummary:
    records_count: int

    first_timestamp: datetime
    last_timestamp: datetime
    duration_seconds: int

    successful_iterations: int
    failed_iterations: int
    success_rate: Decimal
    error_type_counts: dict[str, int]
    max_consecutive_failures: int

    safe_market_count: int
    unsafe_market_count: int
    unknown_market_count: int

    fresh_market_count: int
    stale_market_count: int
    unknown_freshness_count: int
    max_exchange_age_seconds: Decimal | None
    max_unchanged_seconds: Decimal | None
    freshness_reason_counts: dict[str, int]

    risk_allowed_count: int
    risk_blocked_count: int
    unknown_risk_count: int
    kill_switch_triggered: bool
    risk_reason_counts: dict[str, int]
    maximum_recorded_drawdown: Decimal | None

    evaluated_open_orders_count: int
    orders_at_touch_count: int
    crossed_order_count: int
    level_quantity_decreased_count: int
    level_disappeared_count: int
    maximum_open_order_age_seconds: Decimal | None

    confirmed_fill_event_count: int
    buy_fill_count: int
    sell_fill_count: int
    short_window_round_trip_count: int
    near_flat_cycle_count: int
    fair_play_allowed_count: int
    fair_play_blocked_count: int
    unknown_fair_play_count: int
    fair_play_latched: bool
    fair_play_blocked_intents_count: int
    minimum_opposite_fill_delay_seconds: Decimal | None
    maximum_opposite_fill_delay_seconds: Decimal | None
    fair_play_reason_counts: dict[str, int]

    generated_intent_count: int
    submitted_intent_count: int
    fair_play_rejected_intent_count: int
    execution_rejected_intent_count: int
    generated_intent_purpose_counts: dict[str, int]
    confirmed_fill_purpose_counts: dict[str, int]
    unknown_purpose_intent_count: int
    unknown_purpose_fill_count: int

    bullish_signal_count: int
    bearish_signal_count: int
    neutral_signal_count: int
    warming_up_signal_count: int
    unavailable_signal_count: int
    unknown_signal_count: int
    maximum_signal_confidence: Decimal | None
    average_signal_confidence: Decimal | None
    minimum_depth_imbalance: Decimal | None
    maximum_depth_imbalance: Decimal | None
    minimum_rolling_momentum_bps: Decimal | None
    maximum_rolling_momentum_bps: Decimal | None
    average_spread_bps: Decimal | None
    signal_reason_counts: dict[str, int]

    positive_imbalance_l1_count: int
    negative_imbalance_l1_count: int
    positive_imbalance_l5_count: int
    negative_imbalance_l5_count: int
    l1_positive_l5_negative_count: int
    l1_negative_l5_positive_count: int
    sign_consistency_failure_count: int
    average_imbalance_l1: Decimal | None
    average_imbalance_l5: Decimal | None
    average_bid_depth_concentration_l2_to_l5: Decimal | None
    average_ask_depth_concentration_l2_to_l5: Decimal | None

    fills_count: int
    submitted_orders_count: int

    final_cash_balance: Decimal
    final_base_position: Decimal
    final_equity: Decimal
    final_realized_pnl: Decimal
    final_unrealized_pnl: Decimal
    final_total_volume: Decimal

    final_weekly_volume: Decimal
    final_estimated_score: Decimal
    final_raffle_tickets: int
    final_open_orders: int

    max_drawdown: Decimal
    min_mid_price: Decimal | None
    max_mid_price: Decimal | None


class PaperRunAnalyzer:
    """
    Reads JSONL files produced by PaperRunRecorder
    and generates a compact paper-trading summary.
    """

    def analyze_file(self, path: str | Path) -> PaperRunSummary:
        records = self.load_jsonl(path)
        return self.analyze_records(records)

    def load_jsonl(self, path: str | Path) -> list[dict[str, Any]]:
        input_path = Path(path)

        if not input_path.exists():
            raise FileNotFoundError(
                f"paper run file does not exist: {input_path}"
            )

        if not input_path.is_file():
            raise ValueError(
                f"paper run path is not a file: {input_path}"
            )

        records: list[dict[str, Any]] = []

        with input_path.open("r", encoding="utf-8") as file:
            for line_number, raw_line in enumerate(file, start=1):
                line = raw_line.strip()

                if not line:
                    continue

                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(
                        f"invalid JSON on line {line_number}: {exc.msg}"
                    ) from exc

                if not isinstance(record, dict):
                    raise ValueError(
                        f"record on line {line_number} must be a JSON object"
                    )

                records.append(record)

        if not records:
            raise ValueError("paper run file contains no records")

        return records

    def analyze_records(
        self,
        records: list[dict[str, Any]],
    ) -> PaperRunSummary:
        if not records:
            raise ValueError("records must not be empty")

        timestamps = [
            self._parse_timestamp(record.get("timestamp"))
            for record in records
        ]

        first_timestamp = timestamps[0]
        last_timestamp = timestamps[-1]

        duration_seconds = max(
            0,
            int((last_timestamp - first_timestamp).total_seconds()),
        )

        failed_iterations = sum(
            1
            for record in records
            if record.get("iteration_ok") is False
        )

        successful_iterations = len(records) - failed_iterations
        success_rate = Decimal(successful_iterations) / Decimal(len(records))

        error_type_counts: dict[str, int] = {}

        for record in records:
            if record.get("iteration_ok") is not False:
                continue

            error_type = record.get("error_type")

            if error_type is None:
                continue

            error_type_text = str(error_type).strip()

            if not error_type_text:
                continue

            error_type_counts[error_type_text] = (
                error_type_counts.get(error_type_text, 0) + 1
            )

        max_consecutive_failures = max(
            (
                self._to_int(record.get("consecutive_failures"))
                for record in records
            ),
            default=0,
        )

        safe_market_count = sum(
            1
            for record in records
            if record.get("market_safe") is True
        )

        unsafe_market_count = sum(
            1
            for record in records
            if record.get("market_safe") is False
        )

        unknown_market_count = (
            len(records)
            - safe_market_count
            - unsafe_market_count
        )

        fresh_market_count = sum(
            1
            for record in records
            if record.get("market_fresh") is True
        )

        stale_market_count = sum(
            1
            for record in records
            if record.get("market_fresh") is False
        )

        unknown_freshness_count = (
            len(records)
            - fresh_market_count
            - stale_market_count
        )

        exchange_ages = [
            self._to_decimal(record.get("exchange_age_seconds"))
            for record in records
            if record.get("exchange_age_seconds") is not None
        ]

        unchanged_durations = [
            self._to_decimal(record.get("unchanged_seconds"))
            for record in records
            if record.get("unchanged_seconds") is not None
        ]

        freshness_reason_counts: dict[str, int] = {}

        for record in records:
            reason = record.get("market_freshness_reason")

            if reason is None:
                continue

            reason_text = str(reason)
            freshness_reason_counts[reason_text] = (
                freshness_reason_counts.get(reason_text, 0) + 1
            )

        risk_allowed_count = sum(
            1
            for record in records
            if record.get("portfolio_risk_allowed") is True
        )

        risk_blocked_count = sum(
            1
            for record in records
            if record.get("portfolio_risk_allowed") is False
        )

        unknown_risk_count = (
            len(records)
            - risk_allowed_count
            - risk_blocked_count
        )

        kill_switch_triggered = any(
            record.get("portfolio_risk_latched") is True
            for record in records
        )

        risk_reason_counts: dict[str, int] = {}

        for record in records:
            reason = record.get("portfolio_risk_reason")

            if reason is None:
                continue

            reason_text = str(reason).strip()

            if not reason_text:
                continue

            risk_reason_counts[reason_text] = (
                risk_reason_counts.get(reason_text, 0) + 1
            )

        recorded_risk_drawdowns = [
            self._to_decimal(record.get("risk_drawdown"))
            for record in records
            if record.get("risk_drawdown") is not None
        ]

        evaluated_open_orders_count = sum(
            self._to_int(record.get("evaluated_open_orders_count"))
            for record in records
        )
        orders_at_touch_count = sum(
            self._to_int(record.get("orders_at_touch_count"))
            for record in records
        )
        crossed_order_count = sum(
            self._to_int(record.get("crossed_order_count"))
            for record in records
        )
        level_quantity_decreased_count = sum(
            self._to_int(record.get("level_quantity_decreased_count"))
            for record in records
        )
        level_disappeared_count = sum(
            self._to_int(record.get("level_disappeared_count"))
            for record in records
        )
        open_order_ages = [
            self._to_decimal(record.get("max_open_order_age_seconds"))
            for record in records
            if record.get("max_open_order_age_seconds") is not None
        ]

        confirmed_fill_events: list[dict[str, Any]] = []
        for record in records:
            raw_events = record.get("confirmed_fill_events")
            if raw_events is None:
                continue
            if not isinstance(raw_events, list):
                raise ValueError("confirmed_fill_events must be a list")
            for event in raw_events:
                if not isinstance(event, dict):
                    raise ValueError("confirmed fill event must be a JSON object")
                confirmed_fill_events.append(event)

        buy_fill_count = sum(
            1
            for event in confirmed_fill_events
            if str(event.get("side", "")).lower() == "buy"
        )
        sell_fill_count = sum(
            1
            for event in confirmed_fill_events
            if str(event.get("side", "")).lower() == "sell"
        )
        opposite_fill_delays = [
            self._to_decimal(event.get("seconds_since_opposite_fill"))
            for event in confirmed_fill_events
            if event.get("seconds_since_opposite_fill") is not None
        ]
        short_window_round_trip_count = max(
            (
                self._to_int(record.get("short_window_round_trip_count"))
                for record in records
            ),
            default=0,
        )
        near_flat_cycle_count = max(
            (
                self._to_int(record.get("near_flat_cycle_count"))
                for record in records
            ),
            default=0,
        )
        fair_play_allowed_count = sum(
            1 for record in records if record.get("fair_play_allowed") is True
        )
        fair_play_blocked_count = sum(
            1 for record in records if record.get("fair_play_allowed") is False
        )
        unknown_fair_play_count = (
            len(records) - fair_play_allowed_count - fair_play_blocked_count
        )
        fair_play_latched = any(
            record.get("fair_play_latched") is True for record in records
        )
        fair_play_blocked_intents_count = sum(
            self._to_int(record.get("fair_play_blocked_intents_count"))
            for record in records
        )
        fair_play_reason_counts: dict[str, int] = {}
        for record in records:
            reason = record.get("fair_play_reason")
            if reason is None:
                continue
            reason_text = str(reason).strip()
            if reason_text:
                fair_play_reason_counts[reason_text] = (
                    fair_play_reason_counts.get(reason_text, 0) + 1
                )

        trade_intent_events: list[dict[str, Any]] = []
        for record in records:
            raw_events = record.get("trade_intent_events")
            if raw_events is None:
                continue
            if not isinstance(raw_events, list):
                raise ValueError("trade_intent_events must be a list")
            for event in raw_events:
                if not isinstance(event, dict):
                    raise ValueError("trade intent event must be a JSON object")
                trade_intent_events.append(event)

        generated_intent_purpose_counts: dict[str, int] = {}
        for event in trade_intent_events:
            purpose = str(event.get("purpose") or "unknown").strip() or "unknown"
            generated_intent_purpose_counts[purpose] = (
                generated_intent_purpose_counts.get(purpose, 0) + 1
            )
        confirmed_fill_purpose_counts: dict[str, int] = {}
        for event in confirmed_fill_events:
            purpose = str(event.get("purpose") or "unknown").strip() or "unknown"
            confirmed_fill_purpose_counts[purpose] = (
                confirmed_fill_purpose_counts.get(purpose, 0) + 1
            )
        generated_intent_count = len(trade_intent_events)
        submitted_intent_count = sum(
            event.get("submitted") is True for event in trade_intent_events
        )
        fair_play_rejected_intent_count = sum(
            event.get("fair_play_allowed") is False
            for event in trade_intent_events
        )
        execution_rejected_intent_count = sum(
            event.get("execution_approved") is False
            for event in trade_intent_events
        )

        signal_state_counts = {
            state: sum(
                str(record.get("signal_state", "")).lower() == state
                for record in records
            )
            for state in (
                "bullish",
                "bearish",
                "neutral",
                "warming_up",
                "unavailable",
            )
        }
        known_signal_count = sum(signal_state_counts.values())
        signal_confidences = [
            self._to_decimal(record.get("signal_confidence"))
            for record in records
            if record.get("signal_confidence") is not None
        ]
        depth_imbalances = [
            self._to_decimal(record.get("signal_depth_imbalance"))
            for record in records
            if record.get("signal_depth_imbalance") is not None
        ]
        rolling_momentums = [
            self._to_decimal(record.get("signal_rolling_momentum_bps"))
            for record in records
            if record.get("signal_rolling_momentum_bps") is not None
        ]
        signal_spreads = [
            self._to_decimal(record.get("signal_spread_bps"))
            for record in records
            if record.get("signal_spread_bps") is not None
        ]
        signal_reason_counts: dict[str, int] = {}
        for record in records:
            reason = record.get("signal_reason")
            if reason is None:
                continue
            reason_text = str(reason).strip()
            if reason_text:
                signal_reason_counts[reason_text] = (
                    signal_reason_counts.get(reason_text, 0) + 1
                )

        depth_l1 = [
            self._to_decimal(record.get("depth_imbalance_l1"))
            for record in records
            if record.get("depth_imbalance_l1") is not None
        ]
        depth_l5 = [
            self._to_decimal(record.get("depth_imbalance_l5"))
            for record in records
            if record.get("depth_imbalance_l5") is not None
        ]
        bid_concentrations = [
            self._to_decimal(record.get("bid_depth_concentration_l2_to_l5"))
            for record in records
            if record.get("bid_depth_concentration_l2_to_l5") is not None
        ]
        ask_concentrations = [
            self._to_decimal(record.get("ask_depth_concentration_l2_to_l5"))
            for record in records
            if record.get("ask_depth_concentration_l2_to_l5") is not None
        ]
        positive_imbalance_l1_count = sum(value > 0 for value in depth_l1)
        negative_imbalance_l1_count = sum(value < 0 for value in depth_l1)
        positive_imbalance_l5_count = sum(value > 0 for value in depth_l5)
        negative_imbalance_l5_count = sum(value < 0 for value in depth_l5)
        l1_positive_l5_negative_count = sum(
            self._to_decimal(record.get("depth_imbalance_l1")) > 0
            and self._to_decimal(record.get("depth_imbalance_l5")) < 0
            for record in records
            if record.get("depth_imbalance_l1") is not None
            and record.get("depth_imbalance_l5") is not None
        )
        l1_negative_l5_positive_count = sum(
            self._to_decimal(record.get("depth_imbalance_l1")) < 0
            and self._to_decimal(record.get("depth_imbalance_l5")) > 0
            for record in records
            if record.get("depth_imbalance_l1") is not None
            and record.get("depth_imbalance_l5") is not None
        )
        sign_consistency_failure_count = sum(
            record.get("l1_edge_sign_consistent") is False for record in records
        )

        fills_count = sum(
            self._to_int(record.get("fills_count"))
            for record in records
        )

        submitted_orders_count = sum(
            self._to_int(record.get("submitted_orders_count"))
            for record in records
        )

        drawdowns = [
            self._to_decimal(record.get("drawdown"))
            for record in records
        ]

        mid_prices = [
            self._to_decimal(record.get("mid_price"))
            for record in records
            if record.get("mid_price") is not None
        ]

        final_record = records[-1]

        return PaperRunSummary(
            records_count=len(records),
            first_timestamp=first_timestamp,
            last_timestamp=last_timestamp,
            duration_seconds=duration_seconds,
            successful_iterations=successful_iterations,
            failed_iterations=failed_iterations,
            success_rate=success_rate,
            error_type_counts=error_type_counts,
            max_consecutive_failures=max_consecutive_failures,
            safe_market_count=safe_market_count,
            unsafe_market_count=unsafe_market_count,
            unknown_market_count=unknown_market_count,
            fresh_market_count=fresh_market_count,
            stale_market_count=stale_market_count,
            unknown_freshness_count=unknown_freshness_count,
            max_exchange_age_seconds=(
                max(exchange_ages) if exchange_ages else None
            ),
            max_unchanged_seconds=(
                max(unchanged_durations) if unchanged_durations else None
            ),
            freshness_reason_counts=freshness_reason_counts,
            risk_allowed_count=risk_allowed_count,
            risk_blocked_count=risk_blocked_count,
            unknown_risk_count=unknown_risk_count,
            kill_switch_triggered=kill_switch_triggered,
            risk_reason_counts=risk_reason_counts,
            maximum_recorded_drawdown=(
                max(recorded_risk_drawdowns)
                if recorded_risk_drawdowns
                else None
            ),
            evaluated_open_orders_count=evaluated_open_orders_count,
            orders_at_touch_count=orders_at_touch_count,
            crossed_order_count=crossed_order_count,
            level_quantity_decreased_count=level_quantity_decreased_count,
            level_disappeared_count=level_disappeared_count,
            maximum_open_order_age_seconds=(
                max(open_order_ages) if open_order_ages else None
            ),
            confirmed_fill_event_count=len(confirmed_fill_events),
            buy_fill_count=buy_fill_count,
            sell_fill_count=sell_fill_count,
            short_window_round_trip_count=short_window_round_trip_count,
            near_flat_cycle_count=near_flat_cycle_count,
            fair_play_allowed_count=fair_play_allowed_count,
            fair_play_blocked_count=fair_play_blocked_count,
            unknown_fair_play_count=unknown_fair_play_count,
            fair_play_latched=fair_play_latched,
            fair_play_blocked_intents_count=fair_play_blocked_intents_count,
            minimum_opposite_fill_delay_seconds=(
                min(opposite_fill_delays) if opposite_fill_delays else None
            ),
            maximum_opposite_fill_delay_seconds=(
                max(opposite_fill_delays) if opposite_fill_delays else None
            ),
            fair_play_reason_counts=fair_play_reason_counts,
            generated_intent_count=generated_intent_count,
            submitted_intent_count=submitted_intent_count,
            fair_play_rejected_intent_count=fair_play_rejected_intent_count,
            execution_rejected_intent_count=execution_rejected_intent_count,
            generated_intent_purpose_counts=generated_intent_purpose_counts,
            confirmed_fill_purpose_counts=confirmed_fill_purpose_counts,
            unknown_purpose_intent_count=generated_intent_purpose_counts.get(
                "unknown",
                0,
            ),
            unknown_purpose_fill_count=confirmed_fill_purpose_counts.get(
                "unknown",
                0,
            ),
            bullish_signal_count=signal_state_counts["bullish"],
            bearish_signal_count=signal_state_counts["bearish"],
            neutral_signal_count=signal_state_counts["neutral"],
            warming_up_signal_count=signal_state_counts["warming_up"],
            unavailable_signal_count=signal_state_counts["unavailable"],
            unknown_signal_count=len(records) - known_signal_count,
            maximum_signal_confidence=(
                max(signal_confidences) if signal_confidences else None
            ),
            average_signal_confidence=(
                sum(signal_confidences, Decimal("0")) / Decimal(len(signal_confidences))
                if signal_confidences
                else None
            ),
            minimum_depth_imbalance=(
                min(depth_imbalances) if depth_imbalances else None
            ),
            maximum_depth_imbalance=(
                max(depth_imbalances) if depth_imbalances else None
            ),
            minimum_rolling_momentum_bps=(
                min(rolling_momentums) if rolling_momentums else None
            ),
            maximum_rolling_momentum_bps=(
                max(rolling_momentums) if rolling_momentums else None
            ),
            average_spread_bps=(
                sum(signal_spreads, Decimal("0")) / Decimal(len(signal_spreads))
                if signal_spreads
                else None
            ),
            signal_reason_counts=signal_reason_counts,
            positive_imbalance_l1_count=positive_imbalance_l1_count,
            negative_imbalance_l1_count=negative_imbalance_l1_count,
            positive_imbalance_l5_count=positive_imbalance_l5_count,
            negative_imbalance_l5_count=negative_imbalance_l5_count,
            l1_positive_l5_negative_count=l1_positive_l5_negative_count,
            l1_negative_l5_positive_count=l1_negative_l5_positive_count,
            sign_consistency_failure_count=sign_consistency_failure_count,
            average_imbalance_l1=(
                sum(depth_l1, Decimal("0")) / Decimal(len(depth_l1))
                if depth_l1 else None
            ),
            average_imbalance_l5=(
                sum(depth_l5, Decimal("0")) / Decimal(len(depth_l5))
                if depth_l5 else None
            ),
            average_bid_depth_concentration_l2_to_l5=(
                sum(bid_concentrations, Decimal("0")) / Decimal(len(bid_concentrations))
                if bid_concentrations else None
            ),
            average_ask_depth_concentration_l2_to_l5=(
                sum(ask_concentrations, Decimal("0")) / Decimal(len(ask_concentrations))
                if ask_concentrations else None
            ),
            fills_count=fills_count,
            submitted_orders_count=submitted_orders_count,
            final_cash_balance=self._to_decimal(
                final_record.get("cash_balance")
            ),
            final_base_position=self._to_decimal(
                final_record.get("base_position")
            ),
            final_equity=self._to_decimal(
                final_record.get("equity")
            ),
            final_realized_pnl=self._to_decimal(
                final_record.get("realized_pnl")
            ),
            final_unrealized_pnl=self._to_decimal(
                final_record.get("unrealized_pnl")
            ),
            final_total_volume=self._to_decimal(
                final_record.get("total_volume")
            ),
            final_weekly_volume=self._to_decimal(
                final_record.get("weekly_volume")
            ),
            final_estimated_score=self._to_decimal(
                final_record.get("estimated_score")
            ),
            final_raffle_tickets=self._to_int(
                final_record.get("raffle_tickets")
            ),
            final_open_orders=self._to_int(
                final_record.get("open_orders_count")
            ),
            max_drawdown=max(drawdowns, default=Decimal("0")),
            min_mid_price=min(mid_prices) if mid_prices else None,
            max_mid_price=max(mid_prices) if mid_prices else None,
        )

    @staticmethod
    def _parse_timestamp(value: Any) -> datetime:
        if value is None:
            raise ValueError("record timestamp is missing")

        if isinstance(value, datetime):
            return value

        text = str(value).strip()

        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"

        try:
            return datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValueError(
                f"invalid record timestamp: {value}"
            ) from exc

    @staticmethod
    def _to_decimal(
        value: Any,
        default: Decimal = Decimal("0"),
    ) -> Decimal:
        if value is None:
            return default

        try:
            return Decimal(str(value))
        except (InvalidOperation, ValueError, TypeError) as exc:
            raise ValueError(
                f"invalid decimal value in paper run: {value}"
            ) from exc

    @staticmethod
    def _to_int(
        value: Any,
        default: int = 0,
    ) -> int:
        if value is None:
            return default

        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"invalid integer value in paper run: {value}"
            ) from exc
