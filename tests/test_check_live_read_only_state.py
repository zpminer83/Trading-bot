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
    assert "Real submission enabled: NO" in output
    assert "private-account-id" not in output
    assert "create_order" not in output
    assert "Authenticated account source: unconfigured" in output
    assert "Authenticated balances: unavailable" in output
    assert "Authenticated open orders: unavailable" in output
    assert "Authenticated fills: unavailable" in output
    assert "Authenticated pagination complete: NO" in output
    assert "On-chain fills source: unconfigured" in output
    assert "On-chain fills authoritative: NO" in output
    assert "Order metadata source: unconfigured" in output
    assert "Order metadata records resolved: 0" in output
    assert "Order metadata conflicts: 0" in output
    assert "Fill/order correlation status: unavailable" in output
    assert "Account-correlated fills authoritative: NO" in output
    assert "authenticated_account_state_unavailable" in output


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
    assert "Reconciliation complete: NO" in output
    assert token not in output
    assert "Authorization" not in output
