from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import ServiceBotAccess, ServiceInterest, TwitchSubscription
from tests.fixtures.factories import create_bot_account, create_service_account


def _run(coro):
    return asyncio.run(coro)


async def _create_service_headers(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    suffix: str,
) -> tuple[object, dict[str, str]]:
    async with session_factory() as session:
        service, raw_secret = await create_service_account(
            session,
            name=f"svc-route-{suffix}",
            client_id=f"cid-route-{suffix}",
            client_secret=f"secret-route-{suffix}",
        )
    return service, {"X-Client-Id": service.client_id, "X-Client-Secret": raw_secret}


async def _create_bot(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    suffix: str,
    enabled: bool = True,
):
    async with session_factory() as session:
        return await create_bot_account(
            session,
            name=f"bot-route-{suffix}",
            twitch_user_id=str(500000000 + int(suffix[-4:], 16) % 400000000),
            twitch_login=f"botroute{suffix[:8]}",
            enabled=enabled,
        )


@pytest.mark.integration
def test_health_endpoint_returns_ok(app_factory) -> None:
    app = app_factory()
    with TestClient(app) as client:
        resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


@pytest.mark.integration
def test_admin_service_account_crud_endpoints(app_factory, admin_auth_headers) -> None:
    app = app_factory()
    with TestClient(app) as client:
        created = client.post("/v1/admin/service-accounts", headers=admin_auth_headers, params={"name": "svc-admin-a"})
        assert created.status_code == 200
        created_payload = created.json()
        assert created_payload["name"] == "svc-admin-a"
        assert created_payload["client_id"]
        assert created_payload["client_secret"]

        listed = client.get("/v1/admin/service-accounts", headers=admin_auth_headers)
        assert listed.status_code == 200
        listed_names = {row["name"] for row in listed.json()}
        assert "svc-admin-a" in listed_names

        regenerated = client.post(
            f"/v1/admin/service-accounts/{created_payload['client_id']}/regenerate",
            headers=admin_auth_headers,
        )
        assert regenerated.status_code == 200
        assert regenerated.json()["client_id"] == created_payload["client_id"]
        assert regenerated.json()["client_secret"]


@pytest.mark.integration
def test_accessible_bots_endpoint_all_and_restricted_modes(app_factory, db_session_factory) -> None:
    app = app_factory()
    suffix = uuid.uuid4().hex[:8]
    service, headers = _run(_create_service_headers(db_session_factory, suffix=suffix))
    bot_allowed = _run(_create_bot(db_session_factory, suffix=f"{suffix}a"))
    bot_other = _run(_create_bot(db_session_factory, suffix=f"{suffix}b"))

    with TestClient(app) as client:
        all_mode = client.get("/v1/bots/accessible", headers=headers)
        assert all_mode.status_code == 200
        all_payload = all_mode.json()
        assert all_payload["access_mode"] == "all"
        all_ids = {row["id"] for row in all_payload["bots"]}
        assert str(bot_allowed.id) in all_ids
        assert str(bot_other.id) in all_ids

        async def _restrict():
            async with db_session_factory() as session:
                session.add(ServiceBotAccess(service_account_id=service.id, bot_account_id=bot_allowed.id))
                await session.commit()

        _run(_restrict())
        restricted_mode = client.get("/v1/bots/accessible", headers=headers)
        assert restricted_mode.status_code == 200
        restricted_payload = restricted_mode.json()
        assert restricted_payload["access_mode"] == "restricted"
        restricted_ids = {row["id"] for row in restricted_payload["bots"]}
        assert restricted_ids == {str(bot_allowed.id)}


@pytest.mark.integration
def test_active_subscription_snapshot_endpoint_supports_cache_and_refresh(app_factory, db_session_factory) -> None:
    app = app_factory()
    import app.main as main_module

    suffix = uuid.uuid4().hex[:8]
    service, headers = _run(_create_service_headers(db_session_factory, suffix=suffix))
    bot = _run(_create_bot(db_session_factory, suffix=suffix))

    async def _seed():
        async with db_session_factory() as session:
            interest = ServiceInterest(
                service_account_id=service.id,
                bot_account_id=bot.id,
                event_type="stream.online",
                broadcaster_user_id="12345",
                transport="websocket",
                webhook_url=None,
                last_heartbeat_at=datetime.now(UTC),
            )
            session.add(interest)
            session.add(
                TwitchSubscription(
                    bot_account_id=bot.id,
                    event_type="stream.online",
                    broadcaster_user_id="12345",
                    twitch_subscription_id="sub-match-1",
                    status="enabled",
                    session_id=None,
                )
            )
            await session.commit()
            await session.refresh(interest)
            return interest.id

    interest_id = _run(_seed())
    now = datetime.now(UTC)
    main_module.eventsub_manager.get_active_subscriptions_snapshot = AsyncMock(
        side_effect=[
            (
                [
                    {
                        "twitch_subscription_id": "sub-match-1",
                        "status": "enabled",
                        "event_type": "stream.online",
                        "broadcaster_user_id": "12345",
                        "upstream_transport": "webhook",
                        "session_id": None,
                        "connected_at": None,
                        "disconnected_at": None,
                        "bot_account_id": str(bot.id),
                    }
                ],
                now,
                True,
            ),
            (
                [
                    {
                        "twitch_subscription_id": "sub-match-1",
                        "status": "enabled",
                        "event_type": "stream.online",
                        "broadcaster_user_id": "12345",
                        "upstream_transport": "webhook",
                        "session_id": None,
                        "connected_at": None,
                        "disconnected_at": None,
                        "bot_account_id": str(bot.id),
                    }
                ],
                now,
                False,
            ),
        ]
    )

    with TestClient(app) as client:
        cached_resp = client.get("/v1/eventsub/subscriptions/active", headers=headers)
        refreshed_resp = client.get("/v1/eventsub/subscriptions/active?refresh=true", headers=headers)

    assert cached_resp.status_code == 200
    assert cached_resp.json()["source"] == "cache"
    assert cached_resp.json()["matched_for_service"] == 1
    assert cached_resp.json()["items"][0]["matched_interest_ids"] == [str(interest_id)]

    assert refreshed_resp.status_code == 200
    assert refreshed_resp.json()["source"] == "twitch_live"
    assert refreshed_resp.json()["matched_for_service"] == 1


@pytest.mark.integration
def test_twitch_profiles_and_stream_status_helpers(app_factory, db_session_factory, monkeypatch) -> None:
    app = app_factory()
    import app.main as main_module

    suffix = uuid.uuid4().hex[:8]
    _, headers = _run(_create_service_headers(db_session_factory, suffix=suffix))
    bot = _run(_create_bot(db_session_factory, suffix=suffix))

    monkeypatch.setattr("app.routes.twitch_routes.ensure_bot_access_token", AsyncMock(return_value="bot-token"))
    main_module.twitch_client.get_users_by_query = AsyncMock(
        return_value=[{"id": "121212", "login": "targetlogin", "display_name": "Target"}]
    )
    main_module.twitch_client.get_streams_by_user_ids = AsyncMock(
        return_value=[
            {
                "user_id": "121212",
                "title": "Live Title",
                "game_name": "Game Name",
                "started_at": "2024-01-01T00:00:00Z",
            }
        ]
    )

    with TestClient(app) as client:
        profiles = client.get(
            f"/v1/twitch/profiles?bot_account_id={bot.id}&logins=targetlogin",
            headers=headers,
        )
        streams = client.get(
            f"/v1/twitch/streams/status?bot_account_id={bot.id}&broadcaster_user_ids=121212",
            headers=headers,
        )

    assert profiles.status_code == 200
    assert profiles.json()["data"][0]["id"] == "121212"
    assert streams.status_code == 200
    assert streams.json()["data"][0]["is_live"] is True
    assert streams.json()["data"][0]["title"] == "Live Title"


@pytest.mark.integration
def test_chat_send_endpoint_happy_and_error_paths(app_factory, db_session_factory, monkeypatch) -> None:
    app = app_factory()
    import app.main as main_module

    suffix = uuid.uuid4().hex[:8]
    _, headers = _run(_create_service_headers(db_session_factory, suffix=suffix))
    bot = _run(_create_bot(db_session_factory, suffix=suffix))

    monkeypatch.setattr("app.routes.twitch_routes.ensure_bot_access_token", AsyncMock(return_value="bot-token"))
    main_module.twitch_client.validate_user_token = AsyncMock(
        return_value={"user_id": bot.twitch_user_id, "scopes": ["user:write:chat", "user:bot"]}
    )
    main_module.twitch_client.send_chat_message = AsyncMock(
        return_value={"message_id": "msg-1", "is_sent": True}
    )

    with TestClient(app) as client:
        success = client.post(
            "/v1/twitch/chat/messages",
            headers=headers,
            json={
                "bot_account_id": str(bot.id),
                "broadcaster_user_id": "909090",
                "message": "hello",
                "auth_mode": "user",
            },
        )
        assert success.status_code == 200
        assert success.json()["is_sent"] is True
        assert success.json()["auth_mode_used"] == "user"

        main_module.twitch_client.send_chat_message = AsyncMock(side_effect=RuntimeError("send failed"))
        failed = client.post(
            "/v1/twitch/chat/messages",
            headers=headers,
            json={
                "bot_account_id": str(bot.id),
                "broadcaster_user_id": "909090",
                "message": "hello again",
                "auth_mode": "user",
            },
        )
        assert failed.status_code == 502
        assert "send failed" in failed.text


@pytest.mark.integration
def test_clip_create_endpoint_happy_and_error_paths(app_factory, db_session_factory, monkeypatch) -> None:
    app = app_factory()
    import app.main as main_module

    suffix = uuid.uuid4().hex[:8]
    _, headers = _run(_create_service_headers(db_session_factory, suffix=suffix))
    bot = _run(_create_bot(db_session_factory, suffix=suffix))

    monkeypatch.setattr("app.routes.twitch_routes.ensure_bot_access_token", AsyncMock(return_value="bot-token"))
    monkeypatch.setattr("app.routes.twitch_routes.asyncio.sleep", AsyncMock(return_value=None))
    main_module.twitch_client.validate_user_token = AsyncMock(
        return_value={"user_id": bot.twitch_user_id, "scopes": ["clips:edit"]}
    )
    main_module.twitch_client.create_clip = AsyncMock(
        return_value={"id": "clip-1", "edit_url": "https://clips.twitch.tv/edit/clip-1"}
    )
    main_module.twitch_client.get_clips = AsyncMock(
        return_value=[
            {
                "id": "clip-1",
                "created_at": "2024-01-01T00:00:00Z",
                "url": "https://clips.twitch.tv/clip-1",
                "embed_url": "https://clips.twitch.tv/embed?clip=clip-1",
                "thumbnail_url": "https://static-cdn.jtvnw.net/clip-1.jpg",
            }
        ]
    )

    with TestClient(app) as client:
        success = client.post(
            "/v1/twitch/clips",
            headers=headers,
            json={
                "bot_account_id": str(bot.id),
                "broadcaster_user_id": "55555",
                "title": "Clip title",
                "duration": 10.0,
                "has_delay": False,
            },
        )
        assert success.status_code == 200
        assert success.json()["status"] == "ready"
        assert success.json()["clip_id"] == "clip-1"

        main_module.twitch_client.create_clip = AsyncMock(side_effect=RuntimeError("clip create failed"))
        failed = client.post(
            "/v1/twitch/clips",
            headers=headers,
            json={
                "bot_account_id": str(bot.id),
                "broadcaster_user_id": "55555",
                "title": "Clip title",
                "duration": 10.0,
                "has_delay": False,
            },
        )
        assert failed.status_code == 502
        assert "clip create failed" in failed.text
