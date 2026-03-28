from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest

from app.event_router import InterestKey, InterestRegistry, LocalEventHub
from app.eventsub_manager import EventSubManager
from app.models import BotAccount, ServiceInterest, TwitchSubscription


class DummyTwitchClient:
    client_id = "client-id"
    eventsub_ws_url = "ws://example.test/ws"

    def __init__(self):
        self.created_payloads: list[dict] = []

    async def create_eventsub_subscription(self, **kwargs):
        self.created_payloads.append(kwargs)
        return {
            "id": "sub-created",
            "status": "enabled",
            "transport": {
                "method": kwargs["transport"]["method"],
                "session_id": kwargs["transport"].get("session_id"),
            },
        }


class ScalarResult:
    def __init__(self, rows):
        self._rows = list(rows)

    def all(self):
        return list(self._rows)


class DummySession:
    def __init__(self, *, bots, interests=None, subscriptions=None, added_rows=None):
        self._bots = list(bots)
        self._interests = list(interests or [])
        self._subscriptions = list(subscriptions or [])
        self._added_rows = added_rows if added_rows is not None else []

    async def get(self, model, key):
        if getattr(model, "__tablename__", "") == "bot_accounts":
            for bot in self._bots:
                if bot.id == key:
                    return bot
        return None

    async def scalar(self, statement):
        text = str(statement)
        if "FROM twitch_subscriptions" in text:
            return self._subscriptions[0] if self._subscriptions else None
        return None

    async def scalars(self, statement):
        text = str(statement)
        if "FROM twitch_subscriptions" in text:
            return ScalarResult(self._subscriptions)
        if "FROM service_interests" in text:
            return ScalarResult(self._interests)
        if "FROM bot_accounts" in text:
            return ScalarResult(self._bots)
        return ScalarResult([])

    async def execute(self, _statement):
        self._subscriptions.clear()
        return None

    def add(self, row):
        self._added_rows.append(row)
        if isinstance(row, TwitchSubscription):
            self._subscriptions.append(row)

    async def delete(self, row):
        if row in self._subscriptions:
            self._subscriptions.remove(row)

    async def flush(self):
        return None

    async def commit(self):
        return None


class DummySessionFactory:
    def __init__(self, *, bots, interests=None, subscriptions=None):
        self.added_rows: list[object] = []
        self.session = DummySession(
            bots=bots,
            interests=interests,
            subscriptions=subscriptions,
            added_rows=self.added_rows,
        )

    @asynccontextmanager
    async def __call__(self):
        yield self.session


@pytest.mark.asyncio
async def test_sync_from_twitch_reconcile_infers_bot_moderator_for_chat_interest(monkeypatch):
    bot = BotAccount(
        id=uuid.uuid4(),
        name="szym-bot",
        twitch_user_id="1403423270",
        twitch_login="szym_bot",
        access_token="token",
        refresh_token="refresh",
        token_expires_at=datetime.now(UTC) + timedelta(hours=1),
        enabled=True,
    )
    interest = ServiceInterest(
        id=uuid.uuid4(),
        service_account_id=uuid.uuid4(),
        bot_account_id=bot.id,
        event_type="channel.chat.message",
        broadcaster_user_id="501584279",
        authorization_source="bot_moderator",
        transport="websocket",
        webhook_url=None,
    )
    session_factory = DummySessionFactory(bots=[bot], interests=[interest], subscriptions=[])
    manager = EventSubManager(
        DummyTwitchClient(),
        session_factory,
        InterestRegistry(),
        LocalEventHub(),
    )

    async def _list_eventsub_subscriptions_all_tokens():
        return [
            {
                "id": "sub-live-chat",
                "type": "channel.chat.message",
                "status": "enabled",
                "condition": {
                    "broadcaster_user_id": "501584279",
                    "user_id": "1403423270",
                },
                "transport": {
                    "method": "websocket",
                    "session_id": "session-1",
                },
            }
        ]

    async def _record_service_actions_for_key(**_kwargs):
        return None

    monkeypatch.setattr(manager, "_list_eventsub_subscriptions_all_tokens", _list_eventsub_subscriptions_all_tokens)
    monkeypatch.setattr(manager, "_record_service_actions_for_key", _record_service_actions_for_key)

    await manager._sync_from_twitch_and_reconcile()

    persisted = [row for row in session_factory.added_rows if isinstance(row, TwitchSubscription)]
    assert len(persisted) == 1
    assert persisted[0].authorization_source == "bot_moderator"


@pytest.mark.asyncio
async def test_ensure_subscription_persists_bot_moderator_for_chat_events(monkeypatch):
    bot = BotAccount(
        id=uuid.uuid4(),
        name="szym-bot",
        twitch_user_id="1403423270",
        twitch_login="szym_bot",
        access_token="token",
        refresh_token="refresh",
        token_expires_at=datetime.now(UTC) + timedelta(hours=1),
        enabled=True,
    )
    twitch = DummyTwitchClient()
    session_factory = DummySessionFactory(bots=[bot], interests=[], subscriptions=[])
    manager = EventSubManager(
        twitch,
        session_factory,
        InterestRegistry(),
        LocalEventHub(),
    )
    manager._session_id = "session-1"

    async def _resolve_authorization_source(**_kwargs):
        return "bot_moderator", set(), {"user:read:chat", "user:bot", "channel:bot"}, "bot-access-token"

    async def _ensure_bot_access_token(*_args, **_kwargs):
        return "bot-access-token"

    async def _record_service_actions_for_key(**_kwargs):
        return None

    monkeypatch.setattr("app.eventsub_manager_parts.subscription_mixin.ensure_bot_access_token", _ensure_bot_access_token)
    monkeypatch.setattr(manager, "_resolve_authorization_source", _resolve_authorization_source)
    monkeypatch.setattr(manager, "_record_service_actions_for_key", _record_service_actions_for_key)

    await manager._ensure_subscription(
        InterestKey(
            bot.id,
            "channel.chat.message",
            "501584279",
            "bot_moderator",
            "",
        )
    )

    persisted = [row for row in session_factory.added_rows if isinstance(row, TwitchSubscription)]
    assert len(persisted) == 1
    assert persisted[0].authorization_source == "bot_moderator"
    assert twitch.created_payloads[0]["condition"] == {
        "broadcaster_user_id": "501584279",
        "moderator_user_id": "1403423270",
        "user_id": "1403423270",
    }
