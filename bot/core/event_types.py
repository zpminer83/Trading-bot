from enum import Enum


class Event(Enum):

    MARKET_UPDATE = "market_update"

    ORDER_FILLED = "order_filled"

    ORDER_CANCELLED = "order_cancelled"

    POSITION_CHANGED = "position_changed"

    API_ERROR = "api_error"

    WS_DISCONNECTED = "ws_disconnected"

    RISK_CHANGED = "risk_changed"

    BOT_STATE_CHANGED = "bot_state_changed"