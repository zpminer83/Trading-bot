import os
import sys

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from bot.core.controller import BotController
from bot.core.events import EventBus
from bot.core.context import BotContext
from bot.analytics.metrics import Metrics
from bot.core.logger import logger
from bot.core.constants import BOT_NAME, VERSION


def main():

    logger.info("=" * 60)
    logger.info(f"{BOT_NAME} v{VERSION}")
    logger.info("Initializing bot...")
    logger.info("=" * 60)

    context = BotContext()
    bus = EventBus()
    metrics = Metrics()

    controller = BotController()

    logger.info("Context created")
    logger.info("EventBus created")
    logger.info("Metrics created")
    logger.info("Controller created")

    logger.info("Bot initialized successfully")


if __name__ == "__main__":
    main()