"""Offline/public-RPC indexing of the confirmed DreamDEX ``OrderFilled`` event.

The modern Bot Kit ABI deliberately exposes only the event fields that are
actually present on-chain.  In particular, ``owner``, ``isBid`` and
``userData`` are *not* part of ``OrderFilled`` and are never guessed here.
This module contains immutable records, a narrowly allow-listed RPC transport,
and a deterministic fixture transport.  It does not authenticate, sign, or
submit/cancel/replace anything.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import re
from typing import Any, Callable, Mapping, Protocol, Sequence


ORDER_FILLED_EVENT_SIGNATURE = (
    "OrderFilled(uint128,uint128,uint256,uint256,uint256,uint256)"
)
ORDER_FILLED_TOPIC = "0xc87f4223e9e7c4e4f39f9b34fc9d64d78cdb95d9035b3748cbde59521261a399"
ORDER_FILLED_INDEXED_FIELDS = ("takerOrderId", "makerOrderId")
ORDER_FILLED_NON_INDEXED_FIELDS = ("quantityFilled", "takerRemaining", "makerRemaining", "fillPrice")
ORDER_FILLED_ABSENT_FIELDS = ("owner", "isBid", "side", "userData")
SOMI_USDSO_SYMBOL = "SOMI:USDso"
SOMI_USDSO_POOL = "0x035de7403eac6872787779cca7ccf1b4cdb61379"
ALLOWED_RPC_METHODS = frozenset(
    {"eth_getLogs", "eth_blockNumber", "eth_getBlockByNumber", "eth_chainId"}
)


def _utc(value: Any = None, fallback: datetime | None = None) -> datetime:
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, (int, float)):
        result = datetime.fromtimestamp(float(value) / (1000 if value > 10_000_000_000 else 1), tz=timezone.utc)
    elif value:
        try:
            result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            result = fallback or datetime.now(timezone.utc)
    else:
        result = fallback or datetime.now(timezone.utc)
    return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result.astimezone(timezone.utc)


def _hex(value: Any, *, name: str) -> str:
    if not isinstance(value, str) or not value.startswith("0x"):
        raise ValueError(f"malformed_{name}")
    body = value[2:]
    if not body or len(body) % 2 or any(char not in "0123456789abcdefABCDEF" for char in body):
        raise ValueError(f"malformed_{name}")
    return "0x" + body.lower()


def _uint_hex(value: Any, *, name: str) -> int:
    if isinstance(value, int) and value >= 0:
        return value
    if not isinstance(value, str) or not value.startswith("0x"):
        raise ValueError(f"malformed_{name}")
    body = value[2:]
    if not body or any(char not in "0123456789abcdefABCDEF" for char in body):
        raise ValueError(f"malformed_{name}")
    return int(body, 16)


def _word(value: Any, *, name: str) -> int:
    raw = _hex(value, name=name)
    if len(raw) != 66:
        raise ValueError(f"malformed_{name}")
    parsed = int(raw[2:], 16)
    if name in {"taker_order_id", "maker_order_id"} and parsed >= 2**128:
        raise ValueError(f"malformed_{name}")
    return parsed


def _address(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    text = value.lower()
    if not text.startswith("0x") or len(text) != 42 or any(c not in "0123456789abcdef" for c in text[2:]):
        return ""
    return text


def _mask(value: str | None) -> str:
    if not value:
        return "<unresolved>"
    return value if value.startswith("<") else ("***" if len(value) <= 8 else f"{value[:4]}...{value[-4:]}")


def _dec(raw: int, decimals: int) -> Decimal:
    if decimals < 0 or decimals > 255:
        raise ValueError("invalid market decimals")
    return Decimal(raw) / (Decimal(10) ** decimals)


@dataclass(frozen=True)
class FillEventCursor:
    """Safe continuation/reorg cursor for a block range."""

    next_block: int | None = None
    block_number: int | None = None
    block_hash: str | None = None
    from_block: int | None = None
    to_block: int | None = None
    transaction_hash: str | None = None
    log_index: int | None = None


@dataclass(frozen=True)
class RawOrderFilledLog:
    address: str
    topics: tuple[str, ...]
    data: str
    block_number: int
    block_hash: str | None
    transaction_hash: str | None
    log_index: int
    transaction_index: int | None = None
    removed: bool = False
    block_timestamp: datetime | None = None


@dataclass(frozen=True)
class NormalizedOrderFill:
    fill_id: str
    chain_id: int
    pool_address: str
    symbol: str
    order_id: int | None
    taker_order_id: int
    maker_order_id: int
    owner: str | None
    side: str | None
    is_bid: bool | None
    user_data: int | None
    raw_price: int
    raw_quantity: int
    price: Decimal
    quantity: Decimal
    notional: Decimal
    block_number: int
    block_hash: str | None
    block_timestamp: datetime | None
    transaction_hash: str
    log_index: int
    confirmation_depth: int
    confirmed: bool
    removed: bool = False
    account_match: bool | None = None
    raw_taker_remaining: int | None = None
    raw_maker_remaining: int | None = None


@dataclass(frozen=True)
class FillEventSourceStatus:
    status: str
    source: str = "onchain_order_filled"
    observed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    latest_block: int | None = None
    confirmed_through_block: int | None = None
    decoded_fill_count: int = 0
    duplicate_count: int = 0
    malformed_count: int = 0
    confirmation_depth: int = 0
    pagination_complete: bool = False
    next_cursor: FillEventCursor | None = None
    reorg_status: str = "not_detected"
    account_match_status: str = "unresolved"
    reason: str | None = None
    error_code: str | None = None

    @property
    def available(self) -> bool:
        return self.status == "available"

    @property
    def authoritative(self) -> bool:
        return self.available and self.pagination_complete and self.reorg_status == "ok"


@dataclass(frozen=True)
class FillEventPage:
    fills: tuple[NormalizedOrderFill, ...] = ()
    raw_logs: tuple[RawOrderFilledLog, ...] = ()
    source_status: FillEventSourceStatus = field(
        default_factory=lambda: FillEventSourceStatus("unconfigured", reason="onchain_fill_source_unconfigured", error_code="unconfigured")
    )
    from_block: int | None = None
    to_block: int | None = None
    latest_block: int | None = None
    confirmed_through_block: int | None = None
    cursor: FillEventCursor | None = None
    next_cursor: FillEventCursor | None = None

    @classmethod
    def unavailable(cls, reason: str = "onchain_fill_source_unconfigured") -> "FillEventPage":
        return cls(source_status=FillEventSourceStatus("unconfigured", reason=reason, error_code="unconfigured"))

    @property
    def pagination_complete(self) -> bool:
        return self.source_status.pagination_complete

    @property
    def duplicate_count(self) -> int:
        return self.source_status.duplicate_count

    @property
    def account_fills(self) -> tuple[NormalizedOrderFill, ...]:
        return tuple(fill for fill in self.fills if fill.account_match is True)

    @property
    def records(self) -> tuple[NormalizedOrderFill, ...]:
        return self.fills

    @property
    def decoded_fills(self) -> tuple[NormalizedOrderFill, ...]:
        return self.fills


@dataclass(frozen=True)
class FillEventReconciliationReport:
    completed: bool
    account_fills_authoritative: bool
    reason: str
    account_match_status: str
    source_status: FillEventSourceStatus
    fills: tuple[NormalizedOrderFill, ...] = ()
    mismatches: tuple[str, ...] = ()


class FillEventRpcTransport(Protocol):
    def call(self, method: str, params: Sequence[Any]) -> Any: ...


class HttpFillEventRpcTransport:
    """Public RPC transport with an allowlist limited to chain read methods."""

    ALLOWED_METHODS = ALLOWED_RPC_METHODS

    def __init__(self, rpc_url: str, timeout_seconds: float = 10.0) -> None:
        self.rpc_url = rpc_url
        self.timeout_seconds = timeout_seconds

    def call(self, method: str, params: Sequence[Any]) -> Any:
        if method not in self.ALLOWED_METHODS:
            raise ValueError(f"RPC method is not allowed for fill indexing: {method}")
        import httpx
        response = httpx.post(
            self.rpc_url,
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": list(params)},
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("error"):
            raise RuntimeError(str(payload["error"]))
        return payload.get("result")


class UnconfiguredFillEventRpcTransport:
    """Default production-safe transport; no RPC request is attempted."""

    ALLOWED_METHODS = ALLOWED_RPC_METHODS

    def call(self, method: str, params: Sequence[Any]) -> Any:
        raise RuntimeError("onchain_fill_source_unconfigured")


class FixtureFillEventRpcTransport:
    """Deterministic RPC fixture transport.  It performs no network I/O."""

    ALLOWED_METHODS = ALLOWED_RPC_METHODS

    def __init__(self, fixture: Mapping[str, Any]) -> None:
        self.fixture = fixture
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def call(self, method: str, params: Sequence[Any]) -> Any:
        if method not in self.ALLOWED_METHODS:
            raise ValueError(f"RPC method is not allowed for fill indexing: {method}")
        self.calls.append((method, tuple(params)))
        rpc = self.fixture.get("rpc", self.fixture)
        if isinstance(rpc, Mapping):
            if method == "eth_getLogs":
                return rpc.get(method, rpc.get("logs", []))
            if method == "eth_getBlockByNumber":
                blocks = rpc.get(method, rpc.get("blocks", {}))
                block_number = params[0] if params else None
                if isinstance(blocks, Mapping):
                    return blocks.get(block_number, blocks.get(str(block_number), blocks.get("default")))
                return None
            if method == "eth_blockNumber":
                return rpc.get(method, rpc.get("block_number", "0x0"))
            if method == "eth_chainId":
                return rpc.get(method, rpc.get("chain_id", "0x1"))
        return None


def _rpc_call(transport: FillEventRpcTransport | Callable[..., Any], method: str, params: Sequence[Any]) -> Any:
    if hasattr(transport, "call"):
        return transport.call(method, params)  # type: ignore[attr-defined]
    return transport(method, params)  # type: ignore[misc]


def decode_order_filled_log(
    log: Mapping[str, Any],
    *,
    chain_id: int,
    pool_address: str,
    symbol: str,
    base_decimals: int,
    quote_decimals: int,
    confirmation_depth: int = 0,
    latest_block: int | None = None,
    owner_by_order_id: Mapping[int, str] | None = None,
    order_metadata: Mapping[int, Mapping[str, Any]] | None = None,
) -> tuple[RawOrderFilledLog, NormalizedOrderFill]:
    """Decode exactly the modern six-argument event; no absent fields guessed."""
    address = _address(log.get("address"))
    if not address:
        raise ValueError("invalid_contract_address")
    expected_pool = _address(pool_address)
    if not expected_pool or address != expected_pool:
        raise ValueError("wrong_contract_address")
    topics_value = log.get("topics")
    if not isinstance(topics_value, (list, tuple)) or len(topics_value) != 3:
        raise ValueError("malformed_topics")
    topics = tuple(_hex(item, name="topic") for item in topics_value)
    if topics[0] != ORDER_FILLED_TOPIC:
        raise ValueError("unexpected_topic")
    taker_order_id = _word(topics[1], name="taker_order_id")
    maker_order_id = _word(topics[2], name="maker_order_id")
    data = _hex(log.get("data"), name="data")
    if len(data) != 2 + 64 * 4:
        raise ValueError("malformed_data")
    words = [int(data[2 + index * 64:2 + (index + 1) * 64], 16) for index in range(4)]
    quantity_raw, taker_remaining, maker_remaining, price_raw = words
    block_number = _uint_hex(log.get("blockNumber"), name="block_number")
    log_index = _uint_hex(log.get("logIndex"), name="log_index")
    tx_hash = _hex(log.get("transactionHash"), name="transaction_hash")
    block_hash_value = log.get("blockHash")
    block_hash = _hex(block_hash_value, name="block_hash") if block_hash_value else None
    timestamp = log.get("blockTimestamp")
    block_timestamp = _utc(timestamp) if timestamp is not None else None
    removed = bool(log.get("removed", False))
    raw = RawOrderFilledLog(address, topics, data, block_number, block_hash, tx_hash, log_index, None, removed, block_timestamp)
    metadata = order_metadata or {}
    matched_id: int | None = None
    owner: str | None = None
    is_bid: bool | None = None
    user_data: int | None = None
    side: str | None = None
    for candidate in (taker_order_id, maker_order_id):
        row = metadata.get(candidate)
        if row:
            matched_id = candidate
            candidate_owner = _address(row.get("owner"))
            owner = candidate_owner or None
            if row.get("isBid") is not None or row.get("is_bid") is not None:
                is_bid = bool(row.get("isBid", row.get("is_bid")))
                side = "buy" if is_bid else "sell"
            if row.get("userData") is not None or row.get("user_data") is not None:
                user_data = int(row.get("userData", row.get("user_data")))
            break
    if owner is None and owner_by_order_id:
        for candidate in (taker_order_id, maker_order_id):
            owner = _address(owner_by_order_id.get(candidate)) or None
            if owner:
                matched_id = candidate
                break
    account_match: bool | None = None
    price = _dec(price_raw, quote_decimals)
    quantity = _dec(quantity_raw, base_decimals)
    fill_id = f"{chain_id}:{tx_hash}:{log_index}"
    depth = max(0, (latest_block - block_number) if latest_block is not None else confirmation_depth)
    normalized = NormalizedOrderFill(
        fill_id, chain_id, address, symbol, matched_id, taker_order_id, maker_order_id,
        owner, side, is_bid, user_data, price_raw, quantity_raw, price, quantity,
        price * quantity, block_number, block_hash, block_timestamp, tx_hash, log_index,
        depth, depth >= confirmation_depth, removed, account_match, taker_remaining, maker_remaining,
    )
    return raw, normalized


class OrderFilledEventIndexer:
    """Stateful, bounded-range indexer for the official OrderFilled topic."""

    def __init__(
        self,
        transport: FillEventRpcTransport | Callable[..., Any] | None = None,
        *,
        pool_address: str = SOMI_USDSO_POOL,
        symbol: str = SOMI_USDSO_SYMBOL,
        chain_id: int | None = None,
        base_decimals: int = 18,
        quote_decimals: int = 18,
        expected_account: str | None = None,
        confirmation_depth: int = 12,
        max_block_span: int = 1000,
        clock: Callable[[], datetime] | None = None,
        owner_by_order_id: Mapping[int, str] | None = None,
        order_metadata: Mapping[int, Mapping[str, Any]] | None = None,
    ) -> None:
        pool = _address(pool_address)
        if not pool:
            raise ValueError("invalid pool address")
        if confirmation_depth < 0:
            raise ValueError("confirmation_depth must be >= 0")
        if max_block_span < 1:
            raise ValueError("max_block_span must be >= 1")
        if base_decimals < 0 or quote_decimals < 0:
            raise ValueError("market decimals must be >= 0")
        self.transport = transport or UnconfiguredFillEventRpcTransport()
        self.pool_address, self.symbol = pool, symbol
        self.chain_id = chain_id
        self.base_decimals, self.quote_decimals = base_decimals, quote_decimals
        self.expected_account = _address(expected_account) or None
        self.confirmation_depth, self.max_block_span = confirmation_depth, max_block_span
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.owner_by_order_id = dict(owner_by_order_id or {})
        self.order_metadata = dict(order_metadata or {})
        self._seen: dict[str, NormalizedOrderFill] = {}
        self._cursor: FillEventCursor | None = None

    @property
    def cursor(self) -> FillEventCursor | None:
        return self._cursor

    def reset(self) -> None:
        self._seen.clear()
        self._cursor = None

    @staticmethod
    def _error_status(reason: str, *, observed: datetime, error_code: str = "unavailable") -> FillEventSourceStatus:
        return FillEventSourceStatus("unavailable", observed_at=observed, reason=reason, error_code=error_code, reorg_status="unknown")

    def _chain_int(self, method: str, default: int | None = None) -> int | None:
        value = _rpc_call(self.transport, method, [])
        if value is None:
            return default
        return _uint_hex(value, name=method)

    def _block(self, number: int) -> Mapping[str, Any] | None:
        value = _rpc_call(self.transport, "eth_getBlockByNumber", [hex(number), False])
        return value if isinstance(value, Mapping) else None

    def _account_match(self, fills: Sequence[NormalizedOrderFill]) -> tuple[tuple[NormalizedOrderFill, ...], str]:
        if not self.expected_account:
            return tuple(fills), "not_requested"
        if not fills:
            return tuple(fills), "unresolved"
        matched: list[NormalizedOrderFill] = []
        saw_unknown = False
        saw_mismatch = False
        for fill in fills:
            if fill.owner is None:
                saw_unknown = True
                matched.append(fill)
                continue
            is_match = fill.owner == self.expected_account
            saw_mismatch |= not is_match
            matched.append(NormalizedOrderFill(**{**fill.__dict__, "account_match": is_match}))
        if saw_mismatch:
            return tuple(matched), "mismatch"
        if saw_unknown:
            return tuple(matched), "unresolved"
        return tuple(matched), "matched"

    def fetch(
        self,
        *,
        from_block: int | None = None,
        to_block: int | None = None,
        cursor: FillEventCursor | None = None,
    ) -> FillEventPage:
        observed = _utc(self.clock())
        cursor = cursor or self._cursor
        try:
            chain_id = self.chain_id if self.chain_id is not None else self._chain_int("eth_chainId")
            if chain_id is None:
                raise RuntimeError("chain_id_unavailable")
            latest = self._chain_int("eth_blockNumber")
            if latest is None:
                raise RuntimeError("latest_block_unavailable")
            confirmed_through = max(0, latest - self.confirmation_depth)
            target_end = confirmed_through if to_block is None else min(to_block, confirmed_through)
            start = cursor.next_block if cursor and cursor.next_block is not None else (from_block if from_block is not None else 0)
            if start < 0 or target_end < 0:
                raise ValueError("invalid block range")
            if start > target_end:
                status = FillEventSourceStatus("available", observed_at=observed, latest_block=latest, confirmed_through_block=confirmed_through, confirmation_depth=self.confirmation_depth, pagination_complete=True, reorg_status="ok", account_match_status="unresolved" if self.expected_account else "not_requested")
                return FillEventPage((), (), status, start, target_end, latest, confirmed_through, cursor, None)
            end = min(target_end, start + self.max_block_span - 1)
            # Verify a continuation cursor against the canonical block hash.
            if cursor and cursor.block_number is not None and cursor.block_hash:
                header = self._block(cursor.block_number)
                if not header or str(header.get("hash", "")).lower() != cursor.block_hash.lower():
                    status = FillEventSourceStatus("unavailable", observed_at=observed, latest_block=latest, confirmed_through_block=confirmed_through, confirmation_depth=self.confirmation_depth, reorg_status="reorg_detected", reason="cursor_block_hash_mismatch", error_code="reorg_detected")
                    return FillEventPage(source_status=status, from_block=start, to_block=end, latest_block=latest, confirmed_through_block=confirmed_through, cursor=cursor)
            params = [{"address": self.pool_address, "fromBlock": hex(start), "toBlock": hex(end), "topics": [ORDER_FILLED_TOPIC]}]
            payload = _rpc_call(self.transport, "eth_getLogs", params)
            if not isinstance(payload, list):
                raise ValueError("malformed_logs_result")
            raw_logs: list[RawOrderFilledLog] = []
            decoded: list[NormalizedOrderFill] = []
            malformed = 0
            duplicate = 0
            reorg = False
            reason: str | None = None
            block_headers: dict[int, Mapping[str, Any] | None] = {}
            for log in payload:
                if not isinstance(log, Mapping):
                    malformed += 1
                    continue
                if _address(log.get("address")) != self.pool_address or not isinstance(log.get("topics"), (list, tuple)) or not log.get("topics") or str(log.get("topics")[0]).lower() != ORDER_FILLED_TOPIC:
                    malformed += 1
                    reason = "unexpected_log_source"
                    continue
                if bool(log.get("removed")):
                    reorg = True
                    reason = "removed_log"
                try:
                    block_number = _uint_hex(log.get("blockNumber"), name="block_number")
                    if block_number not in block_headers:
                        block_headers[block_number] = self._block(block_number)
                    header = block_headers[block_number]
                    original_block_hash = str(log.get("blockHash", "")).lower() or None
                    header_block_hash = str(header.get("hash", "")).lower() if header and header.get("hash") else None
                    block_hash = header_block_hash or original_block_hash
                    if original_block_hash and header_block_hash and original_block_hash != header_block_hash:
                        reorg = True
                        reason = "block_hash_mismatch"
                    block_timestamp = _uint_hex(header.get("timestamp"), name="timestamp") if header and header.get("timestamp") is not None else None
                    enriched = dict(log)
                    if block_hash:
                        enriched["blockHash"] = block_hash
                    if block_timestamp is not None:
                        enriched["blockTimestamp"] = block_timestamp
                    raw, fill = decode_order_filled_log(enriched, chain_id=chain_id, pool_address=self.pool_address, symbol=self.symbol, base_decimals=self.base_decimals, quote_decimals=self.quote_decimals, confirmation_depth=self.confirmation_depth, latest_block=latest, owner_by_order_id=self.owner_by_order_id, order_metadata=self.order_metadata)
                    raw_logs.append(raw)
                    if fill.removed:
                        # A removed log is reorg evidence, never a confirmed fill.
                        continue
                    existing = self._seen.get(fill.fill_id)
                    if existing:
                        if (existing.raw_quantity, existing.raw_price, existing.taker_order_id, existing.maker_order_id) != (fill.raw_quantity, fill.raw_price, fill.taker_order_id, fill.maker_order_id):
                            malformed += 1
                            reason = "malformed_duplicate"
                        else:
                            duplicate += 1
                        continue
                    self._seen[fill.fill_id] = fill
                    decoded.append(fill)
                except (TypeError, ValueError, InvalidOperation, ArithmeticError):
                    malformed += 1
            decoded, account_status = self._account_match(decoded)
            page_complete = end >= target_end
            next_cursor = None if page_complete else FillEventCursor(next_block=end + 1, block_number=end, block_hash=(block_headers.get(end) or {}).get("hash"), from_block=start, to_block=end)
            status_name = "available"
            error_code = None
            if reorg:
                status_name, error_code = "unavailable", "reorg_detected"
            elif reason == "malformed_duplicate" or malformed:
                status_name, error_code = "malformed", "malformed_duplicate" if reason == "malformed_duplicate" else "malformed_log"
            elif not page_complete:
                reason = "pagination_incomplete"
            if any(fill.block_timestamp is None for fill in decoded):
                reason = reason or "block_timestamp_unavailable"
            status = FillEventSourceStatus(status_name, observed_at=observed, latest_block=latest, confirmed_through_block=confirmed_through, decoded_fill_count=len(decoded), duplicate_count=duplicate, malformed_count=malformed, confirmation_depth=self.confirmation_depth, pagination_complete=page_complete and not reorg and status_name == "available", next_cursor=next_cursor, reorg_status="reorg_detected" if reorg else "ok", account_match_status=account_status, reason=reason, error_code=error_code)
            page = FillEventPage(tuple(decoded), tuple(raw_logs), status, start, end, latest, confirmed_through, cursor, next_cursor)
            self._cursor = next_cursor
            return page
        except Exception as exc:
            text = re.sub(r"0x[0-9a-fA-F]{8,}", "<hex>", str(exc))[:180]
            status = self._error_status(text or "onchain_fill_source_unavailable", observed=observed, error_code="rpc_error")
            return FillEventPage(source_status=status, cursor=cursor)

    index = fetch
    fetch_page = fetch
    scan = fetch

    def reconcile(self, page: FillEventPage | None = None, *, expected_account: str | None = None) -> FillEventReconciliationReport:
        page = page or self.fetch()
        account = _address(expected_account) if expected_account is not None else self.expected_account
        mismatches: list[str] = []
        status = page.source_status
        if not status.available:
            mismatches.append(status.error_code or status.reason or "onchain_fills_unavailable")
        if not status.pagination_complete:
            mismatches.append("incomplete_onchain_fills_pagination")
        if status.reorg_status != "ok":
            mismatches.append("reorg_detected")
        if status.malformed_count:
            mismatches.append("malformed_onchain_fill_logs")
        if any(fill.block_timestamp is None for fill in page.fills):
            mismatches.append("block_timestamp_unavailable")
        if status.duplicate_count and status.error_code == "malformed_duplicate":
            mismatches.append("malformed_duplicate")
        if account and status.account_match_status != "matched":
            mismatches.append("authoritative_account_address_unresolved")
        authoritative = not mismatches and bool(account)
        return FillEventReconciliationReport(authoritative, authoritative, "reconciled" if authoritative else ";".join(dict.fromkeys(mismatches)) or "account_address_unresolved", status.account_match_status, status, page.fills, tuple(dict.fromkeys(mismatches)))


OrderFilledIndexer = OrderFilledEventIndexer
DreamDexFillEventIndexer = OrderFilledEventIndexer
FillEventIndexer = OrderFilledEventIndexer


__all__ = [
    "ORDER_FILLED_EVENT_SIGNATURE", "ORDER_FILLED_TOPIC", "ORDER_FILLED_INDEXED_FIELDS", "ORDER_FILLED_NON_INDEXED_FIELDS", "ORDER_FILLED_ABSENT_FIELDS", "SOMI_USDSO_SYMBOL", "SOMI_USDSO_POOL", "ALLOWED_RPC_METHODS",
    "FillEventCursor", "RawOrderFilledLog", "NormalizedOrderFill", "FillEventPage", "FillEventSourceStatus", "FillEventReconciliationReport",
    "FillEventRpcTransport", "HttpFillEventRpcTransport", "UnconfiguredFillEventRpcTransport", "FixtureFillEventRpcTransport",
    "decode_order_filled_log", "OrderFilledEventIndexer", "OrderFilledIndexer", "DreamDexFillEventIndexer", "FillEventIndexer",
]
