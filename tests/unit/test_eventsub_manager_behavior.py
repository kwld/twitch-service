from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.event_router import InterestKey, InterestRegistry
from app.eventsub_manager import (
    INTEREST_CLEANUP_INTERVAL_SECONDS,
    EventSubManager,
)
from app.models import (
    BotAccount,
    BroadcasterAuthorization,
    ChannelState,
    ServiceEventTrace,
    ServiceInterest,
    TwitchSubscription,
)
from app.twitch import TwitchApiError
from tests.fixtures.factories import create_bot_account, create_service_account


def _build_manager(session_factory: async_sessionmaker[AsyncSession], *, webhook: bool = True):
    twitch = AsyncMock()
    twitch.client_id = "test-client-id"
    event_hub = SimpleNamespace(
        publish_to_service=AsyncMock(return_value=None),
        publish_webhook=AsyncMock(return_value=None),
        envelope=lambda message_id, event_type, event: {
            "id": message_id,
            "provider": "twitch",
            "type": event_type,
            "event": event,
        },
    )
    manager = EventSubManager(
        twitch_client=twitch,
        session_factory=session_factory,
        registry=InterestRegistry(),
        event_hub=event_hub,
        webhook_callback_url="https://callback.example/webhook" if webhook else None,
        webhook_secret="secret-123" if webhook else None,
    )
    return manager, twitch, event_hub


@pytest.mark.unit
@pytest.mark.asyncio
async def test_reconcile_rebuilds_twitch_subscription_snapshot(db_session_factory) -> None:
    manager, _, _ = _build_manager(db_session_factory, webhook=True)
    async with db_session_factory() as session:
        bot = await create_bot_account(
            session,
            name="bot-reconcile",
            twitch_user_id="10101",
            twitch_login="botreconcile",
        )
        session.add(
            TwitchSubscription(
                bot_account_id=bot.id,
                event_type="stream.online",
                broadcaster_user_id="10101",
                twitch_subscription_id="stale-sub",
                status="enabled",
                session_id=None,
            )
        )
        await session.commit()

    manager._list_eventsub_subscriptions_all_tokens = AsyncMock(
        return_value=[
            {
                "id": "fresh-sub",
                "type": "stream.online",
                "status": "enabled",
                "condition": {"broadcaster_user_id": "10101"},
                "transport": {"method": "webhook"},
            }
        ]
    )

    await manager._sync_from_twitch_and_reconcile()

    async with db_session_factory() as session:
        rows = list((await session.scalars(select(TwitchSubscription))).all())
        assert len(rows) == 1
        assert rows[0].twitch_subscription_id == "fresh-sub"
        assert rows[0].status == "enabled"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_reconcile_removes_dead_websocket_subscription(monkeypatch, db_session_factory) -> None:
    manager, twitch, _ = _build_manager(db_session_factory, webhook=True)
    async with db_session_factory() as session:
        await create_bot_account(
            session,
            name="bot-stale-ws",
            twitch_user_id="20202",
            twitch_login="botstalews",
        )

    manager._list_eventsub_subscriptions_all_tokens = AsyncMock(
        return_value=[
            {
                "id": "dead-ws-sub",
                "type": "channel.chat.message",
                "status": "websocket_disconnected",
                "condition": {"broadcaster_user_id": "90909", "user_id": "20202"},
                "transport": {"method": "websocket", "session_id": "old-session"},
            }
        ]
    )
    monkeypatch.setattr(
        "app.eventsub_manager_parts.subscription_mixin.ensure_bot_access_token",
        AsyncMock(return_value="bot-token"),
    )

    await manager._sync_from_twitch_and_reconcile()

    twitch.delete_eventsub_subscription.assert_awaited_once_with("dead-ws-sub", access_token="bot-token")
    async with db_session_factory() as session:
        rows = list((await session.scalars(select(TwitchSubscription))).all())
        assert rows == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_ensure_subscription_uses_transport_version_and_required_condition_fields(
    monkeypatch,
    db_session_factory,
) -> None:
    manager, twitch, _ = _build_manager(db_session_factory, webhook=True)
    async with db_session_factory() as session:
        bot = await create_bot_account(
            session,
            name="bot-ensure-chat",
            twitch_user_id="30303",
            twitch_login="botensurechat",
        )
    manager._session_id = "session-1"
    monkeypatch.setattr(
        "app.eventsub_manager_parts.subscription_mixin.ensure_bot_access_token",
        AsyncMock(return_value="bot-token-30303"),
    )
    twitch.validate_user_token = AsyncMock(
        return_value={"scopes": ["user:read:chat", "user:bot", "channel:bot"]}
    )
    twitch.create_eventsub_subscription = AsyncMock(
        return_value={"id": "created-chat", "status": "enabled", "transport": {"session_id": "session-1"}}
    )

    key = InterestKey(
        bot_account_id=bot.id,
        event_type="channel.chat.message",
        broadcaster_user_id="30303",
    )
    await manager._ensure_subscription(key)

    twitch.create_eventsub_subscription.assert_awaited_once()
    kwargs = twitch.create_eventsub_subscription.await_args.kwargs
    assert kwargs["event_type"] == "channel.chat.message"
    assert kwargs["version"] == "1"
    assert kwargs["transport"] == {"method": "websocket", "session_id": "session-1"}
    assert kwargs["condition"]["broadcaster_user_id"] == "30303"
    assert kwargs["condition"]["user_id"] == "30303"
    assert kwargs["access_token"] == "bot-token-30303"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_ensure_subscription_rejects_missing_bot_scopes(monkeypatch, db_session_factory) -> None:
    manager, twitch, _ = _build_manager(db_session_factory, webhook=True)
    async with db_session_factory() as session:
        bot = await create_bot_account(
            session,
            name="bot-missing-scopes",
            twitch_user_id="40404",
            twitch_login="botmissingscopes",
        )
    manager._session_id = "session-x"
    monkeypatch.setattr(
        "app.eventsub_manager_parts.subscription_mixin.ensure_bot_access_token",
        AsyncMock(return_value="bot-token-40404"),
    )
    twitch.validate_user_token = AsyncMock(return_value={"scopes": ["user:read:chat"]})

    key = InterestKey(
        bot_account_id=bot.id,
        event_type="channel.chat.message",
        broadcaster_user_id="40404",
    )
    with pytest.raises(RuntimeError, match="missing proper authorization"):
        await manager._ensure_subscription(key)
    assert twitch.create_eventsub_subscription.await_count == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_ensure_subscription_rejects_missing_broadcaster_grant_scopes(db_session_factory) -> None:
    manager, twitch, _ = _build_manager(db_session_factory, webhook=True)
    async with db_session_factory() as session:
        bot = await create_bot_account(
            session,
            name="bot-grant-scope-check",
            twitch_user_id="50505",
            twitch_login="botgrantscopecheck",
        )
        service, _ = await create_service_account(
            session,
            name="svc-grant-scope-check",
            client_id="svc-grant-scope-check",
            client_secret="secret-grant-scope-check",
        )
        session.add(
            BroadcasterAuthorization(
                service_account_id=service.id,
                bot_account_id=bot.id,
                broadcaster_user_id="99999",
                broadcaster_login="broadcaster99999",
                scopes_csv="bits:read",
            )
        )
        await session.commit()

    key = InterestKey(
        bot_account_id=bot.id,
        event_type="channel.subscribe",
        broadcaster_user_id="99999",
    )
    with pytest.raises(RuntimeError, match="missing proper authorization"):
        await manager._ensure_subscription(key)
    assert twitch.create_eventsub_subscription.await_count == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_ensure_subscription_handles_stale_websocket_session_error(
    monkeypatch,
    db_session_factory,
) -> None:
    manager, twitch, _ = _build_manager(db_session_factory, webhook=True)
    async with db_session_factory() as session:
        bot = await create_bot_account(
            session,
            name="bot-stale-session",
            twitch_user_id="60606",
            twitch_login="botstalesession",
        )
    manager._session_id = "session-stale"
    monkeypatch.setattr(
        "app.eventsub_manager_parts.subscription_mixin.ensure_bot_access_token",
        AsyncMock(return_value="bot-token-60606"),
    )
    twitch.validate_user_token = AsyncMock(
        return_value={"scopes": ["user:read:chat", "user:bot", "channel:bot"]}
    )
    twitch.create_eventsub_subscription = AsyncMock(
        side_effect=TwitchApiError("session does not exist")
    )

    key = InterestKey(
        bot_account_id=bot.id,
        event_type="channel.chat.message",
        broadcaster_user_id="60606",
    )
    await manager._ensure_subscription(key)

    assert manager._session_id is None
    async with db_session_factory() as session:
        row = await session.scalar(
            select(TwitchSubscription).where(TwitchSubscription.bot_account_id == bot.id)
        )
        assert row is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_notification_fanout_updates_channel_state_and_writes_traces(db_session_factory) -> None:
    manager, twitch, event_hub = _build_manager(db_session_factory, webhook=True)
    async with db_session_factory() as session:
        bot = await create_bot_account(
            session,
            name="bot-fanout",
            twitch_user_id="70707",
            twitch_login="botfanout",
        )
        service_ws, _ = await create_service_account(
            session,
            name="svc-fanout-ws",
            client_id="svc-fanout-ws",
            client_secret="secret-fanout-ws",
        )
        service_hook, _ = await create_service_account(
            session,
            name="svc-fanout-hook",
            client_id="svc-fanout-hook",
            client_secret="secret-fanout-hook",
        )
        interest_ws = ServiceInterest(
            service_account_id=service_ws.id,
            bot_account_id=bot.id,
            event_type="stream.online",
            broadcaster_user_id="80808",
            transport="websocket",
            webhook_url=None,
        )
        interest_hook = ServiceInterest(
            service_account_id=service_hook.id,
            bot_account_id=bot.id,
            event_type="stream.online",
            broadcaster_user_id="80808",
            transport="webhook",
            webhook_url="https://hooks.example/events",
        )
        session.add(interest_ws)
        session.add(interest_hook)
        session.add(
            TwitchSubscription(
                bot_account_id=bot.id,
                event_type="stream.online",
                broadcaster_user_id="80808",
                twitch_subscription_id="sub-fanout-1",
                status="enabled",
                session_id=None,
            )
        )
        await session.commit()
        await session.refresh(interest_ws)
        await session.refresh(interest_hook)

    await manager.registry.load([interest_ws, interest_hook])
    twitch.get_user_by_id_app = AsyncMock(return_value={"id": "80808", "display_name": "Broadcaster"})

    await manager._forward_notification_payload(
        payload={
            "subscription": {
                "id": "sub-fanout-1",
                "type": "stream.online",
                "condition": {"broadcaster_user_id": "80808"},
            },
            "event": {"broadcaster_user_id": "80808", "started_at": "2024-01-01T00:00:00Z"},
        },
        message_id="msg-online",
        incoming_transport="twitch_websocket",
    )

    event_hub.publish_to_service.assert_awaited_once()
    event_hub.publish_webhook.assert_awaited_once()
    async with db_session_factory() as session:
        state = await session.scalar(
            select(ChannelState).where(
                ChannelState.bot_account_id == bot.id,
                ChannelState.broadcaster_user_id == "80808",
            )
        )
        assert state is not None
        assert state.is_live is True
        traces = list((await session.scalars(select(ServiceEventTrace))).all())
        assert len(traces) >= 4

    await manager._forward_notification_payload(
        payload={
            "subscription": {
                "id": "sub-fanout-1",
                "type": "stream.offline",
                "condition": {"broadcaster_user_id": "80808"},
            },
            "event": {"broadcaster_user_id": "80808"},
        },
        message_id="msg-offline",
        incoming_transport="twitch_websocket",
    )
    async with db_session_factory() as session:
        state = await session.scalar(
            select(ChannelState).where(
                ChannelState.bot_account_id == bot.id,
                ChannelState.broadcaster_user_id == "80808",
            )
        )
        assert state is not None
        assert state.is_live is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cleanup_loop_uses_configured_interval(monkeypatch, db_session_factory) -> None:
    manager, _, _ = _build_manager(db_session_factory, webhook=True)
    prune_calls = 0
    sleep_calls: list[int | float] = []

    async def _prune() -> int:
        nonlocal prune_calls
        prune_calls += 1
        manager._stop.set()
        return 0

    async def _sleep(seconds):
        sleep_calls.append(seconds)
        return None

    manager.prune_stale_interests = AsyncMock(side_effect=_prune)
    monkeypatch.setattr("app.eventsub_manager.asyncio.sleep", _sleep)

    await manager._cleanup_stale_interests_loop()

    assert prune_calls == 1
    assert sleep_calls == [INTEREST_CLEANUP_INTERVAL_SECONDS]

