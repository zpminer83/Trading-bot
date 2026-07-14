from pathlib import Path

from scripts import check_live_read_only_state


FIXTURE = Path(__file__).parent / "fixtures" / "read_only" / "normal_account.json"


def test_cli_missing_configuration_is_nonzero_without_traceback(monkeypatch, capsys):
    for name in ("DREAMDEX_READ_ONLY_ACCOUNT_ID", "DREAMDEX_READ_ONLY_BASE_URL", "DREAMDEX_READ_ONLY_FIXTURE"):
        monkeypatch.delenv(name, raising=False)
    code = check_live_read_only_state.main()
    output = capsys.readouterr().out
    assert code != 0
    assert "DREAMDEX_READ_ONLY_ACCOUNT_ID" in output
    assert "Traceback" not in output
    assert "Real submission enabled: NO" in output


def test_cli_fixture_is_read_only_and_masks_account_id(monkeypatch, capsys):
    monkeypatch.setenv("DREAMDEX_READ_ONLY_ACCOUNT_ID", "private-account-id")
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
