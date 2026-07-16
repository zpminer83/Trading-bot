"""Explicit encrypted-keystore metadata and identity check.

This script is never called by the normal read-only diagnostics CLI.  It does
not accept passwords, keys, seeds, RPC settings, or transaction material.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from bot.execution.dreamdex_encrypted_keystore_signer import (
    EncryptedKeystoreDreamDexTransactionSigner,
    DreamDexEncryptedKeystorePolicy,
)
from bot.execution.dreamdex_secret_provider import InteractiveDreamDexKeystoreSecretProvider


def _print_metadata(signer: EncryptedKeystoreDreamDexTransactionSigner) -> None:
    metadata = signer.inspect_metadata()
    print("DREAMDEX ENCRYPTED KEYSTORE IDENTITY CHECK")
    print("Mode: metadata-only" if signer.secret_provider.__class__.__name__ != "InteractiveDreamDexKeystoreSecretProvider" else "Mode: unlock-check")
    print(f"Keystore configured: {'YES' if metadata.file_path_status == 'accepted' else 'NO'}")
    print(f"Keystore path output allowed: NO")
    print(f"Keystore metadata inspected: {'YES' if metadata.file_path_status == 'accepted' else 'NO'}")
    print(f"Keystore status: {metadata.keystore_status}")
    print(f"Keystore format: {'v3' if metadata.keystore_version == 3 else 'unavailable'}")
    print(f"Public address: {metadata.safe_dict()['public_address_masked']}")
    print(f"Public address match: {'confirmed' if metadata.public_address_match is True else 'mismatch' if metadata.public_address_match is False else 'unresolved'}")
    print(f"Crypto section: {'present' if metadata.crypto_section_present else 'unavailable'}")
    print(f"Cipher: {metadata.cipher_name or 'unavailable'}")
    print(f"KDF: {metadata.kdf_name or 'unavailable'}")
    print(f"File size: {metadata.file_size_bytes if metadata.file_size_bytes is not None else 'unavailable'}")
    print(f"Path safety: {metadata.file_path_status}")
    print(f"Symlink status: {metadata.symlink_status}")
    print(f"Metadata fingerprint: {metadata.safe_dict()['metadata_fingerprint']}")
    print(f"Blockers: {', '.join(metadata.blockers) or 'none'}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Explicit offline DreamDEX keystore identity check")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--metadata-only", action="store_true")
    mode.add_argument("--unlock-check", action="store_true")
    parser.add_argument("--keystore", required=True, help="absolute encrypted keystore path")
    parser.add_argument("--expected-address", required=True, help="expected public signer address")
    args = parser.parse_args(argv)
    policy = DreamDexEncryptedKeystorePolicy(expected_signer_address=args.expected_address)
    provider = InteractiveDreamDexKeystoreSecretProvider() if args.unlock_check else None
    signer = EncryptedKeystoreDreamDexTransactionSigner(keystore_path=Path(args.keystore), expected_signer_address=args.expected_address, policy=policy, secret_provider=provider)
    _print_metadata(signer)
    if args.unlock_check:
        result = signer.unlock_check()
        safe = result.safe_dict()
        print("Unlock status:", safe["unlock_status"])
        print("Secret provider:", safe["secret_provider_type"])
        print("Unlock execution performed:", "YES" if safe["secret_provider_invoked"] else "NO")
        print("Unlock attempt count:", safe["unlock_attempt_count"])
        print("Derived address:", safe["derived_address_masked"])
        print("Derived address match:", "confirmed" if safe["derived_address_match"] is True else "mismatch" if safe["derived_address_match"] is False else "unresolved")
        print("Key material persisted: NO")
        print("Password persisted: NO")
        print("Secure memory zeroization guaranteed: NO")
        print("Blockers:", ", ".join(result.blockers) or "none")
        return 0 if result.unlock_status == "verified" else 1
    return 0 if signer.inspect_metadata().keystore_status == "valid" else 1


if __name__ == "__main__":
    raise SystemExit(main())
