from decimal import Decimal
from pathlib import Path

from bot.execution.dry_run_order_validator import DryRunOrderValidator, DryRunValidationLimits
from bot.execution.order import OrderIntent
from bot.integrations.dreamdex_read_only import DreamDexReadOnlyAdapter, FixtureTransport, load_fixture


FIXTURE = Path(__file__).parent / "fixtures" / "read_only" / "normal_account.json"


def state():
    adapter = DreamDexReadOnlyAdapter(transport=FixtureTransport(load_fixture(FIXTURE)), account_identifier="wallet", market_symbol="SOMI:USDso")
    snapshot = adapter.fetch_snapshot()
    report = adapter.reconcile(snapshot, local_cash=Decimal("1000"), local_inventory=Decimal("10"), local_open_order_ids=["order-1"], local_fill_ids=["fill-0"])
    return snapshot, report


def test_valid_hypothetical_intent_is_approved_without_side_effects():
    snapshot, report = state()
    intent = OrderIntent("SOMI:USDso", "buy", "limit", Decimal("10.0000"), Decimal("1"))
    allowed = type("Decision", (), {"allowed": True, "reason": "ok"})()
    result = DryRunOrderValidator().validate(intent, market=snapshot.market, account=snapshot.account, reconciliation=report, market_fresh=True, fair_play_decision=allowed, risk_decision=allowed)
    assert result.approved
    assert result.reasons == ()
    assert result.normalized_price == Decimal("10.0000")
    assert result.normalized_quantity == Decimal("1")
    assert result.notional == Decimal("10.0000")
    assert result.hypothetical_payload["price"] == "10.0000"


def test_invalid_tick_step_minimum_and_stale_market_are_rejected():
    adapter = DreamDexReadOnlyAdapter(transport=FixtureTransport(load_fixture(FIXTURE)), account_identifier="wallet", market_symbol="SOMI:USDso")
    snapshot = adapter.fetch_snapshot()
    report = adapter.reconcile(snapshot, local_cash=Decimal("1000"), local_inventory=Decimal("10"), local_open_order_ids=["order-1"], local_fill_ids=["fill-0"])
    intent = OrderIntent("SOMI:USDso", "buy", "limit", Decimal("10.00005"), Decimal("0.005"))
    allowed = type("Decision", (), {"allowed": True, "reason": "ok"})()
    result = DryRunOrderValidator().validate(intent, market=snapshot.market, account=snapshot.account, reconciliation=report, market_fresh=False, fair_play_decision=allowed, risk_decision=allowed)
    assert not result.approved
    assert {"invalid_price_tick", "invalid_quantity_step", "minimum_quantity", "market_data_stale"}.issubset(result.reasons)


def test_balance_inventory_reconciliation_and_configured_limits_block():
    snapshot, _ = state()
    adapter = DreamDexReadOnlyAdapter(transport=FixtureTransport(load_fixture(FIXTURE)), account_identifier="wallet", market_symbol="SOMI:USDso")
    report = adapter.reconcile(snapshot, local_cash=Decimal("1"), local_inventory=Decimal("0"))
    intent = OrderIntent("SOMI:USDso", "buy", "limit", Decimal("10"), Decimal("20"))
    allowed = type("Decision", (), {"allowed": True, "reason": "ok"})()
    result = DryRunOrderValidator(DryRunValidationLimits(maximum_notional=Decimal("50"), maximum_inventory=Decimal("2"))).validate(intent, market=snapshot.market, account=snapshot.account, reconciliation=report, market_fresh=True, fair_play_decision=allowed, risk_decision=allowed)
    assert not result.approved
    assert "reconciliation_blocked" in result.reasons
    assert "maximum_notional" in result.reasons
    assert "maximum_inventory" in result.reasons


def test_fair_play_and_risk_decisions_are_respected():
    snapshot, report = state()
    intent = OrderIntent("SOMI:USDso", "buy", "limit", Decimal("10"), Decimal("1"))
    fair = type("Decision", (), {"allowed": False, "reason": "fair_play_blocked"})()
    risk = type("Decision", (), {"allowed": False, "reason": "risk_latched"})()
    result = DryRunOrderValidator().validate(intent, market=snapshot.market, account=snapshot.account, reconciliation=report, market_fresh=True, fair_play_decision=fair, risk_decision=risk)
    assert not result.approved
    assert "fair_play_blocked" in result.reasons
    assert "risk_latched" in result.reasons
