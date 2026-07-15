"""Strict, deliberately small SIWE message-signing boundary.

This module contains no key loader and no production signing implementation.
The only supported capability is signing the exact SIWE login message supplied
by the authentication state machine.  Fixture signers are intended solely for
offline tests.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
from enum import Enum
from typing import Mapping, Protocol

from .dreamdex_auth_models import _mask, _normalize_auth_address


SIWE_LOGIN_CAPABILITY = "siwe_login_message"
SIGNER_CAPABILITIES = frozenset({SIWE_LOGIN_CAPABILITY})


class SiweSignerCapability(str, Enum):
    siwe_login_message = SIWE_LOGIN_CAPABILITY


SignerCapability = SiweSignerCapability


class DreamDexSiweMessageSigner(Protocol):
    """Minimal SIWE-only signer interface."""

    def get_address(self) -> str: ...

    def sign_message(self, message: str) -> str: ...


@dataclass(frozen=True)
class SignerStatus:
    configured: bool
    status: str
    capability: str | None
    address: str | None

    def safe_dict(self) -> dict[str, object]:
        return {
            "configured": self.configured,
            "status": self.status,
            "capability": self.capability,
            "address": _mask(self.address),
        }


class UnavailableSiweSigner:
    """Production-safe default: there is intentionally no signer."""

    configured = False
    status = "unavailable"
    capabilities = frozenset()
    capability = None
    address = None

    def get_address(self) -> str:
        raise RuntimeError("signer_unavailable")

    def sign_message(self, message: str) -> str:
        raise RuntimeError("signer_unavailable")

    def __repr__(self) -> str:
        return "UnavailableSiweSigner(configured=False, status='unavailable', address='<unresolved>')"


def build_production_siwe_signer_from_env(environ: Mapping[str, str] | None = None) -> UnavailableSiweSigner:
    """Return the fail-closed production default.

    ``environ`` is accepted for deterministic tests, but is deliberately not
    inspected: no secret-bearing setting is ever read by this factory.
    """
    del environ
    return UnavailableSiweSigner()


build_siwe_signer_from_env = build_production_siwe_signer_from_env
RejectingUnconfiguredSiweSigner = UnavailableSiweSigner


class FixtureSiweMessageSigner:
    """Non-cryptographic signer used by offline tests only."""

    configured = True
    status = "configured"
    capabilities = SIGNER_CAPABILITIES

    def __init__(
        self,
        address: str,
        *,
        expected_message: str | None = None,
        fixture_signature: str | None = None,
        reject: bool = False,
        malformed: bool = False,
    ) -> None:
        self._address = _normalize_auth_address(address)
        self.expected_message = expected_message
        self.fixture_signature = fixture_signature or ("0x" + "11" * 65)
        self.reject = reject
        self.malformed = malformed
        self.call_count = 0
        self.last_message_fingerprint: str | None = None

    def get_address(self) -> str:
        return self._address

    @property
    def address(self) -> str:
        # Compatibility for callers that only expose an address property.
        return self._address

    def sign_message(self, message: str) -> str:
        if not isinstance(message, str) or not message:
            raise ValueError("message_required")
        self.call_count += 1
        self.last_message_fingerprint = hashlib.sha256(message.encode("utf-8")).hexdigest()
        if self.expected_message is not None and message != self.expected_message:
            raise ValueError("unexpected_siwe_message")
        if self.reject:
            raise RuntimeError("fixture_signer_rejected")
        if self.malformed:
            return "0xmalformed-fixture-signature"
        return self.fixture_signature

    def __repr__(self) -> str:
        return f"FixtureSiweMessageSigner(address={_mask(self._address)!r}, configured=True, calls={self.call_count})"


FixtureMessageSigner = FixtureSiweMessageSigner
FixtureSiweSigner = FixtureSiweMessageSigner
ProductionSiweSignerUnavailable = UnavailableSiweSigner


def signer_status(signer: object | None) -> SignerStatus:
    if signer is None:
        return SignerStatus(False, "unavailable", None, None)
    configured_value = getattr(signer, "configured", None)
    configured = bool(hasattr(signer, "sign_message") and (hasattr(signer, "get_address") or hasattr(signer, "address"))) if configured_value is None else bool(configured_value)
    status = str(getattr(signer, "status", "configured" if configured else "unavailable"))
    capabilities = getattr(signer, "capabilities", frozenset())
    capability = SIWE_LOGIN_CAPABILITY if SIWE_LOGIN_CAPABILITY in capabilities else None
    address = None
    try:
        address = signer.get_address() if configured and hasattr(signer, "get_address") else getattr(signer, "address", None)
    except Exception:
        address = None
    return SignerStatus(configured, status, capability, _normalize_auth_address(address) if address else None)


def resolve_auth_mode(*, manual_bearer_configured: bool, managed_siwe_configured: bool) -> str:
    """Return the explicit read-only authentication mode without choosing silently."""
    if manual_bearer_configured and managed_siwe_configured:
        return "conflicting_configuration"
    if managed_siwe_configured:
        return "managed_siwe"
    if manual_bearer_configured:
        return "manual_bearer_read_only"
    return "none"


__all__ = [
    "SIWE_LOGIN_CAPABILITY", "SIGNER_CAPABILITIES", "SiweSignerCapability", "SignerCapability", "DreamDexSiweMessageSigner",
    "SignerStatus", "UnavailableSiweSigner", "FixtureSiweMessageSigner",
    "FixtureMessageSigner", "build_production_siwe_signer_from_env", "build_siwe_signer_from_env",
    "FixtureSiweSigner", "ProductionSiweSignerUnavailable", "RejectingUnconfiguredSiweSigner",
    "signer_status", "resolve_auth_mode",
]
