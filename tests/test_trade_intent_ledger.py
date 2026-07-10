from datetime import datetime, timezone
from decimal import Decimal

import pytest

from bot.competition.fair_play_guard import FairPlayDecision
from bot.competition.trade_intent_ledger import TradeIntentLedger
from bot.execution.conservative_paper_broker import PaperOrder
from bot.execution.order import OrderDecision, OrderIntent, OrderPurpose
from bot.portfolio.portfolio_manager import PortfolioManager


NOW = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)


def intent(**overrides) -> OrderIntent:
    values = {
        "symbol": "SOMI:USDso",
        "side": "buy",
        "order_type": "limit",
        "price": Decimal("1"),
        "quantity": Decimal("5"),
    }
    values.update(overrides)
    return OrderIntent(**values)


def test_order_intent_defaults_and_metadata_validation():
    legacy = intent()
    assert legacy.purpose is OrderPurpose.UNKNOWN
    assert legacy.strategy_name == "unknown"

    metadata = intent(
        purpose=OrderPurpose.ENTRY,
        strategy_name="test_strategy",
        rationale="deterministic rationale",
        signal_id="signal:42",
    )
    assert metadata.purpose is OrderPurpose.ENTRY
    assert metadata.signal_id == "signal:42"

    with pytest.raises(ValueError, match="strategy_name"):
        intent(strategy_name="")
    with pytest.raises(ValueError, match="rationale"):
        intent(rationale="x" * 501)
    with pytest.raises(ValueError, match="signal_id"):
        intent(signal_id="Authorization: Bearer secret")


def test_trade_intent_ledger_records_blocked_rejected_and_submitted_without_mutation():
    ledger = TradeIntentLedger()
    portfolio = PortfolioManager(initial_cash=Decimal("150"))
    blocked_intent = intent(side="sell", purpose=OrderPurpose.INVENTORY_REBALANCE)
    blocked = ledger.record_intent(
        blocked_intent,
        timestamp=NOW,
        fair_play_decision=FairPlayDecision(
            allowed=False,
            reason="opposite_side_cooldown",
            latched=False,
            seconds_since_opposite_fill=Decimal("1"),
            short_window_round_trip_count=0,
            near_flat_cycle_count=0,
        ),
    )
    rejected_intent = intent(purpose=OrderPurpose.ENTRY)
    rejected = ledger.record_intent(
        rejected_intent,
        timestamp=NOW,
        execution_decision=OrderDecision(
            approved=False,
            reason="notional_too_large",
            intent=rejected_intent,
        ),
    )
    submitted_intent = intent(purpose=OrderPurpose.ENTRY)
    submitted = ledger.record_intent(
        submitted_intent,
        timestamp=NOW,
        execution_decision=OrderDecision(
            approved=True,
            reason="approved",
            intent=submitted_intent,
        ),
        submitted_order=PaperOrder(order_id=7, intent=submitted_intent),
    )

    assert blocked.fair_play_allowed is False
    assert blocked.execution_approved is None
    assert rejected.execution_approved is False
    assert submitted.submitted is True
    assert submitted.resulting_order_id == 7
    assert [event.sequence_number for event in ledger.events] == [1, 2, 3]
    assert portfolio.cash_balance == Decimal("150")
    assert portfolio.base_position == Decimal("0")

    ledger.reset()
    assert ledger.events == ()
