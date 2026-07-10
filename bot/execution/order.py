import re
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Literal


OrderSide = Literal["buy", "sell"]
OrderType = Literal["limit", "market"]


class OrderPurpose(str, Enum):
    ENTRY = "entry"
    TAKE_PROFIT = "take_profit"
    SIGNAL_EXIT = "signal_exit"
    STOP_LOSS = "stop_loss"
    RISK_REDUCTION = "risk_reduction"
    INVENTORY_REBALANCE = "inventory_rebalance"
    UNKNOWN = "unknown"


_SIGNAL_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SENSITIVE_SIGNAL_ID_PATTERN = re.compile(
    r"(?i)(authorization|bearer|api[_ -]?key|token|secret|password|"
    r"cookie|private[_ -]?key)"
)


@dataclass(frozen=True)
class OrderIntent:
    symbol: str
    side: OrderSide
    order_type: OrderType
    price: Decimal
    quantity: Decimal
    purpose: OrderPurpose = OrderPurpose.UNKNOWN
    strategy_name: str = "unknown"
    rationale: str | None = None
    signal_id: str | None = None

    def __post_init__(self) -> None:
        purpose = self.purpose
        if not isinstance(purpose, OrderPurpose):
            try:
                purpose = OrderPurpose(str(purpose).lower())
            except ValueError as exc:
                raise ValueError(f"unsupported order purpose: {self.purpose}") from exc
            object.__setattr__(self, "purpose", purpose)

        if not isinstance(self.strategy_name, str) or not self.strategy_name.strip():
            raise ValueError("strategy_name must be non-empty")

        if self.rationale is not None:
            if not isinstance(self.rationale, str):
                raise ValueError("rationale must be a string or None")
            if len(self.rationale) > 500:
                raise ValueError("rationale must be at most 500 characters")

        if self.signal_id is not None:
            if not isinstance(self.signal_id, str):
                raise ValueError("signal_id must be a string or None")
            if (
                not _SIGNAL_ID_PATTERN.fullmatch(self.signal_id)
                or _SENSITIVE_SIGNAL_ID_PATTERN.search(self.signal_id)
            ):
                raise ValueError("signal_id must be a safe stable identifier")

    @property
    def notional(self) -> Decimal:
        return self.price * self.quantity


@dataclass(frozen=True)
class OrderDecision:
    approved: bool
    reason: str
    intent: OrderIntent
