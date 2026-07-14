from dataclasses import replace
from decimal import Decimal
from pathlib import Path

from bot.execution.dry_run_order_validator import DryRunOrderValidator, DryRunValidationLimits
from bot.execution.order import OrderIntent
from bot.integrations.dreamdex_read_only import DreamDexReadOnlyAdapter, FixtureRpcTransport, FixtureTransport, load_fixture


FIXTURE = Path(__file__).parent / "fixtures" / "read_only" / "normal_account.json"


def state():
    fixture = load_fixture(FIXTURE)
    adapter = DreamDexReadOnlyAdapter(transport=FixtureTransport(fixture), rpc_transport=FixtureRpcTransport(fixture), owner="0x1234567890abcdef1234567890abcdef12345678", symbol="SOMI:USDso")
    snapshot = adapter.fetch_snapshot()
    snapshot = replace(snapshot, account=replace(snapshot.account, open_orders_status="confirmed", fills_status="confirmed"))
    report = adapter.reconcile(snapshot, local_cash=Decimal("1000"), local_inventory=Decimal("10"))
    return snapshot, report


def allowed():
    return type("Decision", (), {"allowed": True, "reason": "ok"})()


def test_valid_hypothetical_intent_is_approved():
    snapshot, report = state()
    snapshot = replace(snapshot, market=replace(snapshot.market, status="active"))
    result = DryRunOrderValidator().validate(OrderIntent("SOMI:USDso", "buy", "limit", Decimal("10.0000"), Decimal("1")), market=snapshot.market, account=snapshot.account, reconciliation=report, market_fresh=True, fair_play_decision=allowed(), risk_decision=allowed())
    assert result.approved and result.reasons == ()


def test_incomplete_account_state_rejects_even_when_other_checks_pass():
    snapshot, _ = state()
    incomplete = replace(snapshot.account, open_orders_status="source_unavailable")
    report = replace(_, completed=False, trading_blocked=True, reason="incomplete_open_orders_source")
    result = DryRunOrderValidator().validate(OrderIntent("SOMI:USDso", "buy", "limit", Decimal("10"), Decimal("1")), market=snapshot.market, account=incomplete, reconciliation=report, market_fresh=True, fair_play_decision=allowed(), risk_decision=allowed())
    assert not result.approved
    assert "incomplete_account_state" in result.reasons


def test_invalid_tick_step_minimum_and_stale_market_are_rejected():
    snapshot, report = state()
    result = DryRunOrderValidator().validate(OrderIntent("SOMI:USDso", "buy", "limit", Decimal("10.00005"), Decimal("0.005")), market=snapshot.market, account=snapshot.account, reconciliation=report, market_fresh=False, fair_play_decision=allowed(), risk_decision=allowed())
    assert not result.approved
    assert {"invalid_price_tick", "invalid_quantity_step", "minimum_quantity", "market_data_stale"}.issubset(result.reasons)


def test_limits_and_fair_play_risk_are_enforced():
    snapshot, report = state()
    blocked = type("Decision", (), {"allowed": False, "reason": "blocked"})()
    result = DryRunOrderValidator(DryRunValidationLimits(Decimal("50"), Decimal("2"))).validate(OrderIntent("SOMI:USDso", "buy", "limit", Decimal("10"), Decimal("20")), market=snapshot.market, account=snapshot.account, reconciliation=report, market_fresh=True, fair_play_decision=blocked, risk_decision=blocked)
    assert not result.approved
    assert {"maximum_notional", "maximum_inventory", "blocked"}.issubset(result.reasons)
