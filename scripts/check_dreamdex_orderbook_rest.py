import json
import os
from decimal import Decimal
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from bot.market.market_cache import MarketCache
from bot.market.market_data_service import MarketDataService


DEFAULT_BASE_URL = "https://api.dreamdex.io/v0"
DEFAULT_SYMBOL = "SOMI:USDso"
DEFAULT_DEPTH = 5


def fmt_decimal(value: Decimal | None, places: str = "0.000000") -> str:
    if value is None:
        return "n/a"

    quantized = value.quantize(Decimal(places))
    text = format(quantized, "f")

    return text.rstrip("0").rstrip(".") or "0"


def build_orderbook_url(
    base_url: str,
    symbol: str,
    depth: int,
) -> str:
    query = urlencode(
        {
            "symbols": symbol,
            "depth": depth,
        }
    )

    return f"{base_url.rstrip('/')}/orderbooks?{query}"


def fetch_json(url: str, timeout: int = 15) -> dict[str, Any]:
    request = Request(
        url=url,
        headers={
            "Accept": "application/json",
            "User-Agent": "TradingBotReadOnlyCheck/0.1",
        },
        method="GET",
    )

    try:
        with urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")

    except HTTPError as exc:
        response_body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"HTTP error {exc.code}: {response_body[:500]}"
        ) from exc

    except URLError as exc:
        raise RuntimeError(f"Network error: {exc.reason}") from exc

    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid JSON response: {body[:500]}") from exc

    if not isinstance(parsed, dict):
        raise RuntimeError("Expected JSON object response from DreamDEX")

    return parsed


def extract_orderbook_payload(
    response: dict[str, Any],
    symbol: str,
) -> dict[str, Any]:
    """
    Tries to extract a single orderbook payload from common response shapes.

    Supported examples:
    - {"symbol": "...", "bids": [...], "asks": [...]}
    - {"data": {"symbol": "...", "bids": [...], "asks": [...]}}
    - {"data": [{"symbol": "...", "bids": [...], "asks": [...]}]}
    - {"orderbooks": [{"symbol": "...", "bids": [...], "asks": [...]}]}
    - {"items": [...]}
    """

    if "bids" in response and "asks" in response:
        return response

    for key in ("data", "result", "orderbook", "book"):
        nested = response.get(key)

        if isinstance(nested, dict) and "bids" in nested and "asks" in nested:
            return nested

    for key in ("data", "result", "orderbooks", "items", "markets"):
        nested = response.get(key)

        if isinstance(nested, list):
            for item in nested:
                if not isinstance(item, dict):
                    continue

                item_symbol = (
                    item.get("symbol")
                    or item.get("market")
                    or item.get("pair")
                )

                if item_symbol == symbol and "bids" in item and "asks" in item:
                    return item

            for item in nested:
                if isinstance(item, dict) and "bids" in item and "asks" in item:
                    return item

    raise RuntimeError(
        "Could not locate orderbook data in response. "
        f"Top-level keys: {list(response.keys())}"
    )


def main() -> None:
    base_url = os.getenv("DREAMDEX_API_BASE_URL", DEFAULT_BASE_URL)
    symbol = os.getenv("DREAMDEX_SYMBOL", DEFAULT_SYMBOL)
    depth = int(os.getenv("DREAMDEX_DEPTH", str(DEFAULT_DEPTH)))

    url = build_orderbook_url(
        base_url=base_url,
        symbol=symbol,
        depth=depth,
    )

    print("=" * 70)
    print("DREAMDEX READ-ONLY ORDERBOOK CHECK")
    print("=" * 70)
    print(f"Base URL: {base_url}")
    print(f"Symbol  : {symbol}")
    print(f"Depth   : {depth}")
    print(f"URL     : {url}")
    print("=" * 70)

    response = fetch_json(url)

    print()
    print("Raw response:")
    print(f"Top-level keys: {list(response.keys())}")

    payload = extract_orderbook_payload(
        response=response,
        symbol=symbol,
    )

    market_cache = MarketCache()
    service = MarketDataService(market_cache=market_cache)

    snapshot = service.handle_orderbook_payload(
        payload=payload,
        default_symbol=symbol,
    )

    print()
    print("Parsed orderbook:")
    print(f"Symbol   : {snapshot.symbol}")
    print(f"Timestamp: {snapshot.orderbook.timestamp}")
    print(f"Nonce    : {snapshot.orderbook.nonce}")
    print(f"Bids     : {len(snapshot.orderbook.bids)}")
    print(f"Asks     : {len(snapshot.orderbook.asks)}")

    print()
    print("Market snapshot:")

    if snapshot.best_bid is not None:
        print(
            "Best bid : "
            f"{fmt_decimal(snapshot.best_bid.price, '0.000000')} "
            f"x {fmt_decimal(snapshot.best_bid.quantity, '0.000000')}"
        )
    else:
        print("Best bid : n/a")

    if snapshot.best_ask is not None:
        print(
            "Best ask : "
            f"{fmt_decimal(snapshot.best_ask.price, '0.000000')} "
            f"x {fmt_decimal(snapshot.best_ask.quantity, '0.000000')}"
        )
    else:
        print("Best ask : n/a")

    print(f"Mid price: {fmt_decimal(snapshot.mid_price, '0.000000')}")
    print(f"Spread   : {fmt_decimal(snapshot.spread, '0.000000')}")
    print("=" * 70)


if __name__ == "__main__":
    main()