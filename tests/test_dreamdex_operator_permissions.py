from pathlib import Path
from dataclasses import replace

import pytest

from bot.integrations.dreamdex_operator_permissions import (
    CAPABILITY_NAMES,
    FUND_OWNER_ENV,
    OPERATOR_ENV,
    PERMISSION_PROBE_ENABLED_ENV,
    READ_ONLY_RPC_METHODS,
    audit_open_order_semantics,
    audit_typescript_python_parity,
    audit_vendor_selectors,
    build_capability_matrix,
    build_is_operator_authorized_eth_call,
    build_authority_evidence,
    build_vendor_snapshot_fingerprint,
    check_operator_permission_read_only,
    audit_fund_owner_semantics,
    discover_operator_registry,
    probe_operator_permissions_read_only,
    load_operator_configuration,
    parse_is_operator_authorized_result,
    resolve_operator_permission,
)


POOL = "0x1111111111111111111111111111111111111111"
OWNER = "0x2222222222222222222222222222222222222222"
OPERATOR = "0x3333333333333333333333333333333333333333"
PLACE = "0x80054449"


def test_vendor_snapshot_is_deterministic_and_has_no_absolute_paths():
    first = build_vendor_snapshot_fingerprint()
    second = build_vendor_snapshot_fingerprint()
    assert first.safe_dict() == second.safe_dict()
    assert first.package_version == "0.1.0"
    assert first.source_fingerprints
    assert all("D:\\" not in name and "/vendor/" not in name for name, _ in first.source_fingerprints)
    assert "packages/core/src/contract.ts" in dict(first.source_fingerprints)


def test_local_selectors_recompute_and_reduce_is_explicitly_unavailable():
    evidence = {item.capability: item for item in audit_vendor_selectors()}
    assert evidence["place_order_for"].selector == PLACE
    assert evidence["place_order_for"].status == "confirmed"
    assert evidence["cancel_order_for"].selector == "0xe37b444b"
    assert evidence["cancel_order_for"].status == "confirmed"
    assert evidence["reduce_order_for"].selector == "0x364c2587"
    assert evidence["reduce_order_for"].status == "unavailable"
    assert evidence["grant_operator_per_pool"].selector is not None
    assert evidence["grant_operator_global"].selector is not None
    assert evidence["deny_operator_per_pool"].selector is not None


def test_selector_conflict_is_fail_closed():
    snapshot = build_vendor_snapshot_fingerprint()
    altered = replace(snapshot, declared_selectors=(("placeOrderFor", "0xdeadbeef"),))
    evidence = {item.capability: item for item in audit_vendor_selectors(altered)}
    assert evidence["place_order_for"].status == "conflicting"
    assert evidence["place_order_for"].reason == "declared_selector_mismatch"


def test_capability_matrix_separates_operator_and_owner_capabilities():
    matrix = build_capability_matrix()
    assert set(item.name for item in matrix.capabilities) == set(CAPABILITY_NAMES)
    assert matrix.place_order_for.operator_callable is True
    assert matrix.cancel_order_for.operator_callable is True
    assert matrix.reduce_order_for.operator_callable is None
    assert matrix.deposit.owner_only is True
    assert matrix.withdraw.owner_only is True
    assert matrix.grant_operator_per_pool.owner_only is True
    assert matrix.authoritative is False


def test_permission_resolution_is_conservative_and_denial_wins():
    assert resolve_operator_permission(per_pool_approval=True).effective_permission == "allowed"
    assert resolve_operator_permission(global_approval=True).scope == "broad_scope"
    assert resolve_operator_permission(global_approval=True, per_pool_denial=True).effective_permission == "denied"
    assert resolve_operator_permission().effective_permission == "unknown"
    evidence = build_authority_evidence(permission=resolve_operator_permission(per_pool_approval=True))
    assert evidence.effective_place_status == "rpc_confirmed_allowed"
    denied = build_authority_evidence(permission=resolve_operator_permission(per_pool_approval=True, per_pool_denial=True))
    assert denied.effective_place_status == "rpc_confirmed_denied"


def test_exact_eth_call_target_and_abi_padding_without_value():
    call = build_is_operator_authorized_eth_call(POOL, OWNER, OPERATOR, PLACE)
    assert call["to"] == POOL
    assert call["data"].startswith("0xa8cb3794")
    assert len(call["data"]) == 2 + 8 + 64 * 3
    assert "value" not in call
    assert call["data"][10:74].endswith(OWNER[2:])
    assert call["data"][74:138].endswith(OPERATOR[2:])
    assert call["data"][138:202].startswith(PLACE[2:])


@pytest.mark.parametrize("result,expected", [("0x" + "0" * 64, False), ("0x" + "0" * 63 + "1", True)])
def test_bool_result_is_strict(result, expected):
    assert parse_is_operator_authorized_result(result) is expected


@pytest.mark.parametrize("result", [None, "", "0x", "0x01", "0x" + "0" * 63 + "2", "0x" + "g" * 64])
def test_malformed_bool_result_is_rejected(result):
    with pytest.raises(ValueError):
        parse_is_operator_authorized_result(result)


class RpcFixture:
    def __init__(self, result="0x" + "0" * 63 + "1", *, chain="0x1", block="0x10", error=None):
        self.calls = []
        self.result = result
        self.chain = chain
        self.block = block
        self.error = error

    def call(self, method, params):
        self.calls.append((method, params))
        if method == "eth_chainId":
            return self.chain
        if method == "eth_blockNumber":
            return self.block
        if method == "eth_call":
            if self.error:
                raise RuntimeError(self.error)
            return self.result
        raise AssertionError(method)


def test_read_only_permission_check_allowed_and_denied():
    fixture = RpcFixture()
    result = check_operator_permission_read_only(fixture, pool=POOL, owner=OWNER, operator=OPERATOR, selector=PLACE, expected_chain_id=1)
    assert result.status == "rpc_confirmed_allowed"
    assert result.allowed is True
    assert [method for method, _ in fixture.calls] == ["eth_chainId", "eth_blockNumber", "eth_call"]
    denied = check_operator_permission_read_only(RpcFixture("0x" + "0" * 64), pool=POOL, owner=OWNER, operator=OPERATOR, selector=PLACE)
    assert denied.status == "rpc_confirmed_denied"
    assert denied.allowed is False


def test_rpc_faults_are_redacted_and_wrong_chain_or_stale_block_fail_closed():
    wrong_chain = check_operator_permission_read_only(RpcFixture(chain="0x2"), pool=POOL, owner=OWNER, operator=OPERATOR, selector=PLACE, expected_chain_id=1)
    assert wrong_chain.error_code == "wrong_chain"
    stale = check_operator_permission_read_only(RpcFixture(block="0x1"), pool=POOL, owner=OWNER, operator=OPERATOR, selector=PLACE, minimum_block_number=2)
    assert stale.status == "stale"
    reverted = check_operator_permission_read_only(RpcFixture(error="execution reverted 0xdeadbeef"), pool=POOL, owner=OWNER, operator=OPERATOR, selector=PLACE)
    assert reverted.error_code == "contract_revert"
    assert "deadbeef" not in (reverted.reason or "")


def test_invalid_address_blocks_before_rpc():
    fixture = RpcFixture()
    with pytest.raises(ValueError, match="configuration_invalid"):
        check_operator_permission_read_only(fixture, pool=POOL, owner="bad", operator=OPERATOR, selector=PLACE)
    assert fixture.calls == []


def test_configuration_reads_only_public_addresses_and_invalid_is_fail_closed():
    configured = load_operator_configuration({OPERATOR_ENV: OPERATOR, FUND_OWNER_ENV: OWNER})
    assert configured.status == "configured"
    assert configured.operator_configured and configured.fund_owner_configured
    assert configured.identity.authoritative is False
    invalid = load_operator_configuration({OPERATOR_ENV: "not-an-address", "PRIVATE_KEY": "secret"})
    assert invalid.status == "configuration_invalid"
    assert not invalid.operator_configured


def test_open_order_semantics_are_caller_scoped_but_not_authoritative():
    result = audit_open_order_semantics()
    assert result.status == "caller_scoped"
    assert result.typescript_behavior == "caller_scoped"
    assert result.python_behavior == "caller_scoped"
    assert result.rest_behavior == "source_unavailable"
    assert result.authoritative is False


def test_python_parity_is_explicitly_conflicting_for_missing_operator_surface():
    parity = audit_typescript_python_parity()
    assert parity.place_order_for == "confirmed"
    assert parity.cancel_order_for == "confirmed"
    assert parity.reduce_order_for == "unavailable"
    assert parity.is_operator_authorized == "unavailable"
    assert parity.status == "conflicting"
    assert parity.authoritative is False


def test_identity_roles_are_separate_and_repr_masks_addresses():
    config = load_operator_configuration({OPERATOR_ENV: OPERATOR, FUND_OWNER_ENV: OWNER}, contest_owner_address=OWNER, platform_trading_address=POOL)
    safe = config.identity.safe_dict()
    assert safe["contest_owner_address"] != OWNER
    assert safe["platform_trading_address"] != POOL
    assert safe["onchain_fund_owner_address"] != OWNER
    assert safe["operator_address"] != OPERATOR
    assert config.identity.authoritative is False
    assert OWNER not in repr(config.identity)


def test_rpc_allowlist_has_no_mutation_or_signing_methods():
    assert READ_ONLY_RPC_METHODS == {"eth_call", "eth_chainId", "eth_getCode", "eth_blockNumber"}


def test_mainnet_registry_is_source_confirmed_and_offline():
    result = discover_operator_registry()
    assert result.status == "source_confirmed"
    assert result.registry_address == "0xe7a190736b6024a4dbafadc04e283075877005ce"
    assert result.network_calls == 0
    assert result.operator_mode_blocked is True
    assert result.addresses[0].source_file.endswith("packages/core/src/config/networks.ts")


def test_registry_conflict_never_selects_an_address(tmp_path):
    path = tmp_path / "packages" / "core" / "src" / "config"
    path.mkdir(parents=True)
    (path / "networks.ts").write_text(
        'mainnet: { chainId: 5031, operatorRegistry: "0x1111111111111111111111111111111111111111" },\n'
        'mainnet: { chainId: 5031, operatorRegistry: "0x2222222222222222222222222222222222222222" },\n',
        encoding="utf-8",
    )
    result = discover_operator_registry(tmp_path)
    assert result.status == "conflicting"
    assert result.selected_address is None
    assert result.network_calls == 0


def test_fund_owner_semantics_are_explicit_but_not_authoritative():
    result = audit_fund_owner_semantics()
    assert result.status == "source_confirmed"
    assert "fund owner" in result.place_order_for_owner
    assert result.authoritative is False


def test_permission_probe_flag_defaults_disabled_and_invalid_is_fail_closed():
    default = load_operator_configuration({})
    assert default.permission_probe_enabled is False
    invalid = load_operator_configuration({PERMISSION_PROBE_ENABLED_ENV: "sometimes"})
    assert invalid.status == "configuration_invalid"
    assert invalid.enable_flag_status == "configuration_invalid"


class PermissionRpcFixture:
    def __init__(self, result="0x" + "0" * 63 + "1"):
        self.calls = []
        self.result = result

    def call(self, method, params):
        self.calls.append((method, params))
        if method == "eth_chainId":
            return "0x13a7"
        if method == "eth_blockNumber":
            return "0x20"
        if method == "eth_getCode":
            return "0x6000"
        if method == "eth_call":
            return self.result
        raise AssertionError(method)


def test_permission_probe_uses_only_read_methods_and_keeps_authority_false():
    fixture = PermissionRpcFixture()
    result = probe_operator_permissions_read_only(fixture, pool=POOL, owner=OWNER, operator=OPERATOR)
    assert result.network_attempt_performed is True
    assert result.authoritative is False
    assert result.status == "rpc_confirmed_allowed"
    assert set(method for method, _ in fixture.calls) <= READ_ONLY_RPC_METHODS
    assert "eth_sendTransaction" not in set(method for method, _ in fixture.calls)
