"""Offline EIP-191 verification for SIWE messages.

This module verifies signatures only.  It contains no key material, signing
API, network transport, or wallet/order mutation operation.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re
from typing import Any


SECP256K1_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
SECP256K1_HALF_N = SECP256K1_N // 2
_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")


def _mask(value: str | None) -> str:
    if not value:
        return "<missing>"
    return value if value.startswith("<") and value.endswith(">") else ("***" if len(value) <= 8 else f"{value[:4]}...{value[-4:]}")


def _normalize_address(value: Any) -> str | None:
    if not isinstance(value, str) or not _ADDRESS_RE.fullmatch(value):
        return None
    return value.lower()


@dataclass(frozen=True, repr=False)
class DreamDexSiweSignatureVerification:
    status: str
    recovered_address_masked: str
    expected_address_masked: str
    address_match: str
    signature_format_status: str
    message_fingerprint: str
    recovery_performed: bool
    authoritative_for_signer_address: bool
    authoritative_for_dreamdex_wallet_binding: bool
    unresolved_reasons: tuple[str, ...] = ()

    def __repr__(self) -> str:
        return (
            "DreamDexSiweSignatureVerification("
            f"status={self.status!r}, recovered_address={self.recovered_address_masked!r}, "
            f"expected_address={self.expected_address_masked!r}, address_match={self.address_match!r}, "
            f"signature_format_status={self.signature_format_status!r}, recovery_performed={self.recovery_performed}, "
            f"authoritative_for_signer_address={self.authoritative_for_signer_address}, "
            f"authoritative_for_dreamdex_wallet_binding={self.authoritative_for_dreamdex_wallet_binding})"
        )

    def safe_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "recovered_address_masked": self.recovered_address_masked,
            "expected_address_masked": self.expected_address_masked,
            "address_match": self.address_match,
            "signature_format_status": self.signature_format_status,
            "message_fingerprint": self.message_fingerprint,
            "recovery_performed": self.recovery_performed,
            "authoritative_for_signer_address": self.authoritative_for_signer_address,
            "authoritative_for_dreamdex_wallet_binding": self.authoritative_for_dreamdex_wallet_binding,
            "unresolved_reasons": self.unresolved_reasons,
        }


def _result(
    *, status: str, expected: str | None, fingerprint: str, format_status: str,
    recovered: str | None = None, match: str = "unresolved", recovery: bool = False,
    reasons: tuple[str, ...] = (),
) -> DreamDexSiweSignatureVerification:
    return DreamDexSiweSignatureVerification(
        status=status,
        recovered_address_masked=_mask(recovered),
        expected_address_masked=_mask(expected),
        address_match=match,
        signature_format_status=format_status,
        message_fingerprint=fingerprint,
        recovery_performed=recovery,
        authoritative_for_signer_address=status == "valid" and match == "confirmed",
        # A signature proves only control of the signing address, never the
        # platform's owner-to-smart-wallet mapping.
        authoritative_for_dreamdex_wallet_binding=False,
        unresolved_reasons=reasons,
    )


def _parse_signature(signature: Any) -> tuple[bytes | None, str, str | None]:
    if not isinstance(signature, str) or not signature:
        return None, "missing", "signature_missing"
    if any(char.isspace() for char in signature):
        return None, "whitespace", "signature_whitespace"
    body = signature[2:] if signature.startswith("0x") else signature
    if body.startswith("0x") or len(body) != 130:
        return None, "wrong_length", "signature_length"
    if len(body) % 2 or not re.fullmatch(r"[0-9a-fA-F]+", body):
        return None, "malformed_hex", "signature_hex"
    try:
        raw = bytes.fromhex(body)
    except ValueError:
        return None, "malformed_hex", "signature_hex"
    r, s, v = int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:64], "big"), raw[64]
    if r == 0 or s == 0:
        return None, "invalid_rs", "signature_zero_rs"
    if r >= SECP256K1_N or s >= SECP256K1_N:
        return None, "invalid_rs", "signature_rs_range"
    if s > SECP256K1_HALF_N:
        return None, "non_canonical_s", "signature_high_s"
    if v not in {0, 1, 27, 28}:
        return None, "invalid_v", "signature_v"
    return raw, "valid", None


def verify_siwe_signature(message: str, signature: str, expected_owner_address: str) -> DreamDexSiweSignatureVerification:
    """Recover the EIP-191 personal-sign address and compare it exactly."""
    fingerprint = hashlib.sha256(message.encode("utf-8")).hexdigest() if isinstance(message, str) else "<invalid>"
    expected = _normalize_address(expected_owner_address)
    if not isinstance(message, str) or not message:
        return _result(status="message_mismatch", expected=expected, fingerprint=fingerprint, format_status="not_checked", reasons=("message_invalid",))
    if expected is None:
        return _result(status="internal_error", expected=expected_owner_address if isinstance(expected_owner_address, str) else None, fingerprint=fingerprint, format_status="not_checked", reasons=("expected_address_invalid",))
    raw, format_status, reason = _parse_signature(signature)
    if raw is None:
        return _result(status="invalid_format", expected=expected, fingerprint=fingerprint, format_status=format_status, reasons=(reason or "signature_invalid",))
    try:
        from eth_account import Account
        from eth_account.messages import encode_defunct
        recovered = _normalize_address(Account.recover_message(encode_defunct(text=message), signature=raw))
    except ImportError:
        return _result(status="unavailable", expected=expected, fingerprint=fingerprint, format_status=format_status, reasons=("ethereum_recovery_dependency_unavailable",))
    except Exception:
        return _result(status="invalid_recovery", expected=expected, fingerprint=fingerprint, format_status=format_status, recovery=True, reasons=("signature_recovery_failed",))
    if recovered is None:
        return _result(status="invalid_recovery", expected=expected, fingerprint=fingerprint, format_status=format_status, recovery=True, reasons=("recovered_address_invalid",))
    if recovered != expected:
        return _result(status="address_mismatch", expected=expected, fingerprint=fingerprint, format_status=format_status, recovered=recovered, match="conflicting", recovery=True, reasons=("recovered_address_mismatch",))
    return _result(status="valid", expected=expected, fingerprint=fingerprint, format_status=format_status, recovered=recovered, match="confirmed", recovery=True)


__all__ = ["DreamDexSiweSignatureVerification", "verify_siwe_signature", "SECP256K1_N", "SECP256K1_HALF_N"]
