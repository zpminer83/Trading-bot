"""Strict, read-only DreamDEX market trading-rule evidence.

The public market response is the only runtime source used here.  Values are
kept separate from their evidence status so an incomplete response cannot be
mistaken for a complete set of exchange rules.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Any, Mapping


_FIELD_NAMES = (
    "symbol", "market_address", "base_token_address", "quote_token_address", "stop_registry",
    "market_status", "trading_enabled", "tick_size", "quantity_step",
    "minimum_quantity", "minimum_notional", "base_decimals", "quote_decimals",
    "price_decimals", "quantity_decimals", "confirmed_order_types",
)
_ACTIVE = {"active", "trading", "open"}
_KNOWN_STATUSES = _ACTIVE | {"paused", "halted", "closed", "unknown"}
_BOT_KIT_CONFIRMED = {
    "symbol",
    "market_address", "base_token_address", "quote_token_address", "stop_registry",
    "tick_size", "quantity_step", "minimum_quantity", "base_decimals", "quote_decimals",
}


@dataclass(frozen=True)
class MarketRuleEvidence:
    """Evidence for one rule, independent of the other rules."""

    value: Any = None
    status: str = "unavailable"  # confirmed/unavailable/malformed/conflicting/unsupported
    source: str = "unavailable"  # official_bot_kit/public_rest/both_confirmed/...
    reason: str | None = None


@dataclass(frozen=True)
class PublicMarketSchemaFingerprint:
    endpoint_name: str
    http_status: int | None
    top_level_type: str
    top_level_field_names: tuple[str, ...]
    field_types: tuple[tuple[str, str], ...]
    nested_field_names: tuple[str, ...]
    nested_field_types: tuple[tuple[str, str], ...]
    list_lengths: tuple[tuple[str, int], ...]
    observed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


def _value_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, Mapping):
        return "object"
    if isinstance(value, list):
        return "array"
    if isinstance(value, str):
        return "string"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    return type(value).__name__


def fingerprint_market_payload(payload: Any, *, observed_at: datetime | None = None, http_status: int | None = 200) -> PublicMarketSchemaFingerprint:
    """Capture only public `/markets` structure, never payload values."""
    nested_names: list[str] = []
    nested_types: list[tuple[str, str]] = []
    lengths: list[tuple[str, int]] = []

    def walk(value: Any, path: str, depth: int) -> None:
        if isinstance(value, list):
            lengths.append((path, len(value)))
            if value and depth < 3:
                walk(value[0], f"{path}[0]", depth + 1)
            return
        if not isinstance(value, Mapping) or depth >= 3:
            return
        for key, item in value.items():
            child = f"{path}.{key}"
            nested_names.append(child)
            nested_types.append((child, _value_type(item)))
            walk(item, child, depth + 1)

    top_type = _value_type(payload)
    top_names: tuple[str, ...] = tuple(str(key) for key in payload.keys()) if isinstance(payload, Mapping) else ()
    top_types: tuple[tuple[str, str], ...] = tuple((str(key), _value_type(value)) for key, value in payload.items()) if isinstance(payload, Mapping) else ()
    walk(payload, "$", 0)
    return PublicMarketSchemaFingerprint(
        endpoint_name="/markets", http_status=http_status, top_level_type=top_type,
        top_level_field_names=top_names, field_types=top_types,
        nested_field_names=tuple(nested_names), nested_field_types=tuple(nested_types),
        list_lengths=tuple(lengths), observed_at=_utc(observed_at),
    )


@dataclass(frozen=True)
class DreamDexMarketTradingRules:
    symbol: str | None = None
    market_address: str | None = None
    base_token_address: str | None = None
    quote_token_address: str | None = None
    stop_registry: str | None = None
    market_status: str | None = None
    trading_enabled: bool | None = None
    tick_size: Decimal | None = None
    quantity_step: Decimal | None = None
    minimum_quantity: Decimal | None = None
    minimum_notional: Decimal | None = None
    base_decimals: int | None = None
    quote_decimals: int | None = None
    price_decimals: int | None = None
    quantity_decimals: int | None = None
    confirmed_order_types: tuple[str, ...] | None = None
    source_status: str = "unavailable"
    schema_status: str = "unavailable"
    authoritative_fields: tuple[str, ...] = ()
    unavailable_fields: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()
    conflicting_values: Mapping[str, tuple[Any, ...]] = field(default_factory=dict)
    observed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    field_statuses: Mapping[str, MarketRuleEvidence] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "observed_at", _utc(self.observed_at))
        object.__setattr__(self, "field_statuses", MappingProxyType(dict(self.field_statuses)))
        object.__setattr__(self, "conflicting_values", MappingProxyType({key: tuple(value) for key, value in self.conflicting_values.items()}))

    def status_for(self, field_name: str) -> str:
        evidence = self.field_statuses.get(field_name)
        return evidence.status if evidence is not None else "unavailable"

    def evidence_for(self, field_name: str) -> MarketRuleEvidence:
        return self.field_statuses.get(field_name, MarketRuleEvidence())

    @property
    def available(self) -> bool:
        return self.source_status in {"available", "both_confirmed"} and not self.conflicts


def _utc(value: datetime | None) -> datetime:
    value = value or datetime.now(timezone.utc)
    return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _decimal(value: Any, field_name: str, *, minimum: Decimal | None = None, strictly_positive: bool = False) -> Decimal:
    if isinstance(value, bool) or value is None or value == "":
        raise ValueError(f"malformed {field_name}")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError(f"malformed {field_name}") from None
    if not parsed.is_finite() or (strictly_positive and parsed <= 0) or (minimum is not None and parsed < minimum):
        raise ValueError(f"invalid {field_name}")
    return parsed


def _integer(value: Any, field_name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"malformed {field_name}")
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"malformed {field_name}") from None
    if str(value).strip() != str(parsed) and not isinstance(value, int):
        raise ValueError(f"malformed {field_name}")
    if parsed < 0 or parsed > 255:
        raise ValueError(f"invalid {field_name}")
    return parsed


def _address(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"malformed {field_name}")
    clean = value.lower().removeprefix("0x")
    if len(clean) != 40 or any(c not in "0123456789abcdef" for c in clean):
        raise ValueError(f"invalid {field_name} address")
    return value


def _evidence(value: Any, *, source: str, status: str = "confirmed", reason: str | None = None) -> MarketRuleEvidence:
    return MarketRuleEvidence(value=value, status=status, source=source, reason=reason)


def parse_market_trading_rules(
    row: Mapping[str, Any],
    *,
    symbol: str,
    observed_at: datetime | None = None,
    source: str = "public_rest",
    allow_legacy_aliases: bool = False,
) -> DreamDexMarketTradingRules:
    """Parse a single `/markets` row without inventing missing rules."""

    if not isinstance(row, Mapping):
        raise ValueError("market row must be an object")
    observed = _utc(observed_at)
    requested_symbol = str(symbol)
    raw_symbol = row.get("symbol")
    if raw_symbol is not None and str(raw_symbol) != requested_symbol:
        raise ValueError("market symbol mismatch")

    values: dict[str, Any] = {}
    evidence: dict[str, MarketRuleEvidence] = {}
    conflicts: list[str] = []

    def evidence_source(field_name: str) -> str:
        return "both_confirmed" if source == "public_rest" and field_name in _BOT_KIT_CONFIRMED else source

    def required_address(name: str, key: str, aliases: tuple[str, ...] = ()) -> None:
        raw = next((row[k] for k in (key, *aliases) if k in row and row[k] is not None), None)
        if raw is None:
            evidence[name] = _evidence(None, source="unavailable", status="unavailable", reason=f"missing {key}")
            values[name] = None
            return
        parsed = _address(raw, name)
        values[name] = parsed
        evidence[name] = _evidence(parsed, source=evidence_source(name))

    values["symbol"] = requested_symbol
    evidence["symbol"] = _evidence(requested_symbol, source=evidence_source("symbol"))
    aliases = lambda values: values if allow_legacy_aliases else ()
    required_address("market_address", "contract", aliases(("poolContract", "pool_contract", "poolAddress", "pool_address")))
    required_address("base_token_address", "base", aliases(("baseTokenAddress", "base_token_address", "baseAddress")))
    required_address("quote_token_address", "quote", aliases(("quoteTokenAddress", "quote_token_address", "quoteAddress")))

    def decimal_rule(name: str, keys: tuple[str, ...], *, positive: bool = False, minimum: Decimal | None = None) -> None:
        raw = next((row[k] for k in keys if k in row and row[k] is not None), None)
        if raw is None:
            values[name] = None
            evidence[name] = _evidence(None, source="unavailable", status="unavailable", reason=f"missing {keys[0]}")
            return
        parsed = _decimal(raw, name, strictly_positive=positive, minimum=minimum)
        values[name] = parsed
        evidence[name] = _evidence(parsed, source=evidence_source(name))

    decimal_rule("tick_size", ("tickSize", *aliases(("tick_size", "priceTickSize"))), positive=True)
    decimal_rule("quantity_step", ("lotSize", *aliases(("quantityStepSize", "quantity_step_size", "stepSize"))), positive=True)
    decimal_rule("minimum_quantity", ("minQuantity", *aliases(("minimumQuantity", "min_quantity"))), positive=True)
    decimal_rule("minimum_notional", ("minimumNotional", *aliases(("minNotional", "minimum_notional"))), minimum=Decimal("0"))

    def integer_rule(name: str, keys: tuple[str, ...]) -> None:
        raw = next((row[k] for k in keys if k in row and row[k] is not None), None)
        if raw is None:
            values[name] = None
            evidence[name] = _evidence(None, source="unavailable", status="unavailable", reason=f"missing {keys[0]}")
            return
        parsed = _integer(raw, name)
        values[name] = parsed
        evidence[name] = _evidence(parsed, source=evidence_source(name))

    integer_rule("base_decimals", ("baseDecimals", *aliases(("base_decimals",))))
    integer_rule("quote_decimals", ("quoteDecimals", *aliases(("quote_decimals",))))
    integer_rule("price_decimals", ("priceDecimals", *aliases(("price_decimals",))))
    integer_rule("quantity_decimals", ("quantityDecimals", *aliases(("quantity_decimals",))))

    status_keys = ("status",) if not allow_legacy_aliases else ("status", "marketStatus", "market_status")
    raw_status = next((row[k] for k in status_keys if k in row and row[k] is not None), None)
    if raw_status is None:
        values["market_status"] = None
        values["trading_enabled"] = False
        evidence["market_status"] = _evidence(None, source="unavailable", status="unavailable", reason="missing market status")
        evidence["trading_enabled"] = _evidence(False, source="unavailable", status="unavailable", reason="market status unavailable")
    else:
        normalized = str(raw_status).strip().lower()
        if normalized not in _KNOWN_STATUSES:
            normalized = "unknown"
            status_state = "unsupported"
        else:
            status_state = "confirmed"
        values["market_status"] = normalized
        values["trading_enabled"] = normalized in _ACTIVE
        evidence["market_status"] = _evidence(normalized, source=source, status=status_state)
        evidence["trading_enabled"] = _evidence(values["trading_enabled"], source=source, status=status_state)

    type_keys = ("supportedOrderTypes",) if not allow_legacy_aliases else ("supportedOrderTypes", "supported_order_types", "orderTypes")
    raw_types = next((row[k] for k in type_keys if k in row and row[k] is not None), None)
    if raw_types is None:
        values["confirmed_order_types"] = None
        evidence["confirmed_order_types"] = _evidence(None, source="unavailable", status="unavailable", reason="order types unavailable")
    else:
        if isinstance(raw_types, str):
            raw_types = (raw_types,)
        if not isinstance(raw_types, (list, tuple)) or any(not isinstance(item, str) or not item for item in raw_types):
            raise ValueError("malformed confirmed_order_types")
        values["confirmed_order_types"] = tuple(item.lower() for item in raw_types)
        evidence["confirmed_order_types"] = _evidence(values["confirmed_order_types"], source=source)

    # Stop registry is useful metadata but not an order rule; validate it when present.
    stop_keys = ("stopRegistry",) if not allow_legacy_aliases else ("stopRegistry", "stop_registry")
    stop_raw = next((row[k] for k in stop_keys if k in row and row[k] is not None), None)
    if stop_raw is not None:
        values["stop_registry"] = _address(stop_raw, "stop_registry")
        evidence["stop_registry"] = _evidence(values["stop_registry"], source=evidence_source("stop_registry"))
    else:
        values["stop_registry"] = None
        evidence["stop_registry"] = _evidence(None, source="unavailable", status="unavailable", reason="missing stopRegistry")

    required = ("market_address", "base_token_address", "quote_token_address", "tick_size", "quantity_step", "minimum_quantity", "base_decimals", "quote_decimals")
    missing = [name for name in required if evidence[name].status != "confirmed"]
    authoritative = tuple(name for name in _FIELD_NAMES if evidence.get(name, MarketRuleEvidence()).status == "confirmed")
    unavailable = tuple(name for name in _FIELD_NAMES if evidence.get(name, MarketRuleEvidence()).status != "confirmed")
    schema_status = "incomplete" if missing else "observed"
    source_status = "incomplete" if missing else "available"
    return DreamDexMarketTradingRules(
        symbol=values["symbol"], market_address=values["market_address"],
        base_token_address=values["base_token_address"], quote_token_address=values["quote_token_address"],
        stop_registry=values["stop_registry"],
        market_status=values["market_status"], trading_enabled=values["trading_enabled"],
        tick_size=values["tick_size"], quantity_step=values["quantity_step"],
        minimum_quantity=values["minimum_quantity"], minimum_notional=values["minimum_notional"],
        base_decimals=values["base_decimals"], quote_decimals=values["quote_decimals"],
        price_decimals=values["price_decimals"], quantity_decimals=values["quantity_decimals"],
        confirmed_order_types=values["confirmed_order_types"], source_status=source_status,
        schema_status=schema_status, authoritative_fields=authoritative, unavailable_fields=unavailable,
        conflicts=tuple(conflicts), observed_at=observed, field_statuses=evidence,
    )


__all__ = ["DreamDexMarketTradingRules", "MarketRuleEvidence", "PublicMarketSchemaFingerprint", "fingerprint_market_payload", "parse_market_trading_rules"]
