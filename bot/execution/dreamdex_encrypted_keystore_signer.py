"""Encrypted Ethereum keystore signer at the existing DreamDEX signer boundary.

This module is intentionally explicit: it reads a caller-supplied keystore
path only when a signer method is called, obtains a passphrase through a typed
provider, signs approved finalized material in memory, and returns the existing
ephemeral signed-transaction model.  It never submits, polls, or performs RPC.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Any

from eth_account import Account
from eth_utils import to_checksum_address

from bot.execution.dreamdex_execution_primitives import (
    deterministic_fingerprint,
    mask_evm_address,
    mask_hex_hash,
    validate_evm_address,
)
from bot.execution.dreamdex_secret_provider import (
    DreamDexKeystorePassphraseRequest,
    DreamDexKeystoreSecretProvider,
    UnavailableDreamDexKeystoreSecretProvider,
    build_keystore_passphrase_request,
)
from bot.execution.dreamdex_signed_transaction import (
    DreamDexEphemeralSignedTransaction,
    DreamDexTransactionSigningMaterial,
    validate_transaction_signing_material,
)
from bot.execution.dreamdex_transaction_envelope import validate_unsigned_transaction_envelope
from bot.execution.dreamdex_transaction_signer import (
    ALLOWED_OPERATIONS,
    ALLOWED_SELECTORS,
    ALLOWED_TARGET_ADDRESSES,
    DreamDexTransactionSignerCapabilities,
)

SCHEMA_VERSION = "1"
KEYSTORE_V3 = 3
DEFAULT_MAX_KEYSTORE_BYTES = 1_048_576
_TOP_LEVEL_FIELDS = frozenset({"address", "id", "version", "crypto"})
_CRYPTO_FIELDS = frozenset({"ciphertext", "cipherparams", "cipher", "kdf", "kdfparams", "mac"})


def _tuple(values: Any) -> tuple[str, ...]:
    return tuple(dict.fromkeys(str(value) for value in (values or ()) if str(value)))


def _normalise_address(value: Any, field: str) -> str:
    if isinstance(value, str) and re.fullmatch(r"[0-9a-fA-F]{40}", value):
        value = "0x" + value
    return validate_evm_address(value, field=field)  # type: ignore[return-value]


def _fingerprint(value: Any) -> str:
    return deterministic_fingerprint(value, domain="dreamdex/encrypted-keystore-metadata")


def _pairs_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    seen: set[str] = set()
    for key, value in pairs:
        if not isinstance(key, str):
            raise ValueError("keystore_key_invalid")
        folded = key.casefold()
        if folded in seen:
            raise ValueError("keystore_duplicate_critical_key")
        seen.add(folded)
        result[key] = value
    return result


@dataclass(frozen=True, repr=False)
class DreamDexEncryptedKeystorePolicy:
    schema_version: str = SCHEMA_VERSION
    expected_signer_address: str | None = None
    allowed_chain_ids: tuple[int, ...] = (5031,)
    allowed_target_addresses: tuple[str, ...] = ALLOWED_TARGET_ADDRESSES
    allowed_operations: tuple[str, ...] = ALLOWED_OPERATIONS
    allowed_selectors: tuple[tuple[str, str], ...] = ALLOWED_SELECTORS
    maximum_keystore_file_size_bytes: int = DEFAULT_MAX_KEYSTORE_BYTES
    require_absolute_path: bool = True
    reject_repository_paths: bool = True
    reject_vendor_paths: bool = True
    reject_symlinks: bool = True
    require_regular_file: bool = True
    require_keystore_v3: bool = True
    require_address_field: bool = True
    require_exact_public_address_match: bool = True
    require_exact_derived_address_match: bool = True
    maximum_unlock_attempts: int = 1
    allow_interactive_unlock: bool = True
    allow_unattended_unlock: bool = False
    allow_key_export: bool = False
    allow_raw_key_output: bool = False
    allow_real_submission: bool = False
    authoritative: bool = False
    unresolved_reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError("keystore_policy_schema_invalid")
        expected = None if self.expected_signer_address is None else _normalise_address(self.expected_signer_address, "expected_signer_address")
        object.__setattr__(self, "expected_signer_address", expected)
        if not self.allowed_chain_ids or any(isinstance(x, bool) or not isinstance(x, int) or x < 0 for x in self.allowed_chain_ids):
            raise ValueError("allowed_chain_ids_invalid")
        object.__setattr__(self, "allowed_chain_ids", tuple(dict.fromkeys(self.allowed_chain_ids)))
        object.__setattr__(self, "allowed_target_addresses", tuple(_normalise_address(x, "allowed_target_address") for x in self.allowed_target_addresses))
        object.__setattr__(self, "allowed_selectors", tuple((str(op), str(selector).lower()) for op, selector in self.allowed_selectors))
        if not self.allowed_target_addresses or not set(self.allowed_operations).issubset(set(ALLOWED_OPERATIONS)):
            raise ValueError("keystore_operation_allowlist_invalid")
        selector_map = dict(self.allowed_selectors)
        if any(not re.fullmatch(r"0x[0-9a-f]{8}", selector) for selector in selector_map.values()) or any(selector_map.get(operation) != dict(ALLOWED_SELECTORS).get(operation) for operation in self.allowed_operations):
            raise ValueError("keystore_selector_allowlist_invalid")
        if isinstance(self.maximum_keystore_file_size_bytes, bool) or not isinstance(self.maximum_keystore_file_size_bytes, int) or self.maximum_keystore_file_size_bytes <= 0:
            raise ValueError("maximum_keystore_file_size_invalid")
        if isinstance(self.maximum_unlock_attempts, bool) or not isinstance(self.maximum_unlock_attempts, int) or self.maximum_unlock_attempts != 1:
            raise ValueError("maximum_unlock_attempts_must_be_one")
        object.__setattr__(self, "allow_key_export", False)
        object.__setattr__(self, "allow_raw_key_output", False)
        object.__setattr__(self, "allow_real_submission", False)
        object.__setattr__(self, "authoritative", False)
        object.__setattr__(self, "unresolved_reasons", _tuple(self.unresolved_reasons))

    @property
    def complete(self) -> bool:
        return bool(self.expected_signer_address and self.allowed_chain_ids and self.allowed_target_addresses and not self.unresolved_reasons)

    def safe_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "expected_signer_address_masked": mask_evm_address(self.expected_signer_address),
            "allowed_chain_ids": self.allowed_chain_ids,
            "allowed_target_addresses_masked": tuple(mask_evm_address(x) for x in self.allowed_target_addresses),
            "allowed_operations": self.allowed_operations,
            "allowed_selectors": self.allowed_selectors,
            "maximum_keystore_file_size_bytes": self.maximum_keystore_file_size_bytes,
            "maximum_unlock_attempts": self.maximum_unlock_attempts,
            "allow_interactive_unlock": self.allow_interactive_unlock,
            "allow_unattended_unlock": False,
            "allow_key_export": False,
            "allow_raw_key_output": False,
            "allow_real_submission": False,
            "authoritative": False,
            "complete": self.complete,
            "unresolved_reasons": self.unresolved_reasons,
        }

    def __repr__(self) -> str:
        return f"DreamDexEncryptedKeystorePolicy(expected_signer={mask_evm_address(self.expected_signer_address)!r}, max_unlock_attempts=1, allow_real_submission=False)"


@dataclass(frozen=True, repr=False)
class DreamDexEncryptedKeystoreMetadata:
    schema_version: str
    keystore_status: str
    keystore_version: int | None
    public_address: str | None
    public_address_match: bool | None
    crypto_section_present: bool
    cipher_name: str | None
    kdf_name: str | None
    file_size_bytes: int | None
    file_path_status: str
    symlink_status: str
    metadata_fingerprint: str
    authoritative: bool
    blockers: tuple[str, ...] = ()
    validation_errors: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError("keystore_metadata_schema_invalid")
        if self.public_address is not None:
            object.__setattr__(self, "public_address", _normalise_address(self.public_address, "public_address"))
        object.__setattr__(self, "authoritative", False)
        object.__setattr__(self, "blockers", _tuple(self.blockers))
        object.__setattr__(self, "validation_errors", _tuple(self.validation_errors))

    def safe_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "keystore_status": self.keystore_status,
            "keystore_version": self.keystore_version,
            "public_address_masked": mask_evm_address(self.public_address),
            "public_address_match": self.public_address_match,
            "crypto_section_present": self.crypto_section_present,
            "cipher_name": self.cipher_name,
            "kdf_name": self.kdf_name,
            "file_size_bytes": self.file_size_bytes,
            "file_path_status": self.file_path_status,
            "symlink_status": self.symlink_status,
            "metadata_fingerprint": mask_hex_hash(self.metadata_fingerprint),
            "authoritative": False,
            "raw_keystore_output_allowed": False,
            "blockers": self.blockers,
            "validation_errors": self.validation_errors,
        }

    def __repr__(self) -> str:
        return f"DreamDexEncryptedKeystoreMetadata(status={self.keystore_status!r}, address={mask_evm_address(self.public_address)!r}, path={self.file_path_status!r}, authoritative=False)"


def _metadata_failure(*, status: str, version: int | None = None, address: str | None = None, size: int | None = None, path_status: str = "rejected", symlink_status: str = "unavailable", crypto: bool = False, cipher: str | None = None, kdf: str | None = None, blockers: tuple[str, ...] = (), errors: tuple[str, ...] = ()) -> DreamDexEncryptedKeystoreMetadata:
    return DreamDexEncryptedKeystoreMetadata(SCHEMA_VERSION, status, version, address, None, crypto, cipher, kdf, size, path_status, symlink_status, _fingerprint({"status": status, "version": version, "address": mask_evm_address(address), "size": size, "path_status": path_status, "symlink_status": symlink_status, "crypto": crypto, "cipher": cipher, "kdf": kdf, "blockers": blockers, "errors": errors}), False, blockers, errors)


def _within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def inspect_encrypted_keystore(path: str | Path, policy: DreamDexEncryptedKeystorePolicy | None = None) -> DreamDexEncryptedKeystoreMetadata:
    policy = policy or DreamDexEncryptedKeystorePolicy()
    if not isinstance(path, (str, Path)):
        return _metadata_failure(status="rejected", blockers=("keystore_path_invalid",), errors=("keystore_path_invalid",))
    candidate = Path(path).expanduser()
    if policy.require_absolute_path and not candidate.is_absolute():
        return _metadata_failure(status="rejected", path_status="relative", blockers=("encrypted_keystore_path_unsafe",), errors=("keystore_path_must_be_absolute",))
    try:
        if policy.reject_symlinks and candidate.is_symlink():
            return _metadata_failure(status="rejected", path_status="symlink", symlink_status="rejected", blockers=("encrypted_keystore_path_unsafe",), errors=("keystore_symlink_rejected",))
        resolved = candidate.resolve(strict=False)
    except Exception:
        return _metadata_failure(status="rejected", blockers=("encrypted_keystore_path_unsafe",), errors=("keystore_path_unavailable",))
    repo_root = Path(__file__).resolve().parents[2]
    if policy.reject_repository_paths and _within(resolved, repo_root):
        return _metadata_failure(status="rejected", path_status="repository_rejected", symlink_status="not_symlink", blockers=("encrypted_keystore_path_unsafe",), errors=("keystore_repository_path_rejected",))
    vendor_root = repo_root / "vendor"
    if policy.reject_vendor_paths and _within(resolved, vendor_root):
        return _metadata_failure(status="rejected", path_status="vendor_rejected", symlink_status="not_symlink", blockers=("encrypted_keystore_path_unsafe",), errors=("keystore_vendor_path_rejected",))
    try:
        stat = candidate.stat()
    except OSError:
        return _metadata_failure(status="unavailable", path_status="unavailable", symlink_status="not_symlink", blockers=("encrypted_keystore_unavailable",), errors=("keystore_file_unavailable",))
    size = int(stat.st_size)
    if policy.require_regular_file and not candidate.is_file():
        return _metadata_failure(status="rejected", size=size, path_status="not_regular_file", symlink_status="not_symlink", blockers=("encrypted_keystore_path_unsafe",), errors=("keystore_regular_file_required",))
    if size > policy.maximum_keystore_file_size_bytes:
        return _metadata_failure(status="rejected", size=size, path_status="accepted", symlink_status="not_symlink", blockers=("encrypted_keystore_malformed",), errors=("keystore_file_oversized",))
    try:
        raw = candidate.read_bytes()
        parsed = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs_object)
    except Exception:
        return _metadata_failure(status="malformed", size=size, path_status="accepted", symlink_status="not_symlink", blockers=("encrypted_keystore_malformed",), errors=("keystore_json_invalid",))
    if not isinstance(parsed, dict):
        return _metadata_failure(status="malformed", size=size, path_status="accepted", symlink_status="not_symlink", blockers=("encrypted_keystore_malformed",), errors=("keystore_json_object_required",))
    fields = {str(k).casefold(): v for k, v in parsed.items()}
    unknown = tuple(sorted(set(fields) - _TOP_LEVEL_FIELDS))
    blockers: list[str] = []
    errors: list[str] = []
    if unknown:
        blockers.append("encrypted_keystore_malformed"); errors.append("keystore_unknown_top_level_field")
    version = fields.get("version")
    if policy.require_keystore_v3 and version != KEYSTORE_V3:
        blockers.append("encrypted_keystore_version_unsupported"); errors.append("keystore_v3_required")
    address: str | None = None
    raw_address = fields.get("address")
    if raw_address is None and policy.require_address_field:
        blockers.append("encrypted_keystore_malformed"); errors.append("keystore_address_missing")
    elif raw_address is not None:
        try:
            address = _normalise_address(raw_address, "public_address")
        except ValueError:
            blockers.append("encrypted_keystore_malformed"); errors.append("keystore_address_invalid")
    match: bool | None = None
    if policy.expected_signer_address is None:
        blockers.append("encrypted_keystore_public_address_mismatch"); errors.append("expected_signer_address_unresolved")
    elif address is not None:
        match = address.lower() == policy.expected_signer_address.lower()
        if not match:
            blockers.append("encrypted_keystore_public_address_mismatch"); errors.append("keystore_public_address_mismatch")
    crypto = fields.get("crypto")
    if crypto is None:
        crypto = fields.get("Crypto")
    crypto_present = isinstance(crypto, dict)
    cipher = kdf = None
    if not crypto_present:
        blockers.append("encrypted_keystore_malformed"); errors.append("keystore_crypto_section_missing")
    else:
        crypto_fields = {str(k).casefold(): v for k, v in crypto.items()}
        if set(crypto_fields) - _CRYPTO_FIELDS or not _CRYPTO_FIELDS <= set(crypto_fields):
            blockers.append("encrypted_keystore_malformed"); errors.append("keystore_crypto_fields_invalid")
        cipher = crypto_fields.get("cipher") if isinstance(crypto_fields.get("cipher"), str) else None
        kdf = crypto_fields.get("kdf") if isinstance(crypto_fields.get("kdf"), str) else None
        if not cipher or not kdf:
            blockers.append("encrypted_keystore_malformed"); errors.append("keystore_crypto_algorithm_missing")
    status = "valid" if not blockers and not errors else "malformed"
    return DreamDexEncryptedKeystoreMetadata(SCHEMA_VERSION, status, version if isinstance(version, int) else None, address, match, crypto_present, cipher, kdf, size, "accepted", "not_symlink", _fingerprint({"status": status, "version": version, "address": mask_evm_address(address), "size": size, "crypto_present": crypto_present, "cipher": cipher, "kdf": kdf, "blockers": tuple(dict.fromkeys(blockers)), "errors": tuple(dict.fromkeys(errors))}), False, tuple(dict.fromkeys(blockers)), tuple(dict.fromkeys(errors)))


@dataclass(frozen=True, repr=False)
class DreamDexKeystoreUnlockResult:
    schema_version: str
    unlock_status: str
    secret_provider_type: str
    secret_provider_invoked: bool
    unlock_attempt_count: int
    public_address_match: bool | None
    derived_address: str | None
    derived_address_match: bool | None
    key_material_received: bool
    key_material_reference_released: bool
    secure_memory_zeroization_guaranteed: bool
    unlock_fingerprint: str
    authoritative: bool
    blockers: tuple[str, ...] = ()
    validation_errors: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError("keystore_unlock_schema_invalid")
        if isinstance(self.unlock_attempt_count, bool) or not isinstance(self.unlock_attempt_count, int) or self.unlock_attempt_count < 0:
            raise ValueError("unlock_attempt_count_invalid")
        if self.derived_address is not None:
            object.__setattr__(self, "derived_address", _normalise_address(self.derived_address, "derived_address"))
        object.__setattr__(self, "authoritative", False)
        object.__setattr__(self, "secure_memory_zeroization_guaranteed", False)
        object.__setattr__(self, "blockers", _tuple(self.blockers)); object.__setattr__(self, "validation_errors", _tuple(self.validation_errors))

    def safe_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "unlock_status": self.unlock_status,
            "secret_provider_type": self.secret_provider_type,
            "secret_provider_invoked": self.secret_provider_invoked,
            "unlock_attempt_count": self.unlock_attempt_count,
            "public_address_match": self.public_address_match,
            "derived_address_masked": mask_evm_address(self.derived_address),
            "derived_address_match": self.derived_address_match,
            "key_material_received": self.key_material_received,
            "key_material_persisted": False,
            "key_material_reference_released": self.key_material_reference_released,
            "secure_memory_zeroization_guaranteed": False,
            "authoritative": False,
            "raw_private_key_output_allowed": False,
            "password_output_allowed": False,
            "unlock_fingerprint": mask_hex_hash(self.unlock_fingerprint),
            "blockers": self.blockers,
            "validation_errors": self.validation_errors,
        }

    def __repr__(self) -> str:
        return f"DreamDexKeystoreUnlockResult(status={self.unlock_status!r}, derived={mask_evm_address(self.derived_address)!r}, key_material_persisted=False)"


class EncryptedKeystoreDreamDexTransactionSigner:
    def __init__(self, *, keystore_path: str | Path, expected_signer_address: str, policy: DreamDexEncryptedKeystorePolicy | None = None, secret_provider: DreamDexKeystoreSecretProvider | None = None) -> None:
        expected = _normalise_address(expected_signer_address, "expected_signer_address")
        self.policy = policy or DreamDexEncryptedKeystorePolicy(expected_signer_address=expected)
        if self.policy.expected_signer_address != expected:
            raise ValueError("keystore_expected_signer_policy_mismatch")
        self.keystore_path = Path(keystore_path)
        self.secret_provider = secret_provider or UnavailableDreamDexKeystoreSecretProvider()
        self._unlock_attempts = 0

    def inspect_metadata(self) -> DreamDexEncryptedKeystoreMetadata:
        return inspect_encrypted_keystore(self.keystore_path, self.policy)

    def get_address(self) -> str:
        metadata = self.inspect_metadata()
        return metadata.public_address if metadata.public_address and metadata.public_address_match is not False and not metadata.blockers else "<unavailable>"

    def describe_capabilities(self) -> DreamDexTransactionSignerCapabilities:
        provider = self.secret_provider.describe_capabilities()
        metadata = self.inspect_metadata()
        status = "partial" if metadata.keystore_status == "valid" else "unavailable"
        blockers = ("keystore_unlock_required",) if status == "partial" else metadata.blockers or ("encrypted_keystore_unavailable",)
        if provider.status == "unavailable":
            blockers = tuple(dict.fromkeys((*blockers, "keystore_secret_provider_unavailable")))
        return DreamDexTransactionSignerCapabilities("encrypted_keystore", "available_offline" if metadata.public_address else "unavailable", status, self.policy.allowed_chain_ids, ("legacy", "eip1559"), self.policy.allowed_operations, False, False, False, status, False, blockers)

    def _provider_request(self, purpose: str) -> DreamDexKeystorePassphraseRequest:
        return build_keystore_passphrase_request(keystore_label="dreamdex-keystore", signer_address=self.policy.expected_signer_address or "<unavailable>", purpose=purpose)

    def _decrypt(self, metadata: DreamDexEncryptedKeystoreMetadata, *, purpose: str) -> tuple[bytes, str, str]:
        if metadata.keystore_status != "valid" or not metadata.public_address or metadata.public_address_match is not True:
            raise RuntimeError("encrypted_keystore_metadata_invalid")
        if self._unlock_attempts >= self.policy.maximum_unlock_attempts:
            raise RuntimeError("keystore_unlock_attempt_limit_reached")
        if not self.policy.allow_interactive_unlock and not self.policy.allow_unattended_unlock:
            raise RuntimeError("keystore_unlock_disabled")
        provider_caps = self.secret_provider.describe_capabilities()
        if provider_caps.status == "unavailable":
            raise RuntimeError("keystore_secret_provider_unavailable")
        if provider_caps.interactive and not self.policy.allow_interactive_unlock:
            raise RuntimeError("interactive_keystore_unlock_disabled")
        if provider_caps.unattended and not self.policy.allow_unattended_unlock:
            raise RuntimeError("unattended_keystore_unlock_disabled")
        self._unlock_attempts += 1
        passphrase: str | None = None
        key_material: bytes | None = None
        try:
            passphrase = self.secret_provider.obtain_passphrase(self._provider_request(purpose))
            if not isinstance(passphrase, str) or not passphrase:
                raise RuntimeError("keystore_secret_provider_unavailable")
            payload = self.keystore_path.read_text(encoding="utf-8")
            key_material = Account.decrypt(payload, passphrase)
            if not isinstance(key_material, (bytes, bytearray)) or not key_material:
                raise RuntimeError("keystore_unlock_failed")
            derived = Account.from_key(bytes(key_material)).address
            expected = self.policy.expected_signer_address
            if expected is None or derived.lower() != expected.lower() or derived.lower() != metadata.public_address.lower():
                raise RuntimeError("keystore_derived_address_mismatch")
            return bytes(key_material), derived, provider_caps.provider_type
        except RuntimeError:
            raise
        except Exception:
            raise RuntimeError("keystore_unlock_failed") from None
        finally:
            try:
                self.secret_provider.release_passphrase_reference(None)
            finally:
                passphrase = None
                if key_material is not None:
                    key_material = None

    def unlock_check(self) -> DreamDexKeystoreUnlockResult:
        metadata = self.inspect_metadata()
        provider_type = self.secret_provider.describe_capabilities().provider_type
        if metadata.keystore_status != "valid":
            return DreamDexKeystoreUnlockResult(SCHEMA_VERSION, "blocked", provider_type, False, self._unlock_attempts, metadata.public_address_match, None, None, False, True, False, _fingerprint({"status": "blocked", "metadata": metadata.metadata_fingerprint}), False, metadata.blockers, metadata.validation_errors)
        try:
            key, derived, provider_type = self._decrypt(metadata, purpose="explicit_unlock_check")
            ok = derived.lower() == (self.policy.expected_signer_address or "").lower() == (metadata.public_address or "").lower()
            status = "verified" if ok else "failed"
            blockers = () if ok else ("keystore_derived_address_mismatch",)
            result = DreamDexKeystoreUnlockResult(SCHEMA_VERSION, status, provider_type, True, self._unlock_attempts, True, derived, ok, True, True, False, _fingerprint({"status": status, "address": mask_evm_address(derived), "attempt": self._unlock_attempts}), False, blockers, ())
            key = None
            return result
        except Exception as exc:
            category = str(exc) if str(exc) in {"keystore_unlock_attempt_limit_reached", "keystore_secret_provider_unavailable", "keystore_derived_address_mismatch"} else "keystore_unlock_failed"
            return DreamDexKeystoreUnlockResult(SCHEMA_VERSION, "failed", provider_type, self._unlock_attempts > 0, self._unlock_attempts, metadata.public_address_match, None, False, False, True, False, _fingerprint({"status": "failed", "category": category, "attempt": self._unlock_attempts}), False, (category,), ())

    def _build_transaction(self, envelope: Any) -> dict[str, Any]:
        if envelope.transaction_type not in {"legacy", "eip1559"}:
            raise RuntimeError("keystore_transaction_type_unsupported")
        if any(value is None for value in (envelope.chain_id, envelope.nonce, envelope.gas_limit, envelope.to_address, envelope.value_wei, envelope.calldata)):
            raise RuntimeError("keystore_transaction_fields_unresolved")
        calldata = bytes(envelope.calldata) if isinstance(envelope.calldata, (bytes, bytearray)) else (bytes.fromhex(envelope.calldata[2:]) if isinstance(envelope.calldata, str) and envelope.calldata.startswith("0x") else envelope.calldata)
        transaction: dict[str, Any] = {"chainId": envelope.chain_id, "nonce": envelope.nonce, "to": to_checksum_address(envelope.to_address), "value": envelope.value_wei, "gas": envelope.gas_limit, "data": calldata}
        if envelope.transaction_type == "legacy":
            if envelope.gas_price_wei is None or envelope.gas_price_wei <= 0 or envelope.max_fee_per_gas_wei is not None or envelope.max_priority_fee_per_gas_wei is not None:
                raise RuntimeError("keystore_legacy_fee_fields_invalid")
            transaction["gasPrice"] = envelope.gas_price_wei
        else:
            if envelope.gas_price_wei is not None or envelope.max_fee_per_gas_wei is None or envelope.max_priority_fee_per_gas_wei is None or envelope.max_fee_per_gas_wei <= 0 or envelope.max_priority_fee_per_gas_wei <= 0 or envelope.max_fee_per_gas_wei < envelope.max_priority_fee_per_gas_wei:
                raise RuntimeError("keystore_eip1559_fee_fields_invalid")
            transaction.update({"type": 2, "maxFeePerGas": envelope.max_fee_per_gas_wei, "maxPriorityFeePerGas": envelope.max_priority_fee_per_gas_wei})
        return transaction

    def sign_finalized_transaction(self, material: DreamDexTransactionSigningMaterial) -> DreamDexEphemeralSignedTransaction:
        if not isinstance(material, DreamDexTransactionSigningMaterial):
            raise TypeError("signing_material_typed_inputs_required")
        material_validation = validate_transaction_signing_material(material)
        if not material_validation.valid:
            raise RuntimeError("keystore_signing_material_not_approved")
        if any(not isinstance(value, str) or not value for value in (material.signing_request_fingerprint, material.lease_fingerprint, material.material_fingerprint)):
            raise RuntimeError("keystore_signing_material_fingerprint_invalid")
        expected_material_fingerprint = deterministic_fingerprint(
            {
                "intent_id": material.intent_id,
                "reservation_id": material.reservation_id,
                "lease_id": material.lease_id,
                "operation": material.operation,
                "signing_request_fingerprint": material.signing_request_fingerprint,
                "lease_fingerprint": material.lease_fingerprint,
            },
            domain="dreamdex_signing_material",
        )
        if material.material_fingerprint != expected_material_fingerprint:
            raise RuntimeError("keystore_signing_material_fingerprint_mismatch")
        metadata = self.inspect_metadata()
        if metadata.keystore_status != "valid":
            raise RuntimeError("encrypted_keystore_metadata_invalid")
        envelope = material.finalized_envelope
        selector = "0x" + bytes(envelope.calldata)[:4].hex() if isinstance(envelope.calldata, (bytes, bytearray)) and len(envelope.calldata) >= 4 else None
        expected_selector = dict(self.policy.allowed_selectors).get(envelope.operation)
        if not material.policy_approved or not material.lease_active:
            raise RuntimeError("keystore_signing_material_not_approved")
        if material.operation != envelope.operation or envelope.from_address != self.policy.expected_signer_address:
            raise RuntimeError("keystore_signer_address_mismatch")
        if envelope.chain_id not in self.policy.allowed_chain_ids:
            raise RuntimeError("keystore_chain_id_not_allowlisted")
        if envelope.to_address not in self.policy.allowed_target_addresses:
            raise RuntimeError("keystore_target_not_allowlisted")
        if envelope.operation not in self.policy.allowed_operations or selector != expected_selector:
            raise RuntimeError("keystore_selector_not_allowlisted")
        if envelope.value_wei != 0 or not isinstance(envelope.gas_limit, int) or envelope.gas_limit <= 0:
            raise RuntimeError("keystore_value_or_gas_policy_rejected")
        structural = validate_unsigned_transaction_envelope(envelope)
        if structural.errors:
            raise RuntimeError("keystore_transaction_material_invalid")
        key_material: bytes | None = None
        try:
            key_material, derived, _provider_type = self._decrypt(metadata, purpose="explicit_transaction_signing")
            if derived.lower() != (envelope.from_address or "").lower():
                raise RuntimeError("keystore_derived_address_mismatch")
            transaction = self._build_transaction(envelope)
            signed = Account.sign_transaction(transaction, key_material)
            raw = getattr(signed, "raw_transaction", None)
            if not isinstance(raw, (bytes, bytearray)) or not raw:
                raise RuntimeError("keystore_signed_payload_unavailable")
            recovered = Account.recover_transaction(bytes(raw))
            if recovered.lower() != derived.lower() or recovered.lower() != (envelope.from_address or "").lower():
                raise RuntimeError("keystore_recovered_sender_mismatch")
            return DreamDexEphemeralSignedTransaction(bytes(raw), derived, material.signing_request_fingerprint, material.lease_fingerprint, "encrypted_keystore")
        except RuntimeError:
            raise
        except Exception:
            raise RuntimeError("keystore_signing_failed") from None
        finally:
            key_material = None


__all__ = [
    "SCHEMA_VERSION",
    "DreamDexEncryptedKeystorePolicy",
    "DreamDexEncryptedKeystoreMetadata",
    "DreamDexKeystoreUnlockResult",
    "inspect_encrypted_keystore",
    "EncryptedKeystoreDreamDexTransactionSigner",
]
