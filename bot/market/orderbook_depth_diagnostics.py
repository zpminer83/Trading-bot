from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from bot.market.models import OrderBook


_ZERO_TOLERANCE = Decimal("1e-18")


@dataclass(frozen=True)
class OrderBookDepthDiagnostics:
    """Immutable, telemetry-only depth measurements for one order-book snapshot."""

    timestamp: datetime
    symbol: str
    bid_level_count: int
    ask_level_count: int
    bid_depth_l1: Decimal | None
    ask_depth_l1: Decimal | None
    imbalance_l1: Decimal | None
    bid_depth_l2: Decimal | None
    ask_depth_l2: Decimal | None
    imbalance_l2: Decimal | None
    bid_depth_l3: Decimal | None
    ask_depth_l3: Decimal | None
    imbalance_l3: Decimal | None
    bid_depth_l5: Decimal | None
    ask_depth_l5: Decimal | None
    imbalance_l5: Decimal | None
    bid_depth_l10: Decimal | None
    ask_depth_l10: Decimal | None
    imbalance_l10: Decimal | None
    best_bid_quantity: Decimal | None
    best_ask_quantity: Decimal | None
    microprice_edge_bps: Decimal | None
    l1_edge_sign_consistent: bool | None
    ask_depth_concentration_l2_to_l5: Decimal | None
    bid_depth_concentration_l2_to_l5: Decimal | None

    @classmethod
    def from_orderbook(
        cls,
        orderbook: OrderBook,
        observed_at: datetime | None = None,
    ) -> "OrderBookDepthDiagnostics":
        return calculate_orderbook_depth_diagnostics(orderbook, observed_at)


def calculate_orderbook_depth_diagnostics(
    orderbook: OrderBook,
    observed_at: datetime | None = None,
) -> OrderBookDepthDiagnostics:
    timestamp = _to_utc(observed_at)
    bid_levels = orderbook.bids
    ask_levels = orderbook.asks
    depths: dict[int, tuple[Decimal | None, Decimal | None, Decimal | None]] = {}
    for level_count in (1, 2, 3, 5, 10):
        bid_depth = _depth(bid_levels, level_count)
        ask_depth = _depth(ask_levels, level_count)
        depths[level_count] = (
            bid_depth,
            ask_depth,
            _imbalance(bid_depth, ask_depth),
        )

    best_bid_quantity = bid_levels[0].quantity if bid_levels else None
    best_ask_quantity = ask_levels[0].quantity if ask_levels else None
    edge = _microprice_edge(orderbook, best_bid_quantity, best_ask_quantity)
    l1_consistent = _sign_consistent(depths[1][2], edge)

    bid_depth_l5 = depths[5][0]
    ask_depth_l5 = depths[5][1]
    bid_depth_l2_to_l5 = _tail_depth(bid_levels, 5)
    ask_depth_l2_to_l5 = _tail_depth(ask_levels, 5)
    bid_concentration = _concentration(bid_depth_l2_to_l5, bid_depth_l5)
    ask_concentration = _concentration(ask_depth_l2_to_l5, ask_depth_l5)

    return OrderBookDepthDiagnostics(
        timestamp=timestamp,
        symbol=orderbook.symbol,
        bid_level_count=len(bid_levels),
        ask_level_count=len(ask_levels),
        bid_depth_l1=depths[1][0],
        ask_depth_l1=depths[1][1],
        imbalance_l1=depths[1][2],
        bid_depth_l2=depths[2][0],
        ask_depth_l2=depths[2][1],
        imbalance_l2=depths[2][2],
        bid_depth_l3=depths[3][0],
        ask_depth_l3=depths[3][1],
        imbalance_l3=depths[3][2],
        bid_depth_l5=depths[5][0],
        ask_depth_l5=depths[5][1],
        imbalance_l5=depths[5][2],
        bid_depth_l10=depths[10][0],
        ask_depth_l10=depths[10][1],
        imbalance_l10=depths[10][2],
        best_bid_quantity=best_bid_quantity,
        best_ask_quantity=best_ask_quantity,
        microprice_edge_bps=edge,
        l1_edge_sign_consistent=l1_consistent,
        ask_depth_concentration_l2_to_l5=ask_concentration,
        bid_depth_concentration_l2_to_l5=bid_concentration,
    )


# Concise aliases for callers that prefer a verb rather than a noun.
compute_orderbook_depth_diagnostics = calculate_orderbook_depth_diagnostics
build_orderbook_depth_diagnostics = calculate_orderbook_depth_diagnostics


def _depth(levels, count: int) -> Decimal | None:
    if not levels:
        return None
    return sum((level.quantity for level in levels[:count]), Decimal("0"))


def _tail_depth(levels, count: int) -> Decimal | None:
    if not levels:
        return None
    return sum((level.quantity for level in levels[1:count]), Decimal("0"))


def _imbalance(bid: Decimal | None, ask: Decimal | None) -> Decimal | None:
    if bid is None or ask is None:
        return None
    total = bid + ask
    if total == 0:
        return None
    return (bid - ask) / total


def _concentration(tail: Decimal | None, total: Decimal | None) -> Decimal | None:
    if tail is None or total is None or total == 0:
        return None
    return tail / total


def _microprice_edge(
    orderbook: OrderBook,
    best_bid_quantity: Decimal | None,
    best_ask_quantity: Decimal | None,
) -> Decimal | None:
    if not orderbook.bids or not orderbook.asks:
        return None
    if best_bid_quantity is None or best_ask_quantity is None:
        return None
    best_bid = orderbook.bids[0].price
    best_ask = orderbook.asks[0].price
    total_quantity = best_bid_quantity + best_ask_quantity
    midpoint = (best_bid + best_ask) / Decimal("2")
    if total_quantity == 0 or midpoint <= 0:
        return None
    microprice = (
        best_ask * best_bid_quantity + best_bid * best_ask_quantity
    ) / total_quantity
    return (microprice - midpoint) / midpoint * Decimal("10000")


def _sign_consistent(imbalance: Decimal | None, edge: Decimal | None) -> bool | None:
    if imbalance is None or edge is None:
        return None
    imbalance_sign = _sign(imbalance)
    edge_sign = _sign(edge)
    return imbalance_sign == edge_sign


def _sign(value: Decimal) -> int:
    if abs(value) <= _ZERO_TOLERANCE:
        return 0
    return 1 if value > 0 else -1


def _to_utc(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
