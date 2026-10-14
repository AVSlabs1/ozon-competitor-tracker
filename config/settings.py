"""Модуль конфигурации и валидации параметров окружения."""

from typing import Optional
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Конфигурация сервиса."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    DATABASE_URL: str = Field(default="sqlite+aiosqlite:///./tracker.db")
    TELEGRAM_BOT_TOKEN: str = Field(default="")
    TELEGRAM_ALERT_CHAT_ID: int = Field(default=0)
    ADMIN_CHAT_ID: int = Field(default=0)
    ADMIN_USERNAME: str = Field(default="")
    CHECK_INTERVAL_MINUTES: int = Field(default=30, ge=1)
    DUMPING_THRESHOLD_PERCENT: float = Field(default=5.0, ge=0.1, le=100.0)
    REQUEST_TIMEOUT_SECONDS: int = Field(default=25, ge=5)
    PROXY_SERVER: Optional[str] = Field(default=None)


settings = Settings()
