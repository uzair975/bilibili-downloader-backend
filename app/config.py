from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import List


class Settings(BaseSettings):
    APP_NAME: str = "BiliExtract Engine"
    API_V1_STR: str = "/api/v1"
    PORT: int = 8000
    HOST: str = "0.0.0.0"
    DEBUG: bool = False

    # CORS
    ALLOWED_ORIGINS: List[str] = ["*"]

    # Rate limiting: max requests allowed within window (seconds)
    RATE_LIMIT_REQUESTS: int = 30
    RATE_LIMIT_WINDOW_SECONDS: int = 60

    # Upstream HTTP timeout in seconds
    HTTP_TIMEOUT: float = 12.0

    # Optional Bilibili credentials for VIP / 4K / 1080P60 extraction
    BILIBILI_COOKIE: str = ""
    BILIBILI_SESSDATA: str = ""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )


settings = Settings()
