from enum import Enum


class BotState(Enum):
    STARTING = "starting"
    READY = "ready"
    TRADING = "trading"
    CAUTION = "caution"
    DEFENSIVE = "defensive"
    SURVIVAL = "survival"
    STOPPED = "stopped"


class BotController:

    def __init__(self):

        self.state = BotState.STARTING

    def set_state(self, state: BotState):

        if self.state != state:
            print(f"[STATE] {self.state.value} -> {state.value}")

        self.state = state