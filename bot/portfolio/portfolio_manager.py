from decimal import Decimal


class PortfolioManager:
    """
    Tracks portfolio state for one trading pair.

    For now we support only long-only trading:
    - buy increases base position
    - sell reduces base position
    - short selling is not allowed
    """

    def __init__(self, initial_cash: Decimal):
        self.initial_cash = initial_cash

        self.cash_balance = initial_cash
        self.base_position = Decimal("0")

        self.average_entry_price = Decimal("0")
        self.last_price = Decimal("0")

        self.realized_pnl = Decimal("0")
        self.total_volume = Decimal("0")
        self.fees_paid = Decimal("0")
        self.reserved_cash = Decimal("0")
        self.reserved_base = Decimal("0")

        self.peak_equity = initial_cash

    @property
    def position_value(self) -> Decimal:
        return self.base_position * self.last_price

    @property
    def equity(self) -> Decimal:
        return self.cash_balance + self.position_value

    @property
    def unrealized_pnl(self) -> Decimal:
        if self.base_position == 0:
            return Decimal("0")

        return (self.last_price - self.average_entry_price) * self.base_position

    @property
    def total_pnl(self) -> Decimal:
        return self.realized_pnl + self.unrealized_pnl

    @property
    def starting_equity(self) -> Decimal:
        return self.initial_cash

    @property
    def drawdown(self) -> Decimal:
        if self.peak_equity <= 0:
            return Decimal("0")

        current_equity = self.equity

        if current_equity >= self.peak_equity:
            return Decimal("0")

        return (self.peak_equity - current_equity) / self.peak_equity

    def update_market_price(self, price: Decimal) -> None:
        self.last_price = price
        self._update_peak_equity()

    def buy(self, price: Decimal, quantity: Decimal, *, fee: Decimal = Decimal("0")) -> None:
        cost = price * quantity
        fee = Decimal(str(fee))
        if fee < 0:
            raise ValueError("fee must be non-negative")

        if cost + fee > self.cash_balance:
            raise ValueError("Insufficient cash balance")

        old_position = self.base_position
        new_position = old_position + quantity

        if new_position <= 0:
            raise ValueError("Invalid position size after buy")

        old_cost_basis = self.average_entry_price * old_position
        new_cost_basis = old_cost_basis + cost

        self.average_entry_price = new_cost_basis / new_position

        self.cash_balance -= cost + fee
        self.base_position = new_position
        self.last_price = price
        self.total_volume += cost
        self.fees_paid += fee

        self._update_peak_equity()

    def sell(self, price: Decimal, quantity: Decimal, *, fee: Decimal = Decimal("0")) -> None:
        if quantity > self.base_position:
            raise ValueError("Cannot sell more than current position")
        fee = Decimal(str(fee))
        if fee < 0:
            raise ValueError("fee must be non-negative")

        proceeds = price * quantity

        pnl = (price - self.average_entry_price) * quantity
        self.realized_pnl += pnl

        self.cash_balance += proceeds - fee
        self.base_position -= quantity
        self.last_price = price
        self.total_volume += proceeds
        self.fees_paid += fee

        if self.base_position == 0:
            self.average_entry_price = Decimal("0")

        self._update_peak_equity()

    def _update_peak_equity(self) -> None:
        current_equity = self.equity

        if current_equity > self.peak_equity:
            self.peak_equity = current_equity
