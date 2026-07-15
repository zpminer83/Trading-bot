"""TEST ONLY deterministic external signer fixture.

It intentionally supports a finite set of named fault modes and no arbitrary
commands or environment-controlled keys.
"""
from __future__ import annotations

import json
import os
import sys
import time

from eth_account import Account
from eth_account.messages import encode_defunct


PROTOCOL = "dreamdex-siwe-signer/1"
PRIVATE_KEY = "0x" + "01" * 32  # TEST ONLY; never import this into production.
ADDRESS = Account.from_key(PRIVATE_KEY).address
CAPABILITIES = ["siwe_login_message"]
MODE = sys.argv[1] if len(sys.argv) == 2 else "valid"


def emit(value):
    sys.stdout.write(json.dumps(value, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def response(request, *, status="ok", **fields):
    result = {"protocol": PROTOCOL, "requestId": request.get("requestId"), "status": status}
    result.update(fields)
    return result


for raw in sys.stdin:
    try:
        request = json.loads(raw)
    except Exception:
        emit({"status": "invalid_request"})
        continue
    if MODE == "timeout":
        time.sleep(30)
    if MODE == "nonzero_exit":
        raise SystemExit(7)
    if MODE == "empty_stdout":
        raise SystemExit(0)
    if MODE == "malformed_json":
        sys.stdout.write("not-json\n")
        sys.stdout.flush()
        continue
    if MODE == "oversized_stdout":
        sys.stdout.write("x" * 100000 + "\n")
        sys.stdout.flush()
        continue
    if MODE == "oversized_stderr":
        sys.stderr.write("e" * 100000)
        sys.stderr.flush()
        emit(response(request, signerAddress=ADDRESS, capabilities=CAPABILITIES) if request.get("operation") == "describe" else response(request, signerAddress=ADDRESS, signature="0x11"))
        continue
    if MODE == "extra_stdout":
        emit({"noise": True})
        emit(response(request, signerAddress=ADDRESS, capabilities=CAPABILITIES) if request.get("operation") == "describe" else response(request, signerAddress=ADDRESS, signature="0x11"))
        continue
    if request.get("protocol") != PROTOCOL or MODE == "wrong_protocol":
        emit({**response(request), "protocol": "wrong/0"})
        continue
    if request.get("operation") not in {"describe", "sign_siwe_message"}:
        emit(response(request, status="invalid_request"))
        continue
    if request.get("operation") == "describe":
        if MODE == "wrong_capability":
            emit(response(request, signerAddress=ADDRESS, capabilities=["arbitrary_sign"]))
        elif MODE == "address_mismatch":
            emit(response(request, signerAddress="0xabcdefabcdefabcdefabcdefabcdefabcdefabcd", capabilities=CAPABILITIES))
        elif MODE == "secret_environment_probe":
            emit(response(request, status="rejected" if os.environ.get("DREAMDEX_READ_ONLY_BEARER_TOKEN") else "ok", signerAddress=ADDRESS, capabilities=CAPABILITIES))
        else:
            emit(response(request, signerAddress=ADDRESS, capabilities=CAPABILITIES))
        continue
    message = request.get("message", "")
    if MODE == "message_mutation":
        message = message + "x"
    if MODE == "rejected":
        emit(response(request, status="rejected", signerAddress=ADDRESS))
    elif MODE == "invalid_signature":
        emit(response(request, signerAddress=ADDRESS, signature="0x" + "11" * 65))
    elif MODE == "malformed_signature":
        emit(response(request, signerAddress=ADDRESS, signature="0xnot-hex"))
    elif MODE == "wrong_request_id":
        emit({**response(request, signerAddress=ADDRESS, signature="0x" + Account.sign_message(encode_defunct(text=message), private_key=PRIVATE_KEY).signature.hex()), "requestId": "wrong"})
    elif MODE == "malformed_json":
        sys.stdout.write("{\n")
        sys.stdout.flush()
    else:
        emit(response(request, signerAddress=ADDRESS, signature="0x" + Account.sign_message(encode_defunct(text=message), private_key=PRIVATE_KEY).signature.hex()))
