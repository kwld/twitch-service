import asyncio
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest

from app.event_router import InterestRegistry, LocalEventHub
from app.eventsub_manager import EventSubManager
from app.models import BotAccount, ServiceInterest, TwitchSubscription


class DummyTwitchClient:
    client_id = "client-id"
    eventsub_ws_url = "ws://example.test/ws"

    def __init__(self):
        self.calls = []

    async def close(self):
        return None

    async def list_eventsub_subscriptions(self, access_token=None):
        self.calls.append(access_token or "app")
        if access_token is None:
            return [
                {"id": "shared", "type": "stream.online", "condition": {"broadcaster_user_id": "1"}, "transport": {"method": "websocket"}},
                {"id": "app-only", "type": "stream.offline", "condition": {"broadcaster_user_id": "2"}, "transport": {"method": "websocket"}},
            ]
        if access_token == "token-bot-1":
            return [
                {"id": "shared", "type": "stream.online", "condition": {"broadcaster_user_id": "1"}, "transport": {"method": "websocket"}},
                {"id": "bot1-only", "type": "channel.chat.message", "condition": {"broadcaster_user_id": "3", "user_id": "11"}, "transport": {"method": "websocket"}},
            ]
        if access_token == "token-bot-2":
            return [
                {"id": "bot2-only", "type": "stream.online", "condition": {"broadcaster_user_id": "4"}, "transport": {"method": "webhook"}},
            ]
        return []


class DummySession:
    def __init__(self, *, twitch_subscriptions=None, bots=None):
        self._twitch_subscriptions = list(twitch_subscriptions or [])
        self._bots = list(bots or [])

    async def get(self, model, key):
        if getattr(model, "__tablename__", "") == "bot_accounts":
            for bot in self._bots:
                if bot.id == key:
                    return bot
        return None

    async def scalars(self, statement):
        text = str(statement)
        if "FROM twitch_subscriptions" in text:
            return _ScalarResult(self._twitch_subscriptions)
        if "FROM bot_accounts" in text:
            return _ScalarResult(self._bots)
        return _ScalarResult([])


class _ScalarResult:
    def __init__(self, rows):
        self._rows = list(rows)

    def all(self):
        return list(self._rows)


def make_session_factory(*, twitch_subscriptions=None, bots=None):
    @asynccontextmanager
    async def _factory():
        yield DummySession(twitch_subscriptions=twitch_subscriptions, bots=bots)

    return _factory


@pytest.mark.asyncio
async def test_start_defers_stream_refresh_until_session_welcome_when_websocket_interest_exists(monkeypatch):
    manager = EventSubManager(
        DummyTwitchClient(),
        make_session_factory(),
        InterestRegistry(),
        LocalEventHub(),
    )
    calls = []

    async def _record(name):
        calls.append(name)

    async def _sleep_forever():
        await asyncio.sleep(3600)

    monkeypatch.setattr(manager, "_load_interests", lambda: _record("load_interests"))
    monkeypatch.setattr(manager, "_sync_from_twitch_and_reconcile", lambda: _record("reconcile"))
    monkeypatch.setattr(manager, "_ensure_authorization_revoke_subscription", lambda: _record("ensure_revoke"))
    monkeypatch.setattr(manager, "_ensure_webhook_subscriptions", lambda: _record("ensure_webhooks"))
    monkeypatch.setattr(manager, "_refresh_stream_states_for_active_subscriptions", lambda: _record("refresh_active"))
    monkeypatch.setattr(manager, "_refresh_stream_states_for_interested_channels", lambda: _record("refresh_interested"))
    monkeypatch.setattr(manager, "_has_websocket_interest", lambda: asyncio.sleep(0, result=True))
    monkeypatch.setattr(manager, "_run", _sleep_forever)
    monkeypatch.setattr(manager, "_cleanup_stale_interests_loop", _sleep_forever)

    await manager.start()
    try:
        assert manager._startup_refresh_deferred_to_session_welcome is True
        assert calls == ["load_interests", "reconcile", "ensure_revoke", "ensure_webhooks"]
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_db_active_subscriptions_snapshot_uses_local_rows_only():
    bot_id = uuid.uuid4()
    rows = [
        TwitchSubscription(
            bot_account_id=bot_id,
            event_type="stream.online",
            broadcaster_user_id="123",
            twitch_subscription_id="sub-1",
            status="enabled",
            session_id="sess-1",
            last_seen_at=datetime.now(UTC),
        ),
        TwitchSubscription(
            bot_account_id=bot_id,
            event_type="channel.chat.message",
            broadcaster_user_id="456",
            twitch_subscription_id="sub-2",
            status="enabled",
            session_id="sess-2",
            last_seen_at=datetime.now(UTC),
        ),
    ]

    manager = EventSubManager(
        DummyTwitchClient(),
        make_session_factory(twitch_subscriptions=rows),
        InterestRegistry(),
        LocalEventHub(),
    )

    snapshot, cached_at = await manager.get_db_active_subscriptions_snapshot()

    assert isinstance(cached_at, datetime)
    assert len(snapshot) == 2
    assert snapshot[0]["twitch_subscription_id"] == "sub-1"
    assert snapshot[0]["upstream_transport"] == "websocket"
    assert snapshot[1]["twitch_subscription_id"] == "sub-2"
    assert snapshot[1]["bot_account_id"] == str(bot_id)


@pytest.mark.asyncio
async def test_list_eventsub_subscriptions_all_tokens_dedupes_across_app_and_bot_tokens(monkeypatch):
    bot1 = BotAccount(
        id=uuid.uuid4(),
        name="bot-1",
        twitch_user_id="11",
        twitch_login="bot1",
        access_token="old-1",
        refresh_token="refresh-1",
        token_expires_at=datetime.now(UTC) + timedelta(hours=1),
        enabled=True,
    )
    bot2 = BotAccount(
        id=uuid.uuid4(),
        name="bot-2",
        twitch_user_id="22",
        twitch_login="bot2",
        access_token="old-2",
        refresh_token="refresh-2",
        token_expires_at=datetime.now(UTC) + timedelta(hours=1),
        enabled=True,
    )
    twitch = DummyTwitchClient()
    manager = EventSubManager(
        twitch,
        make_session_factory(bots=[bot1, bot2]),
        InterestRegistry(),
        LocalEventHub(),
    )

    async def _fake_ensure(_session, _twitch, bot, skew_seconds=120):
        return f"token-{bot.name}"

    monkeypatch.setattr("app.eventsub_manager.ensure_bot_access_token", _fake_ensure)

    subs = await manager._list_eventsub_subscriptions_all_tokens()

    ids = sorted(sub["id"] for sub in subs)
    assert ids == ["app-only", "bot1-only", "bot2-only", "shared"]
    assert twitch.calls[0] == "app"
    assert sorted(twitch.calls[1:]) == ["token-bot-1", "token-bot-2"]


@pytest.mark.asyncio
async def test_ensure_all_subscriptions_uses_bounded_concurrency(monkeypatch):
    bot_id = uuid.uuid4()
    service_id = uuid.uuid4()
    registry = InterestRegistry()
    manager = EventSubManager(
        DummyTwitchClient(),
        make_session_factory(),
        registry,
        LocalEventHub(),
    )
    manager._session_id = "session-1"
    manager._subscription_ensure_concurrency = 3

    for broadcaster_id in ("100", "101", "102", "103", "104", "105"):
        await registry.add(
            ServiceInterest(
                id=uuid.uuid4(),
                service_account_id=service_id,
                bot_account_id=bot_id,
                event_type="stream.online",
                broadcaster_user_id=broadcaster_id,
                transport="websocket",
                webhook_url=None,
            )
        )

    running = 0
    max_running = 0
    seen: list[str] = []
    state_lock = asyncio.Lock()

    async def _fake_ensure(key):
        nonlocal running, max_running
        async with state_lock:
            running += 1
            max_running = max(max_running, running)
            seen.append(key.broadcaster_user_id)
        await asyncio.sleep(0.02)
        async with state_lock:
            running -= 1

    monkeypatch.setattr(manager, "_ensure_subscription", _fake_ensure)
    monkeypatch.setattr(manager, "reject_interests_for_key", lambda **_: asyncio.sleep(0))

    await manager._ensure_all_subscriptions()

    assert sorted(seen) == ["100", "101", "102", "103", "104", "105"]
    assert max_running == 3
