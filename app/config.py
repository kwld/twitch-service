from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = PROJECT_ROOT / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(ENV_FILE),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: str = Field(default="dev", alias="APP_ENV")
    app_host: str = Field(default="0.0.0.0", alias="APP_HOST")
    app_port: int = Field(default=8080, alias="APP_PORT")
    app_log_level: str = Field(default="info", alias="APP_LOG_LEVEL")

    database_url: str = Field(alias="DATABASE_URL")

    twitch_client_id: str = Field(alias="TWITCH_CLIENT_ID")
    twitch_client_secret: str = Field(alias="TWITCH_CLIENT_SECRET")
    twitch_eventsub_ws_url: str = Field(
        default="wss://eventsub.wss.twitch.tv/ws",
        alias="TWITCH_EVENTSUB_WS_URL",
    )
    twitch_redirect_uri: str = Field(alias="TWITCH_REDIRECT_URI")
    twitch_scopes: str = Field(
        default=(
            "channel:bot chat:read chat:edit "
            "moderator:read:followers moderator:manage:chat_messages"
        ),
        alias="TWITCH_DEFAULT_SCOPES",
    )

    service_signing_secret: str = Field(alias="SERVICE_SIGNING_SECRET")
    admin_api_key: str = Field(alias="ADMIN_API_KEY")

    ngrok_domain: Optional[str] = Field(default=None, alias="NGROK_DOMAIN")


@dataclass(slots=True)
class RuntimeState:
    settings: Settings


def load_settings() -> Settings:
    if not ENV_FILE.exists():
        raise RuntimeError(
            "Missing .env file. Copy .env.example to .env and fill required values."
        )
    return Settings()
