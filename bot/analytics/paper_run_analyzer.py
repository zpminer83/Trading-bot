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

    safe_market_count: int
    unsafe_market_count: int
    unknown_market_count: int

    fresh_market_count: int
    stale_market_count: int
    unknown_freshness_count: int
    max_exchange_age_seconds: Decimal | None
    max_unchanged_seconds: Decimal | None
    freshness_reason_counts: dict[str, int]

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
