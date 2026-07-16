"""Explicit secret-provider boundary for encrypted DreamDEX keystores.

The normal diagnostics path never constructs or invokes a provider.  Providers
return a passphrase only to an explicit signer workflow and never persist it in
diagnostic models or journal records.
"""
from __future__ import annotations

from dataclasses import dataclass
from getpass import getpass
from typing import Any, Callable, Protocol, runtime_checkable

from bot.execution.dreamdex_execution_primitives import mask_evm_address

SCHEMA_VERSION = "1"


@dataclass(frozen=True, repr=False)
class DreamDexKeystorePassphraseRequest:
    keystore_label: str
    masked_signer_address: str
    purpose: str
    interaction_status: str = "explicit_interactive"

    def __post_init__(self) -> None:
        for name in ("keystore_label", "masked_signer_address", "purpose", "interaction_status"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value or len(value) > 200:
                raise ValueError(f"{name}_invalid")
        if any(token in self.keystore_label.lower() for token in ("password", "secret", "private", "key_material")):
            raise ValueError("keystore_label_must_not_contain_secret")

    def safe_dict(self) -> dict[str, str]:
        return {
            "schema_version": SCHEMA_VERSION,
            "keystore_label": self.keystore_label,
            "masked_signer_address": self.masked_signer_address,
            "purpose": self.purpose,
            "interaction_status": self.interaction_status,
        }

    def __repr__(self) -> str:
        return f"DreamDexKeystorePassphraseRequest(label={self.keystore_label!r}, signer={self.masked_signer_address!r}, purpose={self.purpose!r})"


DreamDexKeystoreSecretRequest = DreamDexKeystorePassphraseRequest


@dataclass(frozen=True, repr=False)
class DreamDexKeystoreSecretProviderCapabilities:
    provider_type: str
    status: str
    interactive: bool
    unattended: bool
    secret_persistence: str = "none"
    blockers: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.status not in {"available", "partial", "unavailable"}:
            raise ValueError("secret_provider_status_invalid")
        object.__setattr__(self, "blockers", tuple(dict.fromkeys(str(x) for x in self.blockers if str(x))))

    def safe_dict(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}

    def __repr__(self) -> str:
        return f"DreamDexKeystoreSecretProviderCapabilities(type={self.provider_type!r}, status={self.status!r}, secret_persistence='none')"


@runtime_checkable
class DreamDexKeystoreSecretProvider(Protocol):
    def describe_capabilities(self) -> DreamDexKeystoreSecretProviderCapabilities: ...
    def obtain_passphrase(self, request: DreamDexKeystorePassphraseRequest) -> str | None: ...
    def release_passphrase_reference(self, reference: Any = None) -> None: ...


class UnavailableDreamDexKeystoreSecretProvider:
    def describe_capabilities(self) -> DreamDexKeystoreSecretProviderCapabilities:
        return DreamDexKeystoreSecretProviderCapabilities("unavailable", "unavailable", False, False, "none", ("keystore_secret_provider_unavailable",))

    def obtain_passphrase(self, request: DreamDexKeystorePassphraseRequest) -> str | None:
        if not isinstance(request, DreamDexKeystorePassphraseRequest):
            raise TypeError("typed_passphrase_request_required")
        return None

    def release_passphrase_reference(self, reference: Any = None) -> None:
        return None


class InteractiveDreamDexKeystoreSecretProvider:
    """One explicit, non-echoing prompt with no retry or persistence."""

    def __init__(self, *, prompt_fn: Callable[[str], str] | None = None, maximum_invocations: int = 1) -> None:
        if isinstance(maximum_invocations, bool) or not isinstance(maximum_invocations, int) or maximum_invocations != 1:
            raise ValueError("interactive_provider_single_attempt_required")
        self._prompt_fn = prompt_fn or getpass
        self._invocation_count = 0

    @property
    def invocation_count(self) -> int:
        return self._invocation_count

    def describe_capabilities(self) -> DreamDexKeystoreSecretProviderCapabilities:
        return DreamDexKeystoreSecretProviderCapabilities("interactive", "available", True, False, "none")

    def obtain_passphrase(self, request: DreamDexKeystorePassphraseRequest) -> str | None:
        if not isinstance(request, DreamDexKeystorePassphraseRequest):
            raise TypeError("typed_passphrase_request_required")
        if self._invocation_count >= 1:
            raise RuntimeError("secret_provider_attempt_limit_reached")
        self._invocation_count += 1
        try:
            value = self._prompt_fn(f"Keystore passphrase for {request.keystore_label} ({request.masked_signer_address}): ")
        except Exception:
            raise RuntimeError("secret_provider_failed") from None
        if not isinstance(value, str) or not value:
            raise RuntimeError("secret_provider_empty")
        return value

    def release_passphrase_reference(self, reference: Any = None) -> None:
        return None


class WindowsCredentialManagerDreamDexKeystoreSecretProvider:
    """Optional provider using the installed public ``win32cred`` API only."""

    def __init__(self) -> None:
        try:
            import win32cred  # type: ignore
        except Exception:
            win32cred = None
        self._win32cred = win32cred

    def describe_capabilities(self) -> DreamDexKeystoreSecretProviderCapabilities:
        if self._win32cred is None:
            return DreamDexKeystoreSecretProviderCapabilities("windows_credential_manager", "unavailable", False, False, "none", ("windows_credential_api_unavailable",))
        return DreamDexKeystoreSecretProviderCapabilities("windows_credential_manager", "partial", False, True, "os_backed", ("credential_target_must_be_explicit",))

    def obtain_passphrase(self, request: DreamDexKeystorePassphraseRequest) -> str | None:
        if not isinstance(request, DreamDexKeystorePassphraseRequest):
            raise TypeError("typed_passphrase_request_required")
        if self._win32cred is None:
            return None
        try:
            credential = self._win32cred.CredRead(request.keystore_label, self._win32cred.CRED_TYPE_GENERIC)
            blob = credential.get("CredentialBlob")
            if isinstance(blob, bytes):
                value = blob.decode("utf-8")
            elif isinstance(blob, str):
                value = blob
            else:
                return None
            return value or None
        except Exception:
            return None

    def release_passphrase_reference(self, reference: Any = None) -> None:
        return None


def build_keystore_passphrase_request(*, keystore_label: str, signer_address: str, purpose: str) -> DreamDexKeystorePassphraseRequest:
    return DreamDexKeystorePassphraseRequest(keystore_label, mask_evm_address(signer_address), purpose)


__all__ = [
    "SCHEMA_VERSION",
    "DreamDexKeystorePassphraseRequest",
    "DreamDexKeystoreSecretRequest",
    "DreamDexKeystoreSecretProviderCapabilities",
    "DreamDexKeystoreSecretProvider",
    "UnavailableDreamDexKeystoreSecretProvider",
    "InteractiveDreamDexKeystoreSecretProvider",
    "WindowsCredentialManagerDreamDexKeystoreSecretProvider",
    "build_keystore_passphrase_request",
]
