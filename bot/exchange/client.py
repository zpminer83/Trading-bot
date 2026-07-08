import httpx

from config.settings import settings
from exchange.models import MarketsResponse


class DreamDexClient:

    def __init__(self):

        self.client = httpx.Client(
            base_url=settings.BASE_URL,
            timeout=10,
        )

    def get_markets(self) -> MarketsResponse:

        response = self.client.get("/markets")

        response.raise_for_status()

        return MarketsResponse.model_validate(response.json())

    def close(self):

        self.client.close()