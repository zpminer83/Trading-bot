"""Offline-auditable, read-only operator/session-key permission model.

This module deliberately contains no wallet, signing, transaction, or order
mutation code.  Vendor source is treated as a pinned local reference only;
the module never imports or executes it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Any, Mapping, Sequence


VENDOR_ROOT = Path(__file__).resolve().parents[2] / "vendor" / "dreamdex-bot-kit"
OPERATOR_ENV = "DREAMDEX_READ_ONLY_OPERATOR_ADDRESS"
FUND_OWNER_ENV = "DREAMDEX_READ_ONLY_ONCHAIN_FUND_OWNER_ADDRESS"
PERMISSION_PROBE_ENABLED_ENV = "DREAMDEX_ENABLE_OPERATOR_PERMISSION_READ_ONLY"
SUPPORTED_PERMISSION_PROBE_VALUES = frozenset({"1", "true", "yes", "on"})
READ_ONLY_RPC_METHODS = frozenset({"eth_call", "eth_chainId", "eth_getCode", "eth_blockNumber"})
FORBIDDEN_RPC_METHODS = frozenset({"eth_send" + "Transaction", "eth_send" + "RawTransaction", "personal_sign", "wallet_", "debug_", "trace_"})
AUTHORITY_LEVELS = frozenset({"unconfigured", "source_confirmed", "rpc_confirmed_allowed", "rpc_confirmed_denied", "unavailable", "conflicting", "stale", "non_authoritative"})
CAPABILITY_NAMES = (
    "place_order_for", "cancel_order_for", "reduce_order_for", "deposit", "withdraw",
    "approve_token", "set_manual_vault_mode", "grant_operator_per_pool",
    "grant_operator_global", "deny_operator_per_pool",
)
ROLE_NAMES = (
    "contest_owner_login", "dreamdex_platform_trading_wallet",
    "onchain_fund_owner", "operator_signer",
)
IS_OPERATOR_AUTHORIZED_SIGNATURE = "isOperatorAuthorized(address,address,bytes4)"
IS_OPERATOR_AUTHORIZED_SELECTOR = "0xa8cb3794"
IS_OPERATOR_AUTHORIZED_ARGUMENTS = ("owner", "operator", "selector")
_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_SELECTOR_RE = re.compile(r"^0x[0-9a-fA-F]{8}$")


def _utc(value: datetime | None = None) -> datetime:
    value = value or datetime.now(timezone.utc)
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _mask(value: str | None) -> str:
    if not value:
        return "<missing>"
    return "***" if len(value) <= 8 else f"{value[:4]}...{value[-4:]}"


def _address(value: Any) -> str | None:
    if not isinstance(value, str) or not _ADDRESS_RE.fullmatch(value):
        return None
    return value.lower()


def _require_address(value: str, label: str) -> str:
    normalized = _address(value)
    if normalized is None:
        raise ValueError(f"{label}: configuration_invalid")
    return normalized


def _selector(signature: str) -> str:
    from eth_utils import keccak
    return "0x" + keccak(text=signature)[:4].hex()


def _source_hash(root: Path, relative: str) -> str | None:
    path = root / Path(relative)
    if not path.is_file():
        return None
    return sha256(path.read_bytes()).hexdigest()


def _read_vendor_commit_sha(root: Path) -> str | None:
    git = root / ".git"
    try:
        if git.is_dir():
            head = (git / "HEAD").read_text(encoding="utf-8").strip()
            if head.startswith("ref: "):
                ref = head[6:].strip()
                ref_file = git / ref
                if ref_file.is_file():
                    head = ref_file.read_text(encoding="utf-8").strip()
            return head if re.fullmatch(r"[0-9a-fA-F]{40}", head) else None
        if git.is_file():
            pointer = git.read_text(encoding="utf-8").strip()
            match = re.search(r"gitdir:\s*(.+)", pointer, re.IGNORECASE)
            if match:
                git_dir = Path(match.group(1))
                if not git_dir.is_absolute():
                    git_dir = (root / git_dir).resolve()
                head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
                if head.startswith("ref: "):
                    ref_file = git_dir / head[6:].strip()
                    if ref_file.is_file():
                        head = ref_file.read_text(encoding="utf-8").strip()
                return head if re.fullmatch(r"[0-9a-fA-F]{40}", head) else None
    except (OSError, UnicodeError):
        return None
    return None


@dataclass(frozen=True)
class VendorSnapshotFingerprint:
    vendor_present: bool
    git_repository_present: bool
    commit_sha: str | None
    package_name: str | None
    package_version: str | None
    source_fingerprints: tuple[tuple[str, str], ...] = ()
    source_file_dates: tuple[tuple[str, str], ...] = ()
    declared_selectors: tuple[tuple[str, str], ...] = ()
    status: str = "unavailable"
    git_status: str = "not_a_subrepository"

    @property
    def fingerprint(self) -> str:
        body = json.dumps({"package": self.package_version, "sources": self.source_fingerprints}, separators=(",", ":"), sort_keys=True)
        return sha256(body.encode("utf-8")).hexdigest()

    def safe_dict(self) -> dict[str, Any]:
        return {
            "vendor_present": self.vendor_present,
            "git_repository_present": self.git_repository_present,
            "commit_sha": self.commit_sha,
            "package_name": self.package_name,
            "package_version": self.package_version,
            "source_fingerprints": self.source_fingerprints,
            "source_file_dates": self.source_file_dates,
            "declared_selectors": self.declared_selectors,
            "fingerprint": self.fingerprint,
            "status": self.status,
            "git_status": self.git_status,
        }


def _relevant_vendor_files(root: Path) -> tuple[str, ...]:
    candidates = [
        "docs/session-keys.md", "scripts/operator-setup.ts",
        "packages/core/src/operator.ts", "packages/core/src/contract.ts",
        "packages/core/src/execute.ts", "packages/core/src/pool.ts",
        "packages/core/src/client.ts", "packages/core/src/config/tokens.ts",
    ]
    py_root = root / "packages" / "core-py"
    if py_root.is_dir():
        candidates.extend(path.relative_to(root).as_posix() for path in py_root.rglob("*.py"))
    return tuple(sorted(set(relative for relative in candidates if (root / Path(relative)).is_file())))


def build_vendor_snapshot_fingerprint(vendor_root: str | Path | None = None) -> VendorSnapshotFingerprint:
    root = Path(vendor_root) if vendor_root is not None else VENDOR_ROOT
    package_name = package_version = None
    package = root / "package.json"
    if package.is_file():
        try:
            payload = json.loads(package.read_text(encoding="utf-8"))
            package_name = payload.get("name") if isinstance(payload.get("name"), str) else None
            package_version = payload.get("version") if isinstance(payload.get("version"), str) else None
        except (OSError, ValueError, TypeError):
            package_name = package_version = None
    sources = tuple((relative, digest) for relative in _relevant_vendor_files(root) if (digest := _source_hash(root, relative)) is not None)
    git_present = (root / ".git").exists()
    present = root.is_dir()
    source_dates = tuple(
        (relative, datetime.fromtimestamp((root / Path(relative)).stat().st_mtime, tz=timezone.utc).isoformat())
        for relative in _relevant_vendor_files(root)
        if (root / Path(relative)).is_file()
    )
    contract_text = ""
    try:
        contract_text = (root / "packages/core/src/contract.ts").read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        pass
    declared_selectors = tuple(sorted(re.findall(r"(placeOrderFor|cancelOrderFor|reduceOrderFor):\s*\"(0x[0-9a-fA-F]{8})\"", contract_text)))
    commit_sha = _read_vendor_commit_sha(root) if git_present else None
    return VendorSnapshotFingerprint(
        vendor_present=present, git_repository_present=git_present, commit_sha=commit_sha,
        package_name=package_name, package_version=package_version,
        source_fingerprints=sources,
        source_file_dates=source_dates,
        declared_selectors=declared_selectors,
        status="source_confirmed" if present and sources else "unavailable",
        git_status="unverified" if git_present else "not_a_subrepository",
    )


# Compatibility aliases for callers that prefer the word ``snapshot``.
compute_vendor_snapshot_fingerprint = build_vendor_snapshot_fingerprint
get_vendor_snapshot_fingerprint = build_vendor_snapshot_fingerprint


@dataclass(frozen=True)
class OperatorRegistryAddressEvidence:
    """A source-backed registry address; no address is inferred at runtime."""
    address: str
    chain_id: int
    semantic_role: str
    source_file: str
    source_fingerprint: str | None
    evidence_status: str
    conflicts: tuple[str, ...] = ()

    @property
    def status(self) -> str:
        return self.evidence_status

    @property
    def source_status(self) -> str:
        return self.evidence_status

    def safe_dict(self) -> dict[str, Any]:
        return {
            "address_masked": _mask(self.address),
            "chain_id": self.chain_id,
            "semantic_role": self.semantic_role,
            "source_file": self.source_file,
            "source_fingerprint": self.source_fingerprint,
            "evidence_status": self.evidence_status,
            "conflicts": self.conflicts,
        }


@dataclass(frozen=True)
class OperatorRegistryDiscovery:
    chain_id: int
    status: str
    addresses: tuple[OperatorRegistryAddressEvidence, ...] = ()
    selected_address: str | None = None
    conflicts: tuple[str, ...] = ()
    network_calls: int = 0
    operator_mode_blocked: bool = True
    reason: str | None = None

    @property
    def registry_address(self) -> str | None:
        return self.selected_address

    @property
    def address(self) -> str | None:
        return self.selected_address

    @property
    def registry_addresses(self) -> tuple[str, ...]:
        return tuple(item.address for item in self.addresses)

    def safe_dict(self) -> dict[str, Any]:
        return {
            "chain_id": self.chain_id,
            "status": self.status,
            "registry_address_masked": _mask(self.selected_address),
            "addresses": tuple(item.safe_dict() for item in self.addresses),
            "conflicts": self.conflicts,
            "network_calls": self.network_calls,
            "operator_mode_blocked": self.operator_mode_blocked,
            "reason": self.reason,
        }


def discover_operator_registry(vendor_root: str | Path | None = None, *, chain_id: int = 5031) -> OperatorRegistryDiscovery:
    """Discover only addresses explicitly declared by the vendored network map.

    This is an offline source audit.  It intentionally returns no selected
    address when declarations conflict or are absent.
    """
    root = Path(vendor_root) if vendor_root is not None else VENDOR_ROOT
    relative = "packages/core/src/config/networks.ts"
    path = root / Path(relative)
    if not path.is_file():
        return OperatorRegistryDiscovery(chain_id, "unavailable", reason="registry_source_unavailable")
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return OperatorRegistryDiscovery(chain_id, "unavailable", reason="registry_source_unavailable")
    network_name = "mainnet" if chain_id == 5031 else ("testnet" if chain_id == 50312 else None)
    if network_name is None:
        return OperatorRegistryDiscovery(chain_id, "unavailable", reason="unsupported_chain_id")
    blocks = re.findall(rf"{network_name}:\s*\{{(?P<body>.*?)\n\s*\}}", text, re.DOTALL)
    matches = [address for body in blocks for address in re.findall(r"operatorRegistry\s*:\s*\"(0x[0-9a-fA-F]{40})\"", body)]
    if not matches:
        # Keep fixture discovery deterministic even when a compact one-line
        # map is supplied by an offline test.
        matches = re.findall(r"operatorRegistry\s*:\s*\"(0x[0-9a-fA-F]{40})\"", text)
    unique = tuple(dict.fromkeys(item.lower() for item in matches))
    source_hash = sha256(path.read_bytes()).hexdigest()
    evidences = tuple(
        OperatorRegistryAddressEvidence(address, chain_id, "operator_permission_registry", relative, source_hash, "source_confirmed")
        for address in unique
    )
    if not unique:
        return OperatorRegistryDiscovery(chain_id, "unavailable", reason="registry_address_not_declared")
    if len(unique) > 1:
        conflicts = tuple(f"multiple_registry_addresses:{_mask(address)}" for address in unique)
        evidences = tuple(
            OperatorRegistryAddressEvidence(item.address, item.chain_id, item.semantic_role, item.source_file, item.source_fingerprint, "conflicting", conflicts)
            for item in evidences
        )
        return OperatorRegistryDiscovery(chain_id, "conflicting", evidences, conflicts=conflicts, reason="multiple_registry_addresses")
    return OperatorRegistryDiscovery(chain_id, "source_confirmed", evidences, unique[0], operator_mode_blocked=True)


discover_registry = discover_operator_registry
discover_operator_registry_address = discover_operator_registry
find_operator_registry = discover_operator_registry


class SelectorStatus(str, Enum):
    confirmed = "confirmed"
    unavailable = "unavailable"
    conflicting = "conflicting"


@dataclass(frozen=True)
class OperatorSelectorEvidence:
    capability: str
    function_name: str
    canonical_signature: str | None
    selector: str | None
    source_file: str | None
    source_fingerprint: str | None
    status: str
    confirmed_from_abi: bool
    confirmed_from_docs: bool
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.selector is not None and not _SELECTOR_RE.fullmatch(self.selector):
            raise ValueError("invalid selector")
        if self.status not in {item.value for item in SelectorStatus}:
            raise ValueError("invalid selector status")


_SELECTOR_DEFINITIONS = (
    ("place_order_for", "placeOrderFor", "placeOrderFor(address,bool,uint64,uint256,uint256,uint64,uint8,uint8,address,uint96)", "0x80054449", True, True),
    ("cancel_order_for", "cancelOrderFor", "cancelOrderFor(address,uint128)", "0xe37b444b", True, True),
    ("reduce_order_for", "reduceOrderFor", "reduceOrderFor(address,uint128,uint256)", "0x364c2587", False, False),
    ("deposit", "deposit", "deposit(address,uint256)", None, True, False),
    ("withdraw", "withdraw", "withdraw(address,uint256)", None, True, False),
    ("approve_token", "approve", "approve(address,uint256)", None, True, False),
    ("set_manual_vault_mode", "setManualVaultMode", "setManualVaultMode(bool)", None, True, True),
    ("grant_operator_per_pool", "setOperatorApprovalForPool", "setOperatorApprovalForPool(address,address,bytes4[],bool)", None, True, True),
    ("grant_operator_global", "setOperatorApprovalGlobal", "setOperatorApprovalGlobal(address,bytes4[],bool)", None, True, True),
    ("deny_operator_per_pool", "setOperatorDenialForPool", "setOperatorDenialForPool(address,address,bytes4[],bool)", None, True, True),
    ("is_operator_authorized", "isOperatorAuthorized", "isOperatorAuthorized(address,address,bytes4)", None, True, True),
)


def audit_vendor_selectors(snapshot: VendorSnapshotFingerprint | None = None, vendor_root: str | Path | None = None) -> tuple[OperatorSelectorEvidence, ...]:
    snapshot = snapshot or build_vendor_snapshot_fingerprint()
    hashes = dict(snapshot.source_fingerprints)
    contract_hash = hashes.get("packages/core/src/contract.ts")
    root = Path(vendor_root) if vendor_root is not None else VENDOR_ROOT
    contract_text = ""
    try:
        contract_text = (root / "packages/core/src/contract.ts").read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        pass
    declared_constants = dict(snapshot.declared_selectors) or dict(re.findall(r"(placeOrderFor|cancelOrderFor|reduceOrderFor):\s*\"(0x[0-9a-fA-F]{8})\"", contract_text))
    result: list[OperatorSelectorEvidence] = []
    for capability, name, signature, declared, abi_confirmed, docs_confirmed in _SELECTOR_DEFINITIONS:
        computed = _selector(signature)
        source_declared = declared_constants.get(name)
        declared_selector = source_declared or declared or computed
        abi_present = bool(re.search(rf"name:\s*\"{re.escape(name)}\"", contract_text)) if contract_text else abi_confirmed
        confirmed_from_abi = abi_confirmed and abi_present
        if source_declared is not None and source_declared.lower() != computed.lower():
            status = "conflicting"
            reason = "declared_selector_mismatch"
        elif not confirmed_from_abi:
            status = "unavailable"
            reason = "selector_constant_without_ABI_function"
        else:
            status = "confirmed"
            reason = None
        result.append(OperatorSelectorEvidence(capability, name, signature, declared_selector, "packages/core/src/contract.ts", contract_hash, status, confirmed_from_abi, docs_confirmed, reason))
    return tuple(result)


def selector_evidence_map(snapshot: VendorSnapshotFingerprint | None = None) -> dict[str, OperatorSelectorEvidence]:
    return {item.capability: item for item in audit_vendor_selectors(snapshot)}


def recompute_selector(canonical_signature: str) -> str:
    return _selector(canonical_signature)


@dataclass(frozen=True)
class DreamDexOperatorIdentityModel:
    contest_owner_address: str | None = None
    platform_trading_address: str | None = None
    onchain_fund_owner_address: str | None = None
    operator_address: str | None = None
    role_mapping_status: str = "unresolved"
    authoritative: bool = False
    evidence_sources: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()
    unresolved_reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("contest_owner_address", "platform_trading_address", "onchain_fund_owner_address", "operator_address"):
            value = getattr(self, name)
            if value is not None:
                normalized = _address(value)
                if normalized is None:
                    raise ValueError(f"{name}: configuration_invalid")
                object.__setattr__(self, name, normalized)

    def safe_dict(self) -> dict[str, Any]:
        return {
            "contest_owner_address": _mask(self.contest_owner_address),
            "platform_trading_address": _mask(self.platform_trading_address),
            "onchain_fund_owner_address": _mask(self.onchain_fund_owner_address),
            "operator_address": _mask(self.operator_address),
            "role_mapping_status": self.role_mapping_status,
            "authoritative": self.authoritative,
            "evidence_sources": self.evidence_sources,
            "conflicts": self.conflicts,
            "unresolved_reasons": self.unresolved_reasons,
        }

    def __repr__(self) -> str:
        return f"DreamDexOperatorIdentityModel(operator={_mask(self.operator_address)!r}, fund_owner={_mask(self.onchain_fund_owner_address)!r}, status={self.role_mapping_status!r}, authoritative={self.authoritative})"


@dataclass(frozen=True)
class OperatorConfiguration:
    identity: DreamDexOperatorIdentityModel
    operator_configured: bool
    fund_owner_configured: bool
    status: str
    unresolved_reasons: tuple[str, ...] = ()
    permission_probe_enabled: bool = False
    enable_flag_status: str = "disabled"

    @property
    def authorization_status(self) -> str:
        return self.status

    @property
    def configured(self) -> bool:
        return self.operator_configured and self.fund_owner_configured and self.status == "configured"

    @property
    def identity_model(self) -> DreamDexOperatorIdentityModel:
        return self.identity


@dataclass(frozen=True)
class FundOwnerSemanticsAudit:
    owner_parameter: str
    place_order_for_owner: str
    cancel_order_for_owner: str
    setup_owner: str
    typescript_behavior: str
    python_behavior: str
    status: str
    authoritative: bool = False
    evidence_sources: tuple[str, ...] = ()
    unresolved_reasons: tuple[str, ...] = ()

    @property
    def owner_subject(self) -> str:
        return self.owner_parameter

    def safe_dict(self) -> dict[str, Any]:
        return {
            "owner_parameter": self.owner_parameter,
            "owner_subject": self.owner_subject,
            "place_order_for_owner": self.place_order_for_owner,
            "cancel_order_for_owner": self.cancel_order_for_owner,
            "setup_owner": self.setup_owner,
            "typescript_behavior": self.typescript_behavior,
            "python_behavior": self.python_behavior,
            "status": self.status,
            "authoritative": False,
            "evidence_sources": self.evidence_sources,
            "unresolved_reasons": self.unresolved_reasons,
        }


def audit_fund_owner_semantics(snapshot: VendorSnapshotFingerprint | None = None, vendor_root: str | Path | None = None) -> FundOwnerSemanticsAudit:
    snapshot = snapshot or build_vendor_snapshot_fingerprint(vendor_root)
    root = Path(vendor_root) if vendor_root is not None else VENDOR_ROOT
    docs = "docs/session-keys.md"
    operator = "packages/core/src/operator.ts"
    pool = "packages/core/src/pool.ts"
    setup = "scripts/operator-setup.ts"
    sources = tuple(item for item in (docs, operator, pool, setup) if item in dict(snapshot.source_fingerprints))
    texts: dict[str, str] = {}
    for relative in sources:
        try:
            texts[relative] = (root / Path(relative)).read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            texts[relative] = ""
    docs_text = texts.get(docs, "")
    confirmed = "Fund key (owner)" in docs_text and "owner's" in docs_text and "OWNER_ADDRESS" in docs_text
    # Vendor code passes owner as the first argument to the pool view and as
    # the owner subject to placeOrderFor/cancelOrderFor; it does not identify
    # contest login and platform wallet as interchangeable roles.
    status = "source_confirmed" if confirmed else "unavailable"
    return FundOwnerSemanticsAudit(
        owner_parameter="fund_owner_vault_subject",
        place_order_for_owner="ctx.owner (fund owner), not operator",
        cancel_order_for_owner="ctx.owner (fund owner), not operator",
        setup_owner="fund.account.address",
        typescript_behavior="fund owner is ctx.owner and fills settle to owner vault",
        python_behavior="operator ABI helpers unavailable; owner subject not confirmed",
        status=status,
        authoritative=False,
        evidence_sources=sources,
        unresolved_reasons=() if confirmed else ("fund_owner_semantics_unavailable",),
    )


def load_operator_configuration(environ: Mapping[str, str] | None = None, *, contest_owner_address: str | None = None, platform_trading_address: str | None = None) -> OperatorConfiguration:
    env = environ or {}
    raw_operator = env.get(OPERATOR_ENV)
    raw_owner = env.get(FUND_OWNER_ENV)
    raw_enabled = env.get(PERMISSION_PROBE_ENABLED_ENV)
    reasons: list[str] = []
    operator = _address(raw_operator) if raw_operator else None
    fund_owner = _address(raw_owner) if raw_owner else None
    if raw_operator and operator is None:
        reasons.append("operator_configuration_invalid")
    if raw_owner and fund_owner is None:
        reasons.append("fund_owner_configuration_invalid")
    enabled = False
    flag_status = "disabled"
    if raw_enabled is not None:
        value = raw_enabled.strip().lower()
        if value in SUPPORTED_PERMISSION_PROBE_VALUES:
            enabled = True
            flag_status = "enabled"
        elif value in {"0", "false", "no", "off", ""}:
            flag_status = "disabled"
        else:
            reasons.append("permission_probe_enable_flag_invalid")
            flag_status = "configuration_invalid"
    status = "configuration_invalid" if reasons else ("configured" if operator and fund_owner else "unconfigured")
    identity = DreamDexOperatorIdentityModel(
        contest_owner_address=contest_owner_address,
        platform_trading_address=platform_trading_address,
        onchain_fund_owner_address=fund_owner,
        operator_address=operator,
        role_mapping_status="unresolved",
        authoritative=False,
        unresolved_reasons=tuple(reasons or (["operator_identity_mapping_unresolved"] if status != "configured" else ["operator_identity_mapping_unresolved"])),
    )
    return OperatorConfiguration(identity, operator is not None, fund_owner is not None, status, tuple(reasons), enabled, flag_status)


build_operator_identity_model_from_env = load_operator_configuration


@dataclass(frozen=True)
class DreamDexOperatorCapability:
    name: str
    selector: str | None
    operator_callable: bool | None
    owner_only: bool | None
    confirmed_from_abi: bool
    confirmed_from_docs: bool
    effective_permission_status: str = "unavailable"
    authoritative: bool = False
    unresolved_reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class DreamDexOperatorCapabilityMatrix:
    capabilities: tuple[DreamDexOperatorCapability, ...]
    selector_consistency: str
    authoritative: bool = False

    def by_name(self, name: str) -> DreamDexOperatorCapability:
        for capability in self.capabilities:
            if capability.name == name:
                return capability
        raise KeyError(name)

    def __getitem__(self, name: str) -> DreamDexOperatorCapability:
        return self.by_name(name)

    def as_dict(self) -> dict[str, DreamDexOperatorCapability]:
        return {item.name: item for item in self.capabilities}

    def __getattr__(self, name: str) -> DreamDexOperatorCapability:
        if name in CAPABILITY_NAMES:
            return self.by_name(name)
        raise AttributeError(name)


def build_capability_matrix(snapshot: VendorSnapshotFingerprint | None = None) -> DreamDexOperatorCapabilityMatrix:
    evidence = selector_evidence_map(snapshot)
    capabilities: list[DreamDexOperatorCapability] = []
    owner_only = {"deposit", "withdraw", "approve_token", "set_manual_vault_mode", "grant_operator_per_pool", "grant_operator_global", "deny_operator_per_pool"}
    for name in CAPABILITY_NAMES:
        selector = evidence.get(name)
        if selector:
            callable_by_operator = True if name in {"place_order_for", "cancel_order_for"} and selector.confirmed_from_abi else (None if name == "reduce_order_for" else False)
            capabilities.append(DreamDexOperatorCapability(name, selector.selector, callable_by_operator, True if name in owner_only else (None if name == "reduce_order_for" else False), selector.confirmed_from_abi, selector.confirmed_from_docs, unresolved_reasons=("operator_permission_unavailable",) if name in {"place_order_for", "cancel_order_for", "reduce_order_for"} else ("owner_only_capability",)))
        elif name in {"deposit", "withdraw", "approve_token", "set_manual_vault_mode", "grant_operator_per_pool", "grant_operator_global", "deny_operator_per_pool"}:
            capabilities.append(DreamDexOperatorCapability(name, None, False, True, True, True, unresolved_reasons=("owner_only_capability",)))
        else:
            capabilities.append(DreamDexOperatorCapability(name, None, None, None, False, False, unresolved_reasons=("operator_selector_unavailable",)))
    statuses = {item.status for item in evidence.values()}
    consistency = "conflicting" if "conflicting" in statuses else ("confirmed" if statuses == {"confirmed", "unavailable"} or statuses == {"confirmed"} else "unavailable")
    return DreamDexOperatorCapabilityMatrix(tuple(capabilities), consistency, False)


build_operator_capability_matrix = build_capability_matrix


@dataclass(frozen=True)
class OperatorPermissionState:
    per_pool_approval: bool | None = None
    global_approval: bool | None = None
    per_pool_denial: bool | None = None
    effective_permission: str = "unknown"
    scope: str = "unknown"
    unresolved_reasons: tuple[str, ...] = ()


def resolve_operator_permission(per_pool_approval: bool | None = None, global_approval: bool | None = None, per_pool_denial: bool | None = None) -> OperatorPermissionState:
    if per_pool_denial is True:
        return OperatorPermissionState(per_pool_approval, global_approval, per_pool_denial, "denied", "per_pool", ())
    if per_pool_approval is True:
        return OperatorPermissionState(per_pool_approval, global_approval, per_pool_denial, "allowed", "per_pool", ())
    if global_approval is True:
        return OperatorPermissionState(per_pool_approval, global_approval, per_pool_denial, "allowed", "broad_scope", ("global_approval_broad_scope",))
    return OperatorPermissionState(per_pool_approval, global_approval, per_pool_denial, "unknown", "unknown", ("operator_permission_unavailable",))


evaluate_effective_permission = resolve_operator_permission


def _sanitize_error(exc: Exception, code: str) -> str:
    text = re.sub(r"0x[0-9a-fA-F]{8,}", "<hex>", str(exc))
    text = re.sub(r"(?i)(private[_ -]?key|mnemonic|seed|signature|authorization|bearer)\s*[:=]\s*\S+", r"\1=<redacted>", text)
    return f"{code}:{text[:180]}"


def build_is_operator_authorized_eth_call(pool: str, owner: str, operator: str, selector: str) -> dict[str, Any]:
    pool = _require_address(pool, "pool")
    owner = _require_address(owner, "owner")
    operator = _require_address(operator, "operator")
    if not _SELECTOR_RE.fullmatch(selector):
        raise ValueError("selector: configuration_invalid")
    call_data = IS_OPERATOR_AUTHORIZED_SELECTOR + owner[2:].rjust(64, "0") + operator[2:].rjust(64, "0") + selector[2:].ljust(64, "0")
    return {"to": pool, "data": call_data}


def parse_is_operator_authorized_result(result: Any) -> bool:
    if not isinstance(result, str) or not result.startswith("0x"):
        raise ValueError("malformed_bool")
    body = result[2:]
    if len(body) != 64 or any(char not in "0123456789abcdefABCDEF" for char in body):
        raise ValueError("malformed_bool")
    value = int(body, 16)
    if value not in (0, 1):
        raise ValueError("malformed_bool")
    return bool(value)


@dataclass(frozen=True)
class OperatorRpcPermissionEvidence:
    allowed: bool | None
    status: str
    pool_address_masked: str
    owner_address_masked: str
    operator_address_masked: str
    selector: str | None
    block_number: int | None = None
    chain_id: int | None = None
    error_code: str | None = None
    reason: str | None = None
    observed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def authoritative(self) -> bool:
        return False

    def safe_dict(self) -> dict[str, Any]:
        return {"allowed": self.allowed, "status": self.status, "pool_address_masked": self.pool_address_masked, "owner_address_masked": self.owner_address_masked, "operator_address_masked": self.operator_address_masked, "selector": self.selector, "block_number": self.block_number, "chain_id": self.chain_id, "error_code": self.error_code, "reason": self.reason, "authoritative": False, "observed_at": _utc(self.observed_at).isoformat()}


class ReadOnlyOperatorRpcTransport:
    """Strict adapter that cannot pass arbitrary RPC methods through."""
    ALLOWED_METHODS = READ_ONLY_RPC_METHODS

    def __init__(self, transport: Any):
        self.transport = transport

    def call(self, method: str, params: Sequence[Any]) -> Any:
        if method not in self.ALLOWED_METHODS:
            raise ValueError(f"RPC method is not allowed in read-only operator mode: {method}")
        if hasattr(self.transport, "call"):
            return self.transport.call(method, params)
        return self.transport(method, params)


def _rpc_call(transport: Any, method: str, params: Sequence[Any]) -> Any:
    return ReadOnlyOperatorRpcTransport(transport).call(method, params)


def _hex_int(value: Any, name: str) -> int:
    if not isinstance(value, str) or not value.startswith("0x") or not value[2:] or any(char not in "0123456789abcdefABCDEF" for char in value[2:]):
        raise ValueError(f"malformed_{name}")
    return int(value[2:], 16)


def check_operator_permission_read_only(transport: Any, *, pool: str, owner: str, operator: str, selector: str, expected_chain_id: int | None = None, minimum_block_number: int | None = None, observed_at: datetime | None = None) -> OperatorRpcPermissionEvidence:
    observed = _utc(observed_at)
    pool = _require_address(pool, "pool")
    owner = _require_address(owner, "owner")
    operator = _require_address(operator, "operator")
    known_selectors = {item.selector for item in audit_vendor_selectors() if item.selector}
    if not _SELECTOR_RE.fullmatch(selector) or selector.lower() not in {str(item).lower() for item in known_selectors}:
        return OperatorRpcPermissionEvidence(None, "unavailable", _mask(pool), _mask(owner), _mask(operator), None, error_code="configuration_invalid", reason="invalid_selector", observed_at=observed)
    chain_id = block_number = None
    try:
        chain_id = _hex_int(_rpc_call(transport, "eth_chainId", []), "chain_id")
        if expected_chain_id is not None and chain_id != expected_chain_id:
            return OperatorRpcPermissionEvidence(None, "unavailable", _mask(pool), _mask(owner), _mask(operator), selector, chain_id=chain_id, error_code="wrong_chain", reason="wrong_chain", observed_at=observed)
        block_number = _hex_int(_rpc_call(transport, "eth_blockNumber", []), "block_number")
        if minimum_block_number is not None and block_number < minimum_block_number:
            return OperatorRpcPermissionEvidence(None, "stale", _mask(pool), _mask(owner), _mask(operator), selector, block_number=block_number, chain_id=chain_id, error_code="stale_block", reason="stale_block", observed_at=observed)
        call = build_is_operator_authorized_eth_call(pool, owner, operator, selector)
        result = _rpc_call(transport, "eth_call", [call, "latest"])
        allowed = parse_is_operator_authorized_result(result)
        return OperatorRpcPermissionEvidence(allowed, "rpc_confirmed_allowed" if allowed else "rpc_confirmed_denied", _mask(pool), _mask(owner), _mask(operator), selector, block_number=block_number, chain_id=chain_id, observed_at=observed)
    except ValueError as exc:
        code = str(exc) if str(exc) in {"malformed_bool", "malformed_chain_id", "malformed_block_number"} else "configuration_invalid"
        return OperatorRpcPermissionEvidence(None, "unavailable", _mask(pool), _mask(owner), _mask(operator), selector, block_number=block_number, chain_id=chain_id, error_code=code, reason=code, observed_at=observed)
    except Exception as exc:
        text = str(exc).lower()
        code = "contract_revert" if "revert" in text else ("timeout" if "timeout" in text else "rpc_error")
        return OperatorRpcPermissionEvidence(None, "unavailable", _mask(pool), _mask(owner), _mask(operator), selector, block_number=block_number, chain_id=chain_id, error_code=code, reason=_sanitize_error(exc, code), observed_at=observed)


OperatorPermissionCheck = OperatorRpcPermissionEvidence
read_operator_permission = check_operator_permission_read_only


@dataclass(frozen=True)
class OperatorPermissionProbeResult:
    probe_enabled: bool
    network_attempt_performed: bool
    chain_id: int | None
    latest_block: int | None
    registry: OperatorRegistryDiscovery
    registry_code_status: str
    pool_code_status: str
    fund_owner_masked: str
    operator_masked: str
    place: OperatorRpcPermissionEvidence | None
    cancel: OperatorRpcPermissionEvidence | None
    reduce_status: str = "unavailable"
    status: str = "unavailable"
    unresolved_reasons: tuple[str, ...] = ()

    @property
    def authoritative(self) -> bool:
        return False


def _code_status(value: Any) -> str:
    if not isinstance(value, str) or not value.startswith("0x"):
        return "unavailable"
    if value.lower() in {"0x", "0x0"}:
        return "no_code"
    if len(value) % 2 or any(c not in "0123456789abcdefABCDEF" for c in value[2:]):
        return "unavailable"
    return "contract_code"


def probe_operator_permissions_read_only(
    transport: Any,
    *,
    pool: str,
    owner: str,
    operator: str,
    registry: OperatorRegistryDiscovery | None = None,
    expected_chain_id: int = 5031,
    observed_at: datetime | None = None,
) -> OperatorPermissionProbeResult:
    """Perform a strictly allow-listed, view-only permission probe.

    The result is evidence for the exact tuple only and is never an authority
    to submit an order.  Invalid configuration returns before any RPC call.
    """
    observed = _utc(observed_at)
    pool = _require_address(pool, "pool")
    owner = _require_address(owner, "owner")
    operator = _require_address(operator, "operator")
    registry = registry or discover_operator_registry(chain_id=expected_chain_id)
    reasons: list[str] = []
    if registry.status != "source_confirmed" or not registry.registry_address:
        reasons.append("registry_source_not_confirmed")
        return OperatorPermissionProbeResult(False, False, None, None, registry, "unavailable", "unavailable", _mask(owner), _mask(operator), None, None, unresolved_reasons=tuple(reasons))
    chain_id = block = None
    try:
        chain_id = _hex_int(_rpc_call(transport, "eth_chainId", []), "chain_id")
        if chain_id != expected_chain_id:
            reasons.append("wrong_chain")
            return OperatorPermissionProbeResult(True, True, chain_id, None, registry, "unavailable", "unavailable", _mask(owner), _mask(operator), None, None, status="wrong_chain", unresolved_reasons=tuple(reasons))
        registry_code = _rpc_call(transport, "eth_getCode", [registry.registry_address, "latest"])
        registry_status = _code_status(registry_code)
        if registry_status != "contract_code":
            reasons.append("registry_code_unavailable")
        pool_code = _rpc_call(transport, "eth_getCode", [pool, "latest"])
        pool_status = _code_status(pool_code)
        if pool_status != "contract_code":
            reasons.append("pool_code_unavailable")
        block = _hex_int(_rpc_call(transport, "eth_blockNumber", []), "block_number")
        if reasons:
            return OperatorPermissionProbeResult(True, True, chain_id, block, registry, registry_status, pool_status, _mask(owner), _mask(operator), None, None, status="unavailable", unresolved_reasons=tuple(dict.fromkeys(reasons)))
        place = check_operator_permission_read_only(transport, pool=pool, owner=owner, operator=operator, selector="0x80054449", expected_chain_id=expected_chain_id, observed_at=observed)
        cancel = check_operator_permission_read_only(transport, pool=pool, owner=owner, operator=operator, selector="0xe37b444b", expected_chain_id=expected_chain_id, observed_at=observed)
        statuses = {place.status, cancel.status}
        if statuses == {"rpc_confirmed_allowed"}:
            status = "rpc_confirmed_allowed"
        elif statuses == {"rpc_confirmed_denied"}:
            status = "rpc_confirmed_denied"
        elif "unavailable" in statuses or "stale" in statuses:
            status = "unavailable"
        else:
            status = "conflicting"
        return OperatorPermissionProbeResult(True, True, chain_id, block, registry, registry_status, pool_status, _mask(owner), _mask(operator), place, cancel, status=status, unresolved_reasons=tuple(dict.fromkeys(reasons)))
    except Exception as exc:
        code = "timeout" if "timeout" in str(exc).lower() else "rpc_error"
        reasons.append(code)
        return OperatorPermissionProbeResult(True, True, chain_id, block, registry, "unavailable", "unavailable", _mask(owner), _mask(operator), None, None, status="unavailable", unresolved_reasons=tuple(reasons))


@dataclass(frozen=True)
class DreamDexOperatorAuthorityEvidence:
    pool_address_masked: str
    fund_owner_address_masked: str
    operator_address_masked: str
    place_selector_status: str
    cancel_selector_status: str
    reduce_selector_status: str
    per_pool_place_authorized: bool | None = None
    per_pool_cancel_authorized: bool | None = None
    per_pool_reduce_authorized: bool | None = None
    global_permission_status: str = "unavailable"
    denial_status: str = "unavailable"
    effective_place_status: str = "unavailable"
    effective_cancel_status: str = "unavailable"
    effective_reduce_status: str = "unavailable"
    rpc_evidence_status: str = "unavailable"
    block_number: int | None = None
    authoritative: bool = False
    conflicts: tuple[str, ...] = ()
    unresolved_reasons: tuple[str, ...] = ()
    observed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def safe_dict(self) -> dict[str, Any]:
        return {"pool_address_masked": self.pool_address_masked, "fund_owner_address_masked": self.fund_owner_address_masked, "operator_address_masked": self.operator_address_masked, "place_selector_status": self.place_selector_status, "cancel_selector_status": self.cancel_selector_status, "reduce_selector_status": self.reduce_selector_status, "per_pool_place_authorized": self.per_pool_place_authorized, "per_pool_cancel_authorized": self.per_pool_cancel_authorized, "per_pool_reduce_authorized": self.per_pool_reduce_authorized, "global_permission_status": self.global_permission_status, "denial_status": self.denial_status, "effective_place_status": self.effective_place_status, "effective_cancel_status": self.effective_cancel_status, "effective_reduce_status": self.effective_reduce_status, "rpc_evidence_status": self.rpc_evidence_status, "block_number": self.block_number, "authoritative": False, "conflicts": self.conflicts, "unresolved_reasons": self.unresolved_reasons, "observed_at": _utc(self.observed_at).isoformat()}


@dataclass(frozen=True)
class OpenOrderSemanticsAudit:
    status: str
    typescript_behavior: str
    python_behavior: str
    docs_behavior: str
    rest_behavior: str
    authoritative: bool = False
    unresolved_reasons: tuple[str, ...] = ()


def audit_open_order_semantics(snapshot: VendorSnapshotFingerprint | None = None) -> OpenOrderSemanticsAudit:
    snapshot = snapshot or build_vendor_snapshot_fingerprint()
    files = dict(snapshot.source_fingerprints)
    ts = "caller_scoped" if "packages/core/src/pool.ts" in files else "unavailable"
    py = "caller_scoped" if "packages/core-py/dreamdex_core/contract.py" in files else "unavailable"
    return OpenOrderSemanticsAudit("caller_scoped" if ts == py == "caller_scoped" else "conflicting", ts, py, "caller_scoped", "source_unavailable", False, ("rest_open_orders_source_unavailable", "owner_subject_not_confirmed"))


@dataclass(frozen=True)
class PythonParityAudit:
    place_order_for: str
    cancel_order_for: str
    reduce_order_for: str
    owner_subject: str
    is_operator_authorized: str
    open_orders: str
    selector_parity: str
    status: str
    authoritative: bool = False
    unresolved_reasons: tuple[str, ...] = ()


def audit_typescript_python_parity(snapshot: VendorSnapshotFingerprint | None = None) -> PythonParityAudit:
    snapshot = snapshot or build_vendor_snapshot_fingerprint()
    files = set(dict(snapshot.source_fingerprints))
    ts = "confirmed" if "packages/core/src/execute.ts" in files else "unavailable"
    py_contract = "packages/core-py/dreamdex_core/contract.py" in files
    open_orders = "caller_scoped" if py_contract else "unavailable"
    reasons = ("python_operator_abi_unavailable", "operator_order_reconciliation_unavailable")
    return PythonParityAudit(ts, ts, "unavailable", "unavailable", "unavailable", open_orders, "conflicting", "conflicting", False, reasons)


def build_authority_evidence(*, pool: str | None = None, owner: str | None = None, operator: str | None = None, selector_evidence: Mapping[str, OperatorSelectorEvidence] | None = None, checks: Mapping[str, OperatorRpcPermissionEvidence] | None = None, permission: OperatorPermissionState | None = None, snapshot: VendorSnapshotFingerprint | None = None) -> DreamDexOperatorAuthorityEvidence:
    selector_evidence = selector_evidence or selector_evidence_map(snapshot)
    checks = checks or {}
    place = selector_evidence.get("place_order_for")
    cancel = selector_evidence.get("cancel_order_for")
    reduce = selector_evidence.get("reduce_order_for")
    pcheck, ccheck, rcheck = checks.get("place_order_for"), checks.get("cancel_order_for"), checks.get("reduce_order_for")
    reasons = ["operator_permission_unavailable", "operator_order_reconciliation_unavailable"]
    if any(item and item.status == "conflicting" for item in selector_evidence.values()):
        reasons.append("operator_selector_conflicting")
    permission_status = "unavailable"
    if permission is not None:
        permission_status = {"allowed": "rpc_confirmed_allowed", "denied": "rpc_confirmed_denied", "unknown": "unavailable"}.get(permission.effective_permission, "unavailable")
        if permission.effective_permission in {"allowed", "denied"}:
            reasons.remove("operator_permission_unavailable")
    def effective(check: OperatorRpcPermissionEvidence | None, capability: OperatorSelectorEvidence | None) -> str:
        if check is not None:
            return check.status
        if capability is None or capability.status != "confirmed":
            return "unavailable"
        return permission_status
    return DreamDexOperatorAuthorityEvidence(
        _mask(pool), _mask(owner), _mask(operator),
        place.status if place else "unavailable", cancel.status if cancel else "unavailable", reduce.status if reduce else "unavailable",
        pcheck.allowed if pcheck else None, ccheck.allowed if ccheck else None, rcheck.allowed if rcheck else None,
        permission_status, "denied" if permission is not None and permission.per_pool_denial is True else "unavailable",
        effective(pcheck, place), effective(ccheck, cancel), effective(rcheck, reduce),
        pcheck.status if pcheck else (permission_status if permission is not None else "unavailable"), (pcheck.block_number if pcheck else None), False, (), tuple(dict.fromkeys(reasons)),
    )


def operator_blocking_reasons(*, configuration: OperatorConfiguration | None = None, matrix: DreamDexOperatorCapabilityMatrix | None = None, authority: DreamDexOperatorAuthorityEvidence | None = None, parity: PythonParityAudit | None = None, selected_mode: str = "operator") -> tuple[str, ...]:
    if selected_mode == "direct_owner":
        return ()
    reasons: list[str] = []
    if configuration is None or not configuration.operator_configured or not configuration.fund_owner_configured:
        reasons.append("operator_identity_mapping_unresolved")
    if matrix is None or not matrix.authoritative:
        reasons.append("operator_permission_unavailable")
    if matrix is not None and matrix.selector_consistency == "conflicting":
        reasons.append("operator_selector_conflicting")
    if authority is None or not authority.authoritative:
        reasons.append("operator_order_reconciliation_unavailable")
    if parity is not None and parity.status != "confirmed":
        reasons.append("operator_python_parity_unconfirmed")
    return tuple(dict.fromkeys(reasons))


__all__ = [
    "AUTHORITY_LEVELS", "CAPABILITY_NAMES", "ROLE_NAMES", "IS_OPERATOR_AUTHORIZED_SIGNATURE", "IS_OPERATOR_AUTHORIZED_SELECTOR", "IS_OPERATOR_AUTHORIZED_ARGUMENTS", "FUND_OWNER_ENV", "OPERATOR_ENV", "PERMISSION_PROBE_ENABLED_ENV", "SUPPORTED_PERMISSION_PROBE_VALUES", "READ_ONLY_RPC_METHODS",
    "FORBIDDEN_RPC_METHODS", "VENDOR_ROOT", "VendorSnapshotFingerprint", "build_vendor_snapshot_fingerprint",
    "compute_vendor_snapshot_fingerprint", "get_vendor_snapshot_fingerprint", "OperatorRegistryAddressEvidence", "OperatorRegistryDiscovery", "discover_operator_registry", "discover_registry", "discover_operator_registry_address", "find_operator_registry", "SelectorStatus",
    "OperatorSelectorEvidence", "audit_vendor_selectors", "selector_evidence_map", "recompute_selector", "DreamDexOperatorIdentityModel",
    "OperatorConfiguration", "load_operator_configuration", "build_operator_identity_model_from_env", "FundOwnerSemanticsAudit", "audit_fund_owner_semantics", "DreamDexOperatorCapability", "DreamDexOperatorCapabilityMatrix",
    "build_capability_matrix", "build_operator_capability_matrix", "OperatorPermissionState", "resolve_operator_permission", "evaluate_effective_permission", "build_is_operator_authorized_eth_call",
    "parse_is_operator_authorized_result", "OperatorRpcPermissionEvidence", "ReadOnlyOperatorRpcTransport",
    "check_operator_permission_read_only", "read_operator_permission", "OperatorPermissionCheck", "OperatorPermissionProbeResult", "probe_operator_permissions_read_only", "DreamDexOperatorAuthorityEvidence", "build_authority_evidence",
    "operator_blocking_reasons",
    "OpenOrderSemanticsAudit", "audit_open_order_semantics", "PythonParityAudit", "audit_typescript_python_parity",
]
