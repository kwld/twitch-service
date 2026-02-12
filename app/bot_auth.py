from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import BotAccount
from app.twitch import TwitchClient


async def ensure_bot_access_token(
    session: AsyncSession,
    twitch: TwitchClient,
    bot: BotAccount,
    skew_seconds: int = 120,
) -> str:
    now = datetime.now(UTC)
    if bot.token_expires_at and bot.token_expires_at > now + timedelta(seconds=skew_seconds):
        return bot.access_token

    refreshed = await twitch.refresh_token(bot.refresh_token)
    bot.access_token = refreshed.access_token
    bot.refresh_token = refreshed.refresh_token
    bot.token_expires_at = refreshed.expires_at
    await session.commit()
    return bot.access_token
