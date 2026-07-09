from decimal import Decimal, InvalidOperation
from typing import Any

from bot.market.market_cache import MarketCache
from bot.market.models import OrderBook, OrderBookLevel


class DreamDexMarketAdapter:
    """
    Converts raw DreamDEX market payloads into internal bot market models.

    This adapter is intentionally defensive:
    - supports REST-like and WS-like payload shapes
    - supports list levels: ["1.00", "5"]
    - supports dict levels: {"price": "1.00", "quantity": "5"}
    - sorts bids descending and asks ascending
    - skips empty / zero-size levels
    """

    @classmethod
    def parse_orderbook(
        cls,
        payload: dict[str, Any],
        default_symbol: str | None = None,
    ) -> OrderBook:
        data = cls._flatten_payload(payload)

        symbol = cls._extract_symbol(
            data=data,
            default_symbol=default_symbol,
        )

        bids = cls._parse_levels(data.get("bids", []))
        asks = cls._parse_levels(data.get("asks", []))

        bids.sort(key=lambda level: level.price, reverse=True)
        asks.sort(key=lambda level: level.price)

        timestamp = cls._extract_timestamp(data)
        nonce = cls._extract_nonce(data)

        return OrderBook(
            symbol=symbol,
            bids=bids,
            asks=asks,
            timestamp=timestamp,
            nonce=nonce,
        )

    @classmethod
    def update_cache_from_orderbook(
        cls,
        market_cache: MarketCache,
        payload: dict[str, Any],
        default_symbol: str | None = None,
    ) -> OrderBook:
        orderbook = cls.parse_orderbook(
            payload=payload,
            default_symbol=default_symbol,
        )

        market_cache.update_orderbook(orderbook)

        return orderbook

    @classmethod
    def _flatten_payload(cls, payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError("orderbook payload must be a dictionary")

        data = dict(payload)

        for key in ("data", "result", "orderbook", "book"):
            nested = data.get(key)

            if isinstance(nested, dict):
                data = {
                    **data,
                    **nested,
                }

        return data

    @classmethod
    def _extract_symbol(
        cls,
        data: dict[str, Any],
        default_symbol: str | None,
    ) -> str:
        raw_symbol = (
            data.get("symbol")
            or data.get("market")
            or data.get("pair")
            or default_symbol
        )

        if not raw_symbol:
            raise ValueError("orderbook symbol is missing")

        return str(raw_symbol)

    @classmethod
    def _parse_levels(cls, raw_levels: Any) -> list[OrderBookLevel]:
        levels: list[OrderBookLevel] = []

        if raw_levels is None:
            return levels

        if isinstance(raw_levels, dict):
            iterable = raw_levels.items()
        elif isinstance(raw_levels, list | tuple):
            iterable = raw_levels
        else:
            raise ValueError("orderbook levels must be a list, tuple, or dictionary")

        for raw_level in iterable:
            level = cls._parse_level(raw_level)

            if level.quantity <= 0:
                continue

            if level.price <= 0:
                continue

            levels.append(level)

        return levels

    @classmethod
    def _parse_level(cls, raw_level: Any) -> OrderBookLevel:
        if isinstance(raw_level, dict):
            price = cls._get_required_value(
                raw_level,
                keys=("price", "p"),
                label="price",
            )

            quantity = cls._get_required_value(
                raw_level,
                keys=("quantity", "qty", "size", "amount", "q"),
                label="quantity",
            )

            return OrderBookLevel(
                price=cls._to_decimal(price),
                quantity=cls._to_decimal(quantity),
            )

        if isinstance(raw_level, tuple) and len(raw_level) == 2:
            first, second = raw_level

            if not isinstance(first, int | float | str | Decimal):
                price, quantity = second
            else:
                price, quantity = first, second

            return OrderBookLevel(
                price=cls._to_decimal(price),
                quantity=cls._to_decimal(quantity),
            )

        if isinstance(raw_level, list) and len(raw_level) >= 2:
            return OrderBookLevel(
                price=cls._to_decimal(raw_level[0]),
                quantity=cls._to_decimal(raw_level[1]),
            )

        raise ValueError(f"invalid orderbook level: {raw_level}")

    @classmethod
    def _get_required_value(
        cls,
        data: dict[str, Any],
        keys: tuple[str, ...],
        label: str,
    ) -> Any:
        for key in keys:
            if key in data:
                return data[key]

        raise ValueError(f"orderbook level {label} is missing")

    @classmethod
    def _to_decimal(cls, value: Any) -> Decimal:
        try:
            return Decimal(str(value))
        except (InvalidOperation, ValueError) as exc:
            raise ValueError(f"invalid decimal value: {value}") from exc

    @classmethod
    def _extract_timestamp(cls, data: dict[str, Any]) -> int:
        raw_timestamp = (
            data.get("timestamp")
            or data.get("ts")
            or data.get("time")
            or data.get("updatedAt")
            or 0
        )

        try:
            return int(raw_timestamp)
        except (TypeError, ValueError):
            return 0

    @classmethod
    def _extract_nonce(cls, data: dict[str, Any]) -> str:
        raw_nonce = (
            data.get("nonce")
            or data.get("sequence")
            or data.get("seq")
            or data.get("lastUpdateId")
            or ""
        )

        return str(raw_nonce)