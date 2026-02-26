from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import generate_client_id, generate_client_secret, hash_secret
from app.models import BotAccount, BroadcasterAuthorization, ServiceAccount


async def create_service_account(
    session: AsyncSession,
    *,
    name: str = "test-service",
    client_id: str | None = None,
    client_secret: str | None = None,
    enabled: bool = True,
) -> tuple[ServiceAccount, str]:
    raw_secret = client_secret or generate_client_secret()
    account = ServiceAccount(
        name=name,
        client_id=client_id or generate_client_id(),
        client_secret_hash=hash_secret(raw_secret),
        enabled=enabled,
    )
    session.add(account)
    await session.commit()
    await session.refresh(account)
    return account, raw_secret


async def create_bot_account(
    session: AsyncSession,
    *,
    name: str = "test-bot",
    twitch_user_id: str = "12345678",
    twitch_login: str = "testbot",
    enabled: bool = True,
) -> BotAccount:
    bot = BotAccount(
        name=name,
        twitch_user_id=twitch_user_id,
        twitch_login=twitch_login,
        access_token=f"token-{uuid.uuid4().hex}",
        refresh_token=f"refresh-{uuid.uuid4().hex}",
        token_expires_at=datetime.now(UTC) + timedelta(hours=1),
        enabled=enabled,
    )
    session.add(bot)
    await session.commit()
    await session.refresh(bot)
    return bot


async def create_broadcaster_authorization(
    session: AsyncSession,
    *,
    service_account_id,
    bot_account_id,
    broadcaster_user_id: str,
    broadcaster_login: str,
    scopes: list[str] | None = None,
) -> BroadcasterAuthorization:
    row = BroadcasterAuthorization(
        service_account_id=service_account_id,
        bot_account_id=bot_account_id,
        broadcaster_user_id=broadcaster_user_id,
        broadcaster_login=broadcaster_login,
        scopes_csv=",".join(sorted(set(scopes or []))),
        authorized_at=datetime.now(UTC),
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row
