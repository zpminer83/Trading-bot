from decimal import Decimal

from bot.execution.conservative_paper_broker import ConservativePaperBroker
from bot.execution.execution_manager import ExecutionManager
from bot.execution.order import OrderIntent
from bot.market.market_cache import MarketCache
from bot.market.models import OrderBook, OrderBookLevel
from bot.portfolio.portfolio_manager import PortfolioManager
from bot.risk.risk_manager import RiskManager


def make_market() -> MarketCache:
    market = MarketCache()

    market.update_orderbook(
        OrderBook(
            symbol="SOMI:USDso",
            bids=[
                OrderBookLevel(price=Decimal("1.00"), quantity=Decimal("100")),
            ],
            asks=[
                OrderBookLevel(price=Decimal("1.02"), quantity=Decimal("100")),
            ],
            timestamp=12345,
        )
    )

    return market


def review(intent: OrderIntent, portfolio: PortfolioManager):
    risk = RiskManager()
    execution = ExecutionManager(portfolio=portfolio, risk_manager=risk)

    return execution.review_order(intent)


def test_passive_buy_order_does_not_fill_immediately():
    market = make_market()
    portfolio = PortfolioManager(initial_cash=Decimal("150"))
    broker = ConservativePaperBroker(portfolio=portfolio)

    intent = OrderIntent(
        symbol="SOMI:USDso",
        side="buy",
        order_type="limit",
        price=Decimal("1.00"),
        quantity=Decimal("5"),
    )

    decision = review(intent=intent, portfolio=portfolio)

    order = broker.submit(decision)
    fills = broker.process_market(market)

    assert order is not None
    assert fills == []
    assert len(broker.open_orders) == 1
    assert portfolio.cash_balance == Decimal("150")
    assert portfolio.base_position == Decimal("0")


def test_crossing_buy_order_fills_at_best_ask():
    market = make_market()
    portfolio = PortfolioManager(initial_cash=Decimal("150"))
    broker = ConservativePaperBroker(portfolio=portfolio)

    intent = OrderIntent(
        symbol="SOMI:USDso",
        side="buy",
        order_type="limit",
        price=Decimal("1.02"),
        quantity=Decimal("4"),
    )

    decision = review(intent=intent, portfolio=portfolio)

    broker.submit(decision)
    fills = broker.process_market(market)

    assert len(fills) == 1

    fill = fills[0]

    assert fill.side == "buy"
    assert fill.price == Decimal("1.02")
    assert fill.quantity == Decimal("4")
    assert fill.notional == Decimal("4.08")

    assert portfolio.cash_balance == Decimal("145.92")
    assert portfolio.base_position == Decimal("4")
    assert broker.open_orders == []


def test_crossing_sell_order_fills_at_best_bid():
    market = make_market()
    portfolio = PortfolioManager(initial_cash=Decimal("150"))

    portfolio.buy(price=Decimal("1.00"), quantity=Decimal("10"))

    broker = ConservativePaperBroker(portfolio=portfolio)

    intent = OrderIntent(
        symbol="SOMI:USDso",
        side="sell",
        order_type="limit",
        price=Decimal("1.00"),
        quantity=Decimal("5"),
    )

    decision = review(intent=intent, portfolio=portfolio)

    broker.submit(decision)
    fills = broker.process_market(market)

    assert len(fills) == 1

    fill = fills[0]

    assert fill.side == "sell"
    assert fill.price == Decimal("1.00")
    assert fill.quantity == Decimal("5")
    assert fill.notional == Decimal("5.00")

    assert portfolio.base_position == Decimal("5")
    assert portfolio.cash_balance == Decimal("145.00")
    assert broker.open_orders == []


def test_cancel_all_removes_open_orders():
    market = make_market()
    portfolio = PortfolioManager(initial_cash=Decimal("150"))
    broker = ConservativePaperBroker(portfolio=portfolio)

    intent = OrderIntent(
        symbol="SOMI:USDso",
        side="buy",
        order_type="limit",
        price=Decimal("1.00"),
        quantity=Decimal("5"),
    )

    decision = review(intent=intent, portfolio=portfolio)

    broker.submit(decision)

    assert len(broker.open_orders) == 1

    broker.cancel_all()

    assert broker.open_orders == []