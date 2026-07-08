from dataclasses import dataclass


@dataclass
class BotContext:

    state: str = "STARTING"

    balance: float = 0

    position: float = 0

    pnl: float = 0

    drawdown: float = 0

    risk_score: float = 0