"""Constrained JSONL IPC client for an external SIWE-only signer.

The production factory is intentionally unavailable.  A launcher is injected
by offline tests and never receives the bot's full environment or credentials.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import json
import os
from pathlib import Path
import queue
import subprocess
import threading
import time
from typing import Any, Mapping, Protocol, Sequence
import uuid

from .dreamdex_siwe_signer import SIWE_LOGIN_CAPABILITY, SIGNER_CAPABILITIES
from .dreamdex_siwe_signature_verifier import _normalize_address


PROTOCOL = "dreamdex-siwe-signer/1"
OPERATIONS = frozenset({"describe", "sign_siwe_message"})
RESPONSE_STATUSES = frozenset({"ok", "rejected", "unavailable", "invalid_request", "internal_error"})
EXTERNAL_SIGNER_EXIT_STATUSES = frozenset({"unavailable", "running", "clean", "killed_timeout", "nonzero_exit", "failed"})
EXTERNAL_SIGNER_ADDRESS_MATCH_STATUSES = frozenset({"unresolved", "confirmed", "mismatch"})
EXTERNAL_SIGNER_ENVIRONMENT_STATUSES = frozenset({"unavailable", "confirmed", "mismatch"})
EXTERNAL_SIGNER_MESSAGE_INTEGRITY_STATUSES = frozenset({"unavailable", "confirmed", "mismatch"})
EXTERNAL_SIGNER_SIGNATURE_VERIFICATION_STATUSES = frozenset({"unavailable", "valid", "invalid"})
MAX_JSONL_BYTES = 64 * 1024
MAX_MESSAGE_BYTES = 32 * 1024
MAX_SIGNATURE_BYTES = 512
MAX_STDERR_BYTES = 64 * 1024


@dataclass(frozen=True)
class ExternalSignerProcessDiagnostics:
    started: bool = False
    protocol_status: str = "unavailable"
    describe_performed: bool = False
    sign_performed: bool = False
    exit_status: str = "unavailable"
    request_count: int = 0
    address_match: str = "unresolved"
    environment_isolated: str = "unavailable"
    message_integrity: str = "unavailable"
    signature_verification: str = "unavailable"

    def __post_init__(self) -> None:
        # Normalize the pre-diagnostic boolean used by the original model.
        if isinstance(self.environment_isolated, bool):
            object.__setattr__(self, "environment_isolated", "confirmed" if self.environment_isolated else "unavailable")
        if self.exit_status not in EXTERNAL_SIGNER_EXIT_STATUSES:
            raise ValueError("invalid external signer exit status")
        if self.address_match not in EXTERNAL_SIGNER_ADDRESS_MATCH_STATUSES:
            raise ValueError("invalid external signer address status")
        if self.environment_isolated not in EXTERNAL_SIGNER_ENVIRONMENT_STATUSES:
            raise ValueError("invalid external signer environment status")
        if self.message_integrity not in EXTERNAL_SIGNER_MESSAGE_INTEGRITY_STATUSES:
            raise ValueError("invalid external signer message status")
        if self.signature_verification not in EXTERNAL_SIGNER_SIGNATURE_VERIFICATION_STATUSES:
            raise ValueError("invalid external signer signature status")


class SignerProcessLauncher(Protocol):
    def start(self) -> None: ...
    def request(self, payload: Mapping[str, Any]) -> Mapping[str, Any]: ...
    def close(self) -> None: ...


class ExternalSignerProcessLauncher:
    """Safe injectable launcher using argv and ``shell=False`` only."""

    def __init__(
        self,
        argv: Sequence[str],
        *,
        environment: Mapping[str, str] | None = None,
        request_timeout_seconds: float = 5.0,
        startup_timeout_seconds: float = 5.0,
        shutdown_timeout_seconds: float = 1.0,
        max_jsonl_bytes: int = MAX_JSONL_BYTES,
        max_stderr_bytes: int = MAX_STDERR_BYTES,
    ) -> None:
        if not argv or any(not isinstance(part, str) or not part for part in argv):
            raise ValueError("argv must contain non-empty argument elements")
        if request_timeout_seconds <= 0 or startup_timeout_seconds <= 0 or shutdown_timeout_seconds <= 0:
            raise ValueError("timeouts must be positive")
        self.argv = tuple(argv)
        self.request_timeout_seconds = float(request_timeout_seconds)
        self.startup_timeout_seconds = float(startup_timeout_seconds)
        self.shutdown_timeout_seconds = float(shutdown_timeout_seconds)
        self.max_jsonl_bytes = int(max_jsonl_bytes)
        self.max_stderr_bytes = int(max_stderr_bytes)
        self.environment = self._minimal_environment(environment)
        self.process: subprocess.Popen[bytes] | None = None
        self._stdout_queue: queue.Queue[bytes | BaseException | None] = queue.Queue()
        self._stderr_size = 0
        self.last_exit_status = "unavailable"

    @staticmethod
    def _minimal_environment(values: Mapping[str, str] | None) -> dict[str, str]:
        values = values or {}
        allowed = {"SYSTEMROOT", "WINDIR", "PATH"}
        result = {key: str(value) for key, value in os.environ.items() if key.upper() in allowed}
        for key in allowed:
            if key in values:
                result[key] = str(values[key])
        result.update({"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"})
        return result

    def _read_stdout(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        try:
            for line in iter(self.process.stdout.readline, b""):
                if len(line) > self.max_jsonl_bytes:
                    self._stdout_queue.put(ValueError("signer_output_oversized"))
                    break
                self._stdout_queue.put(line)
        except BaseException as exc:
            self._stdout_queue.put(exc)
        finally:
            self._stdout_queue.put(None)

    def _read_stderr(self) -> None:
        if self.process is None or self.process.stderr is None:
            return
        try:
            while True:
                chunk = self.process.stderr.read(min(4096, self.max_stderr_bytes + 1))
                if not chunk:
                    break
                self._stderr_size += len(chunk)
                if self._stderr_size > self.max_stderr_bytes:
                    break
        except Exception:
            pass

    def start(self) -> None:
        if self.process is not None and self.process.poll() is None:
            return
        self.last_exit_status = "unavailable"
        try:
            self.process = subprocess.Popen(
                list(self.argv), shell=False, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, env=self.environment, close_fds=True,
            )
        except Exception as exc:
            raise RuntimeError("signer_process_start_failed") from None
        self._stderr_size = 0
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()
        deadline = time.monotonic() + min(self.startup_timeout_seconds, 0.1)
        while self.process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.005)
            # A running process is sufficient; protocol readiness is checked
            # by the first request.
        if self.process.poll() is not None and self.process.returncode != 0:
            self.last_exit_status = "nonzero_exit"
            raise RuntimeError("signer_process_start_failed")

    def request(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        self.start()
        assert self.process is not None and self.process.stdin is not None
        encoded = (json.dumps(dict(payload), ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        if len(encoded) > self.max_jsonl_bytes:
            raise RuntimeError("signer_request_oversized")
        try:
            self.process.stdin.write(encoded)
            self.process.stdin.flush()
            item = self._stdout_queue.get(timeout=self.request_timeout_seconds)
        except queue.Empty:
            self.last_exit_status = "killed_timeout"
            raise RuntimeError("signer_request_timeout") from None
        except Exception:
            raise RuntimeError("signer_request_failed") from None
        if isinstance(item, BaseException):
            raise RuntimeError("signer_output_failed") from None
        if self._stderr_size > self.max_stderr_bytes:
            raise RuntimeError("signer_stderr_oversized")
        if item is None or item == b"":
            if self.process is not None:
                try:
                    self.process.wait(timeout=0.1)
                except Exception:
                    pass
                if self.process.poll() not in (None, 0):
                    self.last_exit_status = "nonzero_exit"
            raise RuntimeError("signer_empty_stdout")
        try:
            text = item.decode("utf-8")
        except UnicodeDecodeError:
            raise RuntimeError("signer_non_utf8_output") from None
        if not text.endswith("\n") or text.count("\n") != 1:
            raise RuntimeError("signer_extra_stdout")
        body = text[:-1]
        if body.endswith("\r"):
            body = body[:-1]
        if "\r" in body:
            raise RuntimeError("signer_extra_stdout")
        if len(body.encode("utf-8")) > self.max_jsonl_bytes:
            raise RuntimeError("signer_output_oversized")
        try:
            value = json.loads(body)
        except Exception:
            raise RuntimeError("signer_malformed_json") from None
        if not isinstance(value, dict):
            raise RuntimeError("signer_response_not_object")
        try:
            extra = self._stdout_queue.get_nowait()
        except queue.Empty:
            extra = "none"
        if extra not in {"none", None}:
            raise RuntimeError("signer_extra_stdout")
        return value

    def close(self) -> None:
        process = self.process
        self.process = None
        if process is None:
            return
        if process.poll() not in (None, 0) and self.last_exit_status == "unavailable":
            self.last_exit_status = "nonzero_exit"
        if process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=self.shutdown_timeout_seconds)
            except Exception:
                try:
                    process.kill()
                    process.wait(timeout=self.shutdown_timeout_seconds)
                except Exception:
                    pass
        if self.last_exit_status not in {"killed_timeout", "nonzero_exit"}:
            self.last_exit_status = "clean"


class UnavailableExternalSiweSigner:
    configured = False
    status = "unavailable"
    executable = "<unresolved>"
    protocol_status = "unavailable"
    capabilities = frozenset()
    environment_isolated = "unavailable"
    exit_status = "unavailable"
    address_match = "unresolved"
    message_integrity = "unavailable"
    signature_verification = "unavailable"

    def get_address(self) -> str:
        raise RuntimeError("external_signer_unavailable")

    def sign_message(self, message: str) -> str:
        raise RuntimeError("external_signer_unavailable")

    def __repr__(self) -> str:
        return "UnavailableExternalSiweSigner(configured=False, status='unavailable', executable='<unresolved>')"


def build_production_external_siwe_signer_from_env(environ: Mapping[str, str] | None = None) -> UnavailableExternalSiweSigner:
    del environ
    return UnavailableExternalSiweSigner()


build_external_siwe_signer_from_env = build_production_external_siwe_signer_from_env
ExternalSiweSignerUnavailable = UnavailableExternalSiweSigner


class DreamDexExternalSiweSignerClient:
    configured = True
    status = "unavailable"
    capabilities = frozenset()

    def __init__(self, launcher: SignerProcessLauncher, *, owner_address: str) -> None:
        self.launcher = launcher
        self.owner_address = _normalize_address(owner_address)
        if self.owner_address is None:
            raise ValueError("invalid owner_address")
        self._address: str | None = None
        self._capabilities = SIGNER_CAPABILITIES
        self._described = False
        self._closed = False
        self.diagnostics = ExternalSignerProcessDiagnostics()
        self.status = "unavailable"

    @property
    def capabilities(self):
        return self._capabilities

    @property
    def process_started(self) -> bool:
        return self.diagnostics.started

    @property
    def protocol_status(self) -> str:
        return self.diagnostics.protocol_status

    @property
    def describe_performed(self) -> bool:
        return self.diagnostics.describe_performed

    @property
    def sign_performed(self) -> bool:
        return self.diagnostics.sign_performed

    @property
    def exit_status(self) -> str:
        return self.diagnostics.exit_status

    @property
    def environment_isolated(self) -> str:
        return self.diagnostics.environment_isolated

    @property
    def address_match(self) -> str:
        return self.diagnostics.address_match

    @property
    def message_integrity(self) -> str:
        return self.diagnostics.message_integrity

    @property
    def signature_verification(self) -> str:
        return self.diagnostics.signature_verification

    def _with_diagnostics(self, **changes: Any) -> None:
        self.diagnostics = replace(self.diagnostics, **changes)

    def _safe_error(self, response: Mapping[str, Any] | None = None) -> RuntimeError:
        del response
        return RuntimeError("external_signer_protocol_failed")

    def _validate_response(self, request: Mapping[str, Any], response: Mapping[str, Any], *, operation: str) -> None:
        expected_fields = {"protocol", "requestId", "status", "signerAddress", "capabilities"} if operation == "describe" else {"protocol", "requestId", "status", "signerAddress", "signature"}
        if response.get("status") != "ok":
            if set(response) - {"protocol", "requestId", "status", "errorCode"}:
                raise self._safe_error()
        elif set(response) != expected_fields:
            raise self._safe_error()
        if response.get("protocol") != PROTOCOL or response.get("requestId") != request.get("requestId"):
            raise self._safe_error()
        if response.get("status") not in RESPONSE_STATUSES:
            raise self._safe_error()
        if response.get("status") != "ok":
            raise RuntimeError(f"external_signer_{response.get('status')}")
        address = _normalize_address(response.get("signerAddress"))
        if address is None:
            raise RuntimeError("external_signer_address_invalid")
        if operation == "describe":
            caps = response.get("capabilities")
            if not isinstance(caps, list) or set(caps) != set(SIGNER_CAPABILITIES) or len(caps) != 1:
                raise RuntimeError("external_signer_capability_invalid")
            self._capabilities = frozenset(caps)
        if self._address is not None and self._address != address:
            raise RuntimeError("external_signer_address_changed")
        if address != self.owner_address:
            raise RuntimeError("external_signer_address_mismatch")
        self._address = address

    def _describe(self) -> None:
        request = {"protocol": PROTOCOL, "requestId": uuid.uuid4().hex, "operation": "describe"}
        try:
            response = self.launcher.request(request)
            self._validate_response(request, response, operation="describe")
        except Exception as exc:
            if "address_mismatch" in str(exc):
                self._with_diagnostics(address_match="mismatch")
            elif "timeout" in str(exc):
                self._with_diagnostics(exit_status="killed_timeout")
            else:
                launcher_status = getattr(self.launcher, "last_exit_status", None)
                if launcher_status in EXTERNAL_SIGNER_EXIT_STATUSES and launcher_status != "unavailable":
                    self._with_diagnostics(exit_status=launcher_status)
            self.close()
            raise RuntimeError("external_signer_describe_failed") from None
        self._described = True
        self.status = "configured"
        self.diagnostics = ExternalSignerProcessDiagnostics(
            started=True, protocol_status="confirmed", describe_performed=True,
            sign_performed=False, exit_status="running", address_match="confirmed",
            environment_isolated="confirmed", request_count=1,
        )

    def get_address(self) -> str:
        if not self._described:
            self._describe()
        assert self._address is not None
        return self._address

    def sign_message(self, message: str) -> str:
        if not isinstance(message, str) or not message or len(message.encode("utf-8")) > MAX_MESSAGE_BYTES:
            raise ValueError("external_signer_message_invalid")
        if not self._described:
            self._describe()
        request = {"protocol": PROTOCOL, "requestId": uuid.uuid4().hex, "operation": "sign_siwe_message", "expectedAddress": self.owner_address, "message": message}
        try:
            response = self.launcher.request(request)
            self._validate_response(request, response, operation="sign_siwe_message")
            signature = response.get("signature")
            if not isinstance(signature, str) or len(signature.encode("utf-8")) > MAX_SIGNATURE_BYTES:
                raise RuntimeError("external_signer_signature_invalid")
            self.diagnostics = replace(
                self.diagnostics, sign_performed=True, request_count=2,
                message_integrity="confirmed",
            )
            return signature
        finally:
            self.close()

    def close(self) -> None:
        try:
            self.launcher.close()
        finally:
            self._closed = True
            self._described = False
            if self.diagnostics.started:
                status = getattr(self.launcher, "last_exit_status", None) or "clean"
                if status not in EXTERNAL_SIGNER_EXIT_STATUSES:
                    status = "clean"
                self.diagnostics = replace(self.diagnostics, exit_status=status)

    def record_signature_verification(self, *, valid: bool, message_integrity: str | None = None) -> None:
        """Record only verification status; never retain the message or signature."""
        self._with_diagnostics(
            signature_verification="valid" if valid else "invalid",
            message_integrity=message_integrity or ("confirmed" if valid else "mismatch"),
        )

    def __repr__(self) -> str:
        return f"DreamDexExternalSiweSignerClient(configured=True, status={self.status!r}, address={'***' if self._address else '<unresolved>'})"


ExternalSiweSignerClient = DreamDexExternalSiweSignerClient


__all__ = [
    "PROTOCOL", "OPERATIONS", "RESPONSE_STATUSES", "EXTERNAL_SIGNER_EXIT_STATUSES",
    "EXTERNAL_SIGNER_ADDRESS_MATCH_STATUSES", "EXTERNAL_SIGNER_ENVIRONMENT_STATUSES",
    "EXTERNAL_SIGNER_MESSAGE_INTEGRITY_STATUSES", "EXTERNAL_SIGNER_SIGNATURE_VERIFICATION_STATUSES",
    "ExternalSignerProcessDiagnostics",
    "SignerProcessLauncher", "ExternalSignerProcessLauncher", "UnavailableExternalSiweSigner",
    "DreamDexExternalSiweSignerClient", "ExternalSiweSignerClient", "build_production_external_siwe_signer_from_env",
    "build_external_siwe_signer_from_env", "ExternalSiweSignerUnavailable",
]
