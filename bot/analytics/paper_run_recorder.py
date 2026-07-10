import json
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

    best_bid: Decimal | None = None
    best_ask: Decimal | None = None
    mid_price: Decimal | None = None
    spread: Decimal | None = None

    market_safe: bool | None = None
    market_safety_reason: str | None = None

    market_fresh: bool | None = None
    market_freshness_reason: str | None = None
    exchange_age_seconds: Decimal | None = None
    unchanged_seconds: Decimal | None = None

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

    def to_dict(self) -> dict[str, Any]:
        return serialize_value(
            {
                "timestamp": self.timestamp,
                "symbol": self.symbol,
                "best_bid": self.best_bid,
                "best_ask": self.best_ask,
                "mid_price": self.mid_price,
                "spread": self.spread,
                "market_safe": self.market_safe,
                "market_safety_reason": self.market_safety_reason,
                "market_fresh": self.market_fresh,
                "market_freshness_reason": self.market_freshness_reason,
                "exchange_age_seconds": self.exchange_age_seconds,
                "unchanged_seconds": self.unchanged_seconds,
                "intents_count": self.intents_count,
                "decisions_count": self.decisions_count,
                "fills_count": self.fills_count,
                "submitted_orders_count": self.submitted_orders_count,
                "open_orders_count": self.open_orders_count,
                "cash_balance": self.cash_balance,
                "base_position": self.base_position,
                "equity": self.equity,
                "realized_pnl": self.realized_pnl,
                "unrealized_pnl": self.unrealized_pnl,
                "drawdown": self.drawdown,
                "total_volume": self.total_volume,
                "weekly_volume": self.weekly_volume,
                "estimated_score": self.estimated_score,
                "raffle_tickets": self.raffle_tickets,
                "notes": self.notes,
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
