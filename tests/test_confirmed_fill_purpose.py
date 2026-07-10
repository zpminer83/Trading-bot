from datetime import datetime, timezone
from decimal import Decimal

from bot.competition.confirmed_fill_ledger import ConfirmedFillLedger
from bot.execution.conservative_paper_broker import ConservativePaperBroker
from bot.execution.order import OrderDecision, OrderIntent, OrderPurpose
from bot.execution.paper_broker import PaperFill
from bot.market.market_cache import MarketCache
from bot.market.models import OrderBook, OrderBookLevel
from bot.portfolio.portfolio_manager import PortfolioManager


def test_confirmed_fill_inherits_metadata_from_actual_filled_paper_order():
    portfolio = PortfolioManager(initial_cash=Decimal("150"))
    broker = ConservativePaperBroker(portfolio=portfolio)
    market = MarketCache()
    market.update_orderbook(
        OrderBook(
            symbol="SOMI:USDso",
            bids=[OrderBookLevel(price=Decimal("0.99"), quantity=Decimal("10"))],
            asks=[OrderBookLevel(price=Decimal("1"), quantity=Decimal("10"))],
            timestamp=1_784_332_800,
        )
    )
    intent = OrderIntent(
        symbol="SOMI:USDso",
        side="buy",
        order_type="limit",
        price=Decimal("1"),
        quantity=Decimal("5"),
        purpose=OrderPurpose.ENTRY,
        strategy_name="test_strategy",
        rationale="test entry",
        signal_id="signal:entry-1",
    )
    order = broker.submit(OrderDecision(approved=True, reason="approved", intent=intent))
    fills = broker.process_market(market)

    assert order is not None
    event = ConfirmedFillLedger().record_fills(
        fills,
        starting_position=Decimal("0"),
        timestamp=datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc),
        source_orders_by_fill_id={id(fills[0]): broker.source_order_for_fill(fills[0])},
    )[0]

    assert event.purpose == "entry"
    assert event.strategy_name == "test_strategy"
    assert event.rationale == "test entry"
    assert event.signal_id == "signal:entry-1"
    assert event.source_order_id == order.order_id


def test_confirmed_fill_without_filled_order_metadata_stays_unknown():
    fill = PaperFill(
        symbol="SOMI:USDso",
        side="buy",
        price=Decimal("1"),
        quantity=Decimal("5"),
        notional=Decimal("5"),
    )
    event = ConfirmedFillLedger().record_fills(
        [fill],
        starting_position=Decimal("0"),
    )[0]

    assert event.purpose == "unknown"
    assert event.strategy_name == "unknown"
    assert event.source_order_id is None
