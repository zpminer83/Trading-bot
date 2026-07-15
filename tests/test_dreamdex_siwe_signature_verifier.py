from eth_account import Account
from eth_account.messages import encode_defunct

from bot.integrations.dreamdex_siwe_signature_verifier import verify_siwe_signature


PRIVATE_KEY = "0x" + "01" * 32  # TEST ONLY; never read from environment.
ACCOUNT = Account.from_key(PRIVATE_KEY)
MESSAGE = "api.dreamdex.io wants you to sign in with your Ethereum account:\nhello"


def signature(message=MESSAGE):
    return Account.sign_message(encode_defunct(text=message), private_key=PRIVATE_KEY).signature.hex()


def test_valid_eip191_signature_recovers_expected_owner_without_proving_wallet_binding():
    result = verify_siwe_signature(MESSAGE, signature(), ACCOUNT.address)
    assert result.status == "valid"
    assert result.address_match == "confirmed"
    assert result.recovery_performed
    assert result.authoritative_for_signer_address
    assert not result.authoritative_for_dreamdex_wallet_binding
    assert ACCOUNT.address not in repr(result)
    assert PRIVATE_KEY not in repr(result)


def test_wrong_owner_and_wrong_message_fail_closed():
    wrong_owner = "0xabcdefabcdefabcdefabcdefabcdefabcdefabcd"
    assert verify_siwe_signature(MESSAGE, signature(), wrong_owner).status == "address_mismatch"
    assert verify_siwe_signature(MESSAGE + "!", signature(), ACCOUNT.address).status == "address_mismatch"


def test_signature_format_and_components_are_strict():
    valid = signature()
    cases = ["", "0x1", valid[:-2], valid[:-2] + "zz", "0x" + "00" * 32 + valid[66:]]
    for value in cases:
        assert verify_siwe_signature(MESSAGE, value, ACCOUNT.address).status == "invalid_format"
    high_s = bytearray(bytes.fromhex(valid.removeprefix("0x")))
    high_s[32:64] = (2**256 - 1).to_bytes(32, "big")
    assert verify_siwe_signature(MESSAGE, "0x" + bytes(high_s).hex(), ACCOUNT.address).status == "invalid_format"
    invalid_v = bytearray(bytes.fromhex(valid.removeprefix("0x")))
    invalid_v[64] = 29
    assert verify_siwe_signature(MESSAGE, "0x" + bytes(invalid_v).hex(), ACCOUNT.address).status == "invalid_format"


def test_exact_message_bytes_are_not_normalized_and_fingerprint_is_safe():
    result = verify_siwe_signature(MESSAGE + "\n", signature(), ACCOUNT.address)
    assert result.status == "address_mismatch"
    assert result.message_fingerprint
    assert MESSAGE not in repr(result)
    assert "\r" not in result.message_fingerprint
