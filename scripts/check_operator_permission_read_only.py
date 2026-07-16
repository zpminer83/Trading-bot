"""Manual, fail-closed operator permission probe.

The command is intentionally disabled by default.  It only issues the four
allow-listed JSON-RPC view methods when every public configuration prerequisite
is present; it never signs, submits, or mutates anything.
"""
from __future__ import annotations

import json
import os
from urllib.request import Request, urlopen
from typing import Any

from bot.integrations.dreamdex_operator_permissions import (
    FUND_OWNER_ENV,
    OPERATOR_ENV,
    PERMISSION_PROBE_ENABLED_ENV,
    READ_ONLY_RPC_METHODS,
    audit_fund_owner_semantics,
    audit_vendor_selectors,
    build_capability_matrix,
    build_vendor_snapshot_fingerprint,
    discover_operator_registry,
    load_operator_configuration,
    probe_operator_permissions_read_only,
)
from bot.integrations.dreamdex_read_only import mask_account_id


class JsonRpcTransport:
    """Minimal read-only transport with no generic method passthrough."""

    def __init__(self, url: str, timeout: float = 10.0) -> None:
        self.url = url
        self.timeout = timeout
        self._counter = 0

    def call(self, method: str, params: list[Any]) -> Any:
        if method not in READ_ONLY_RPC_METHODS:
            raise ValueError("RPC method is not allowed in read-only operator mode")
        self._counter += 1
        request = Request(
            self.url,
            data=json.dumps({"jsonrpc": "2.0", "id": self._counter, "method": method, "params": params}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=self.timeout) as response:  # noqa: S310 - explicit user-enabled read-only probe
            body = json.loads(response.read().decode("utf-8"))
        if isinstance(body, dict) and body.get("error") is not None:
            raise RuntimeError("RPC error")
        if not isinstance(body, dict) or "result" not in body:
            raise RuntimeError("malformed RPC response")
        return body["result"]


def _flag_status(raw: str | None) -> tuple[bool, str]:
    if raw is None or raw.strip().lower() in {"", "0", "false", "no", "off"}:
        return False, "disabled"
    if raw.strip().lower() in {"1", "true", "yes", "on"}:
        return True, "enabled"
    return False, "configuration_invalid"


def run_probe_from_env() -> int:
    enabled, flag_status = _flag_status(os.environ.get(PERMISSION_PROBE_ENABLED_ENV))
    print("OPERATOR PERMISSION READ-ONLY PROBE:")
    print(f"  probe enabled: {'YES' if enabled else 'NO'}")
    if not enabled:
        print("  network attempt performed: NO")
        print("  status: disabled")
        print("  authority authoritative: NO")
        print("  unresolved reasons: operator_permission_probe_disabled")
        return 0
    if flag_status == "configuration_invalid":
        print("  network attempt performed: NO")
        print("  status: configuration_invalid")
        print("  authority authoritative: NO")
        print("  unresolved reasons: invalid_enable_flag")
        return 2

    config = load_operator_configuration(os.environ)
    registry = discover_operator_registry(chain_id=5031)
    market_pool = os.environ.get("DREAMDEX_READ_ONLY_POOL_ADDRESS") or os.environ.get("DREAMDEX_POOL_ADDRESS")
    rpc_url = os.environ.get("DREAMDEX_READ_ONLY_RPC_URL") or os.environ.get("RPC_URL")
    selectors = {item.capability: item for item in audit_vendor_selectors()}
    matrix = build_capability_matrix()
    prerequisites = []
    if config.status != "configured":
        prerequisites.append("operator_or_fund_owner_configuration_invalid")
    if not market_pool:
        prerequisites.append("pool_address_unconfigured")
    elif os.environ.get("DREAMDEX_READ_ONLY_POOL_SOURCE_STATUS") != "source_confirmed":
        prerequisites.append("pool_source_not_confirmed")
    if not rpc_url:
        prerequisites.append("rpc_url_unconfigured")
    if registry.status != "source_confirmed":
        prerequisites.append("registry_source_not_confirmed")
    if selectors.get("place_order_for") is None or selectors["place_order_for"].status != "confirmed":
        prerequisites.append("place_selector_unconfirmed")
    if selectors.get("cancel_order_for") is None or selectors["cancel_order_for"].status != "confirmed":
        prerequisites.append("cancel_selector_unconfirmed")
    if matrix.selector_consistency != "confirmed":
        prerequisites.append("selector_consistency_unconfirmed")
    if selectors.get("reduce_order_for") is not None and selectors["reduce_order_for"].status == "conflicting":
        prerequisites.append("reduce_selector_conflicting")
    if prerequisites:
        print("  network attempt performed: NO")
        print("  chain ID: unavailable")
        print(f"  registry address: {mask_account_id(registry.registry_address) if registry.registry_address else '<unresolved>'}")
        print(f"  registry source status: {registry.status}")
        print(f"  pool address: {mask_account_id(market_pool) if market_pool else '<unresolved>'}")
        print(f"  fund owner: {mask_account_id(config.identity.onchain_fund_owner_address) if config.identity.onchain_fund_owner_address else '<unresolved>'}")
        print(f"  operator: {mask_account_id(config.identity.operator_address) if config.identity.operator_address else '<unresolved>'}")
        print("  status: configuration_invalid")
        print(f"  authority authoritative: NO")
        print(f"  unresolved reasons: {', '.join(dict.fromkeys(prerequisites))}")
        return 2

    result = probe_operator_permissions_read_only(
        JsonRpcTransport(rpc_url),
        pool=market_pool,
        owner=config.identity.onchain_fund_owner_address,
        operator=config.identity.operator_address,
        registry=registry,
    )
    print(f"  network attempt performed: {'YES' if result.network_attempt_performed else 'NO'}")
    print(f"  chain ID: {result.chain_id if result.chain_id is not None else 'unavailable'}")
    print(f"  latest block: {result.latest_block if result.latest_block is not None else 'unavailable'}")
    print(f"  registry address: {mask_account_id(registry.registry_address) if registry.registry_address else '<unresolved>'}")
    print(f"  registry source status: {registry.status}")
    print(f"  registry code status: {result.registry_code_status}")
    print(f"  pool address: {mask_account_id(market_pool)}")
    print(f"  pool code status: {result.pool_code_status}")
    print(f"  fund owner: {result.fund_owner_masked}")
    print("  fund owner candidate role: explicit on-chain fund owner")
    print(f"  operator: {result.operator_masked}")
    for label, selector, evidence in (("place", "0x80054449", result.place), ("cancel", "0xe37b444b", result.cancel)):
        print(f"  {label} selector: {selector}")
        print(f"  {label} authorization: {evidence.status if evidence else 'unavailable'}")
    print("  reduce selector: 0x364c2587")
    print("  reduce authorization: unavailable (no RPC call)")
    print(f"  status: {result.status}")
    print("  evidence block: " + (str(result.latest_block) if result.latest_block is not None else "unavailable"))
    print("  evidence freshness: current session only")
    print("  authority authoritative: NO")
    print(f"  unresolved reasons: {', '.join(result.unresolved_reasons) or 'none'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run_probe_from_env())
