from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    BASE_URL: str = "https://stg.api.dreamdex.io/v0"
    WS_URL: str = "wss://stg.api.dreamdex.io/v0/ws/public"

    PRIVATE_KEY: str = ""
    JWT_TOKEN: str = ""

    LOG_LEVEL: str = "INFO"

    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore"
    )


settings = Settings()