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
    app_eventsub_log_path: str = Field(default="./logs/eventsub.log", alias="APP_EVENTSUB_LOG_PATH")
    app_allowed_ips: str = Field(default="", alias="APP_ALLOWED_IPS")
    app_trust_x_forwarded_for: bool = Field(default=False, alias="APP_TRUST_X_FORWARDED_FOR")
    app_webhook_target_allowlist: str = Field(default="", alias="APP_WEBHOOK_TARGET_ALLOWLIST")
    app_block_private_webhook_targets: bool = Field(default=True, alias="APP_BLOCK_PRIVATE_WEBHOOK_TARGETS")
    loki_host: Optional[str] = Field(default=None, alias="LOKI_HOST")
    loki_port: Optional[int] = Field(default=None, alias="LOKI_PORT")

    database_url: str = Field(alias="DATABASE_URL")

    twitch_client_id: str = Field(alias="TWITCH_CLIENT_ID")
    twitch_client_secret: str = Field(alias="TWITCH_CLIENT_SECRET")
    twitch_eventsub_ws_url: str = Field(
        default="wss://eventsub.wss.twitch.tv/ws",
        alias="TWITCH_EVENTSUB_WS_URL",
    )
    twitch_eventsub_webhook_callback_url: Optional[str] = Field(
        default=None,
        alias="TWITCH_EVENTSUB_WEBHOOK_CALLBACK_URL",
    )
    twitch_eventsub_webhook_secret: str = Field(
        min_length=10,
        max_length=100,
        alias="TWITCH_EVENTSUB_WEBHOOK_SECRET",
    )
    twitch_redirect_uri: str = Field(alias="TWITCH_REDIRECT_URI")
    twitch_eventsub_webhook_event_types: str = Field(
        default="stream.online,stream.offline",
        alias="TWITCH_EVENTSUB_WEBHOOK_EVENT_TYPES",
    )
    twitch_scopes: str = Field(
        default=(
            "channel:bot user:bot user:read:chat user:write:chat clips:edit chat:read chat:edit "
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
    return Settings()
