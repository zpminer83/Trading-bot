from pathlib import Path

from scripts import check_live_read_only_state
from bot.integrations.dreamdex_authenticated_read_only import ENABLE_ENV, TOKEN_ENV


FIXTURE = Path(__file__).parent / "fixtures" / "read_only" / "normal_account.json"


def test_cli_missing_configuration_is_nonzero_without_traceback(monkeypatch, capsys):
    for name in ("DREAMDEX_READ_ONLY_OWNER_ADDRESS", "DREAMDEX_READ_ONLY_BASE_URL", "DREAMDEX_READ_ONLY_RPC_URL", "DREAMDEX_READ_ONLY_FIXTURE"):
        monkeypatch.delenv(name, raising=False)
    code = check_live_read_only_state.main()
    output = capsys.readouterr().out
    assert code != 0
    assert "DREAMDEX_READ_ONLY_OWNER_ADDRESS" in output
    assert "Traceback" not in output
    assert "Real submission enabled: NO" in output


def test_cli_fixture_is_read_only_and_masks_account_id(monkeypatch, capsys):
    monkeypatch.setenv("DREAMDEX_READ_ONLY_OWNER_ADDRESS", "0x1234567890abcdef1234567890abcdef12345678")
    monkeypatch.setenv("DREAMDEX_READ_ONLY_FIXTURE", str(FIXTURE))
    monkeypatch.setenv("DREAMDEX_READ_ONLY_DRY_RUN_PRICE", "10.0000")
    monkeypatch.setenv("DREAMDEX_READ_ONLY_DRY_RUN_QUANTITY", "1")
    code = check_live_read_only_state.main()
    output = capsys.readouterr().out
    assert code == 0
    assert "READ-ONLY ACCOUNT CHECK" in output
    assert "MARKET TRADING RULES:" in output
    assert "tick size: 0.0001" in output
    assert "quantity step: 0.01" in output
    assert "minimum notional: unavailable" in output
    assert "public schema fingerprint: observed" in output
    assert "Real submission enabled: NO" in output
    assert "private-account-id" not in output
    assert "create_order" not in output
    assert "Authenticated account source: unconfigured" in output
    assert "Authenticated balances: unavailable" in output
    assert "Authenticated open orders: unavailable" in output
    assert "Authenticated fills: unavailable" in output
    assert "Authenticated pagination complete: NO" in output
    assert "AUTHENTICATION STATE:" in output
    assert "manager configured: NO" in output
    assert "signer configured: NO" in output
    assert "signature verification performed: NO" in output
    assert "signature verification status: unavailable" in output
    assert "recovered signer address: <missing>" in output
    assert "signer/owner cryptographic match: unresolved" in output
    assert "external signer configured: NO" in output
    assert "external signer process started: NO" in output
    assert "external signer protocol status: unavailable" in output
    assert "external signer describe performed: NO" in output
    assert "external signer sign performed: NO" in output
    assert "external signer exit status: unavailable" in output
    assert "external signer address match: unresolved" in output
    assert "external signer environment isolated: unavailable" in output
    assert "external signer message integrity: unavailable" in output
    assert "external signer signature verification: unavailable" in output
    assert "SIWE HTTP transport configured: NO" in output
    assert "SIWE HTTP transport status: disabled" in output
    assert "auth network attempt performed: NO" in output
    assert "nonce request performed: NO" in output
    assert "login request performed: NO" in output
    assert "auth state: unconfigured" in output
    assert "token present: NO" in output
    assert "identity authoritative: NO" in output
    assert "On-chain fills source: unconfigured" in output
    assert "On-chain fills authoritative: NO" in output
    assert "Order metadata source: unconfigured" in output
    assert "Order metadata records resolved: 0" in output
    assert "Order metadata conflicts: 0" in output
    assert "Fill/order correlation status: unavailable" in output
    assert "Account-correlated fills authoritative: NO" in output
    assert "authenticated_account_state_unavailable" in output
    assert "DIRECT OWNER EXECUTION MODEL:" in output
    assert "selected execution mode: direct_owner" in output
    assert "operator mode active: NO" in output
    assert "transaction signer capability: unavailable" in output
    assert "direct execution authoritative: NO" in output
    assert "direct_order_transport_unconfirmed" not in output.split("Hypothetical trading blocked reason:", 1)[1].split("\n", 1)[0]
    assert "python_direct_execution_partial" in output
    assert "unsigned transaction model: available_offline" in output
    assert "unsigned place builder: available_offline" in output
    assert "unsigned cancel builder: available_offline" in output
    assert "unsigned reduce builder: available_offline" in output
    assert "raw calldata output allowed: NO" in output
    assert "gas resolution: unavailable" in output
    assert "nonce resolution: unavailable" in output
    assert "fee resolution: unavailable" in output
    assert "signing capability: unavailable" in output
    assert "submission capability: unavailable" in output
    assert "unsigned request authoritative: NO" in output
    assert "unsigned request ready for signing: NO" in output
    assert "unsigned request ready for submission: NO" in output
    assert "direct_transaction_transport_unimplemented" in output
    assert "transaction envelope model: available_offline" in output
    assert "envelope builder: available_offline" in output
    assert "envelope validation: available_offline" in output
    assert "request fingerprint: unavailable" in output
    assert "envelope fingerprint: unavailable" in output
    assert "envelope ready for signing: NO" in output
    assert "envelope ready for submission: NO" in output
    assert "envelope raw calldata output allowed: NO" in output
    assert "transaction lifecycle model: available_offline" in output
    assert "prepared lifecycle builder: available_offline" in output
    assert "external submission import: available_offline" in output
    assert "receipt evidence validation: available_offline" in output
    assert "event evidence validation: available_offline" in output
    assert "receipt fetch capability: unavailable" in output
    assert "log fetch capability: unavailable" in output
    assert "transaction hash: <missing>" in output
    assert "lifecycle state: unavailable" in output
    assert "lifecycle authoritative: NO" in output
    assert "lifecycle reconciliation: incomplete" in output
    assert "raw receipt output allowed: NO" in output
    assert "raw event output allowed: NO" in output
    assert "operator_permission_unavailable" not in output.split("Hypothetical trading blocked reason:", 1)[1].split("\n", 1)[0]


def test_cli_prints_platform_roles_and_identity_binding_without_authorizing(monkeypatch, capsys):
    monkeypatch.setenv("DREAMDEX_READ_ONLY_OWNER_ADDRESS", "0x1234567890abcdef1234567890abcdef12345678")
    monkeypatch.setenv("DREAMDEX_READ_ONLY_TRADING_ADDRESS", "0xabcdefabcdefabcdefabcdefabcdefabcdefabcd")
    monkeypatch.setenv("DREAMDEX_READ_ONLY_TRADING_PLATFORM_ROLE", "dreamdex_smart_wallet")
    monkeypatch.setenv("DREAMDEX_READ_ONLY_OWNER_PLATFORM_ROLE", "owner_login_wallet")
    monkeypatch.setenv("DREAMDEX_READ_ONLY_FIXTURE", str(FIXTURE))
    code = check_live_read_only_state.main()
    output = capsys.readouterr().out
    assert code == 0
    assert "platform role: owner_login_wallet" in output
    assert "platform role: dreamdex_smart_wallet" in output
    assert "on-chain code type: eoa_no_code" in output
    assert "IDENTITY BINDING:" in output
    assert "binding status: observed" in output
    assert "authoritative: NO" in output
    assert "1234567890abcdef" not in output


def test_cli_authenticated_factory_wires_configured_transport_without_leaking_token(monkeypatch, capsys):
    token = "offline-test-bearer"
    monkeypatch.setenv("DREAMDEX_READ_ONLY_OWNER_ADDRESS", "0x1234567890abcdef1234567890abcdef12345678")
    monkeypatch.setenv("DREAMDEX_READ_ONLY_FIXTURE", str(FIXTURE))
    monkeypatch.setenv(ENABLE_ENV, "TrUe")
    monkeypatch.setenv(TOKEN_ENV, token)

    class Response:
        status_code = 401
        headers = {"content-type": "application/json"}
        content = b'{"error":"unauthorized"}'

        def json(self):
            return {"error": "unauthorized"}

    calls = []
    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        return Response()

    monkeypatch.setattr("httpx.get", fake_get)
    code = check_live_read_only_state.main()
    output = capsys.readouterr().out
    assert code == 0
    assert calls
    assert "Authenticated transport: configured" in output
    assert "Authenticated transport configured: YES" in output
    assert "Authenticated request execution: enabled" in output
    assert "Authenticated vault REST status: unauthorized" in output
    assert "Authenticated vault schema:" in output
    assert "Authenticated order list schema:" in output
    assert "top-level type: null" in output
    assert "unauthorized" not in output.split("Authenticated vault schema:", 1)[1].split("Authenticated order list schema:", 1)[0]
    assert "Reconciliation complete: NO" in output
    assert token not in output
    assert "Authorization" not in output
    assert "AUTHENTICATION STATE:" in output
    assert "token present: NO" in output
