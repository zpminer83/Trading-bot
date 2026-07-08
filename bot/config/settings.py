from dataclasses import dataclass


@dataclass
class Settings:

    paper_trading: bool = True

    max_drawdown: float = 0.10

    max_position_percent: float = 0.40

    order_size_usd: float = 5.0

    trading_symbol: str = "SOMI:USDso"

    log_level: str = "INFO"


settings = Settings()