from decimal import Decimal
import socket

from bot.execution.conservative_paper_broker import ConservativePaperBroker
from bot.execution.order import OrderDecision, OrderIntent
from bot.market.market_cache import MarketCache
from bot.market.models import OrderBook, OrderBookLevel
from bot.portfolio.portfolio_manager import PortfolioManager
from scripts import run_fair_play_paper_scenario as scenario


def test_pre_trade_cooldown_scenario_passes_and_blocks_sell_before_review():
    result = scenario.run_pre_trade_cooldown_scenario()

    assert result.passed is True
    assert len(result.confirmed_buy_fills) == 1
    assert result.confirmed_fills_from_broker is True
    assert result.position > 0
    assert result.blocked_intents >= 1
    assert "opposite_side_cooldown" in result.fair_play_decision_reasons
    assert result.execution_reviewed_sell_intents == 0
    assert result.submitted_sell_orders == 0
    assert result.guard_latched is False
    assert result.competition_volume == result.confirmed_buy_fills[0].notional


def test_confirmed_round_trip_is_created_by_broker_and_latches_guard():
    result = scenario.run_confirmed_round_trip_latch_scenario()

    assert result.passed is True
    assert len(result.broker_fills) == 2
    assert result.broker_created_fills_only is True
    assert result.opposite_fill_delay == Decimal("20")
    assert result.quantity_difference_ratio == Decimal("0")
    assert result.short_window_detected is True
    assert result.short_window_round_trip_count == 1
    assert result.guard_latched is True
    assert result.guard_reason == "short_window_round_trip"
    assert result.reset_successful is True
    assert result.competition_volume_created is False


def test_scenario_is_offline_and_prints_a_passing_report(monkeypatch, capsys):
    monkeypatch.setattr(
        socket,
        "create_connection",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("scenario must not make network calls")
        ),
    )

    assert scenario.main() == 0
    output = capsys.readouterr().out
    assert "FAIR-PLAY PAPER SAFETY SCENARIO" in output
    assert "Scenario 1" in output
    assert "isolated direct broker test harness" in output
    assert "Overall result: PASS" in output


def test_scenario_does_not_change_conservative_broker_passive_fill_rule():
    portfolio = PortfolioManager(initial_cash=Decimal("150"))
    broker = ConservativePaperBroker(portfolio=portfolio)
    market = MarketCache()
    market.update_orderbook(
        OrderBook(
            symbol=scenario.SYMBOL,
            bids=[OrderBookLevel(price=Decimal("100"), quantity=Decimal("10"))],
            asks=[OrderBookLevel(price=Decimal("101"), quantity=Decimal("10"))],
            timestamp=int(scenario.T0.timestamp()),
        )
    )
    broker.submit(
        OrderDecision(
            approved=True,
            reason="approved",
            intent=OrderIntent(
                symbol=scenario.SYMBOL,
                side="buy",
                order_type="limit",
                price=Decimal("100"),
                quantity=Decimal("1"),
            ),
        )
    )

    assert broker.process_market(market) == []
    assert portfolio.base_position == Decimal("0")
