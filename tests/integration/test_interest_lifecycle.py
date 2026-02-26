from __future__ import annotations

import asyncio
import sys
import threading
import time
import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import ServiceAccount, ServiceInterest
from tests.fixtures.factories import create_bot_account, create_service_account


def _run(coro):
    return asyncio.run(coro)


async def _create_service_and_headers(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    suffix: str,
) -> tuple[ServiceAccount, dict[str, str]]:
    async with session_factory() as session:
        service, raw_secret = await create_service_account(
            session,
            name=f"svc-interest-{suffix}",
            client_id=f"cid-interest-{suffix}",
            client_secret=f"secret-interest-{suffix}",
        )
    return service, {
        "X-Client-Id": service.client_id,
        "X-Client-Secret": raw_secret,
    }


async def _create_bot(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    suffix: str,
):
    async with session_factory() as session:
        return await create_bot_account(
            session,
            name=f"bot-interest-{suffix}",
            twitch_user_id=str(300000000 + int(suffix[-4:], 16) % 600000000),
            twitch_login=f"botinterest{suffix[:8]}",
        )


def _interest_payload(bot_id: str, *, event_type: str = "stream.online", broadcaster: str = "12345") -> dict:
    return {
        "bot_account_id": bot_id,
        "event_type": event_type,
        "broadcaster_user_id": broadcaster,
        "transport": "websocket",
        "webhook_url": None,
    }


@pytest.mark.integration
def test_create_interest_invalid_event_type_rejected(app_factory, db_session_factory) -> None:
    app = app_factory()
    suffix = uuid.uuid4().hex[:8]
    _, headers = _run(_create_service_and_headers(db_session_factory, suffix=suffix))
    bot = _run(_create_bot(db_session_factory, suffix=suffix))
    with TestClient(app) as client:
        resp = client.post(
            "/v1/interests",
            headers=headers,
            json=_interest_payload(str(bot.id), event_type="invalid.event"),
        )
    assert resp.status_code == 422
    assert "Unsupported event_type" in resp.text


@pytest.mark.integration
def test_create_interest_webhook_requires_url(app_factory, db_session_factory) -> None:
    app = app_factory()
    suffix = uuid.uuid4().hex[:8]
    _, headers = _run(_create_service_and_headers(db_session_factory, suffix=suffix))
    bot = _run(_create_bot(db_session_factory, suffix=suffix))
    payload = _interest_payload(str(bot.id), event_type="stream.online")
    payload["transport"] = "webhook"
    payload["webhook_url"] = None
    with TestClient(app) as client:
        resp = client.post("/v1/interests", headers=headers, json=payload)
    assert resp.status_code == 422
    assert "webhook_url is required" in resp.text


@pytest.mark.integration
def test_create_interest_broadcaster_login_is_normalized(app_factory, db_session_factory) -> None:
    app = app_factory()
    import app.main as main_module

    suffix = uuid.uuid4().hex[:8]
    _, headers = _run(_create_service_and_headers(db_session_factory, suffix=suffix))
    bot = _run(_create_bot(db_session_factory, suffix=suffix))

    main_module.eventsub_manager.on_interest_added = AsyncMock(return_value=None)
    main_module.twitch_client.app_access_token = AsyncMock(return_value="app-token")
    main_module.twitch_client.get_users_by_query = AsyncMock(
        return_value=[{"id": "77777777", "login": "loginname"}]
    )

    payload = _interest_payload(str(bot.id), event_type="stream.online", broadcaster="SomeLogin")
    with TestClient(app) as client:
        resp = client.post("/v1/interests", headers=headers, json=payload)
    assert resp.status_code == 200
    assert resp.json()["broadcaster_user_id"] == "77777777"


@pytest.mark.integration
def test_create_interest_broadcaster_url_is_normalized(app_factory, db_session_factory) -> None:
    app = app_factory()
    import app.main as main_module

    suffix = uuid.uuid4().hex[:8]
    _, headers = _run(_create_service_and_headers(db_session_factory, suffix=suffix))
    bot = _run(_create_bot(db_session_factory, suffix=suffix))

    main_module.eventsub_manager.on_interest_added = AsyncMock(return_value=None)
    main_module.twitch_client.app_access_token = AsyncMock(return_value="app-token")
    main_module.twitch_client.get_users_by_query = AsyncMock(
        return_value=[{"id": "88888888", "login": "urlname"}]
    )

    payload = _interest_payload(
        str(bot.id),
        event_type="stream.online",
        broadcaster="https://www.twitch.tv/UrlName",
    )
    with TestClient(app) as client:
        resp = client.post("/v1/interests", headers=headers, json=payload)
    assert resp.status_code == 200
    assert resp.json()["broadcaster_user_id"] == "88888888"


@pytest.mark.integration
def test_create_interest_dedupes_same_key(app_factory, db_session_factory) -> None:
    app = app_factory()
    import app.main as main_module

    suffix = uuid.uuid4().hex[:8]
    _, headers = _run(_create_service_and_headers(db_session_factory, suffix=suffix))
    bot = _run(_create_bot(db_session_factory, suffix=suffix))
    main_module.eventsub_manager.on_interest_added = AsyncMock(return_value=None)

    payload = _interest_payload(str(bot.id), event_type="stream.online", broadcaster="55555")
    with TestClient(app) as client:
        first = client.post("/v1/interests", headers=headers, json=payload)
        second = client.post("/v1/interests", headers=headers, json=payload)

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["id"] == second.json()["id"]


@pytest.mark.skipif(sys.platform.startswith("win"), reason="Threaded TestClient startup is unstable on Windows")
@pytest.mark.integration
def test_create_interest_concurrent_requests_reuse_single_row(app_factory, db_session_factory) -> None:
    import app.main as main_module

    suffix = uuid.uuid4().hex[:8]
    _, headers = _run(_create_service_and_headers(db_session_factory, suffix=suffix))
    bot = _run(_create_bot(db_session_factory, suffix=suffix))
    main_module.eventsub_manager.on_interest_added = AsyncMock(return_value=None)
    payload = _interest_payload(str(bot.id), event_type="stream.online", broadcaster="98765")

    barrier = threading.Barrier(2)
    results: list[tuple[int, dict]] = []

    def _worker():
        local_app = app_factory()
        with TestClient(local_app) as client:
            barrier.wait(timeout=10)
            resp = client.post("/v1/interests", headers=headers, json=payload)
            results.append((resp.status_code, resp.json()))

    t1 = threading.Thread(target=_worker)
    t2 = threading.Thread(target=_worker)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert len(results) == 2
    assert all(status == 200 for status, _ in results)
    ids = {payload_json["id"] for _, payload_json in results}
    assert len(ids) == 1


@pytest.mark.integration
def test_create_interest_auto_creates_default_stream_interests(app_factory, db_session_factory) -> None:
    app = app_factory()
    import app.main as main_module

    suffix = uuid.uuid4().hex[:8]
    service, headers = _run(_create_service_and_headers(db_session_factory, suffix=suffix))
    bot = _run(_create_bot(db_session_factory, suffix=suffix))
    main_module.eventsub_manager.on_interest_added = AsyncMock(return_value=None)

    payload = _interest_payload(str(bot.id), event_type="channel.update", broadcaster="424242")
    with TestClient(app) as client:
        resp = client.post("/v1/interests", headers=headers, json=payload)
    assert resp.status_code == 200

    async def _assert_defaults():
        async with db_session_factory() as session:
            rows = list(
                (
                    await session.scalars(
                        select(ServiceInterest).where(
                            ServiceInterest.service_account_id == service.id,
                            ServiceInterest.bot_account_id == bot.id,
                            ServiceInterest.broadcaster_user_id == "424242",
                        )
                    )
                ).all()
            )
            event_types = {row.event_type for row in rows}
            assert "channel.update" in event_types
            assert "stream.online" in event_types
            assert "stream.offline" in event_types

    _run(_assert_defaults())


@pytest.mark.integration
def test_create_interest_upstream_failure_calls_reject_and_returns_502(app_factory, db_session_factory) -> None:
    app = app_factory()
    import app.main as main_module

    suffix = uuid.uuid4().hex[:8]
    _, headers = _run(_create_service_and_headers(db_session_factory, suffix=suffix))
    bot = _run(_create_bot(db_session_factory, suffix=suffix))

    main_module.eventsub_manager.on_interest_added = AsyncMock(side_effect=RuntimeError("upstream failed"))
    main_module.eventsub_manager.reject_interests_for_key = AsyncMock(return_value=1)

    payload = _interest_payload(str(bot.id), event_type="stream.online", broadcaster="11111")
    with TestClient(app) as client:
        resp = client.post("/v1/interests", headers=headers, json=payload)
    assert resp.status_code == 502
    assert "Upstream subscription rejected" in resp.text
    assert main_module.eventsub_manager.reject_interests_for_key.await_count >= 1


@pytest.mark.integration
def test_create_interest_upstream_failure_cleans_up_interest(app_factory, db_session_factory) -> None:
    app = app_factory()
    import app.main as main_module

    suffix = uuid.uuid4().hex[:8]
    service, headers = _run(_create_service_and_headers(db_session_factory, suffix=suffix))
    bot = _run(_create_bot(db_session_factory, suffix=suffix))

    main_module.eventsub_manager.on_interest_added = AsyncMock(side_effect=RuntimeError("upstream failed"))

    payload = _interest_payload(str(bot.id), event_type="stream.online", broadcaster="22222")
    with TestClient(app) as client:
        resp = client.post("/v1/interests", headers=headers, json=payload)
    assert resp.status_code == 502

    async def _assert_cleaned():
        async with db_session_factory() as session:
            row = await session.scalar(
                select(ServiceInterest).where(
                    ServiceInterest.service_account_id == service.id,
                    ServiceInterest.bot_account_id == bot.id,
                    ServiceInterest.event_type == "stream.online",
                    ServiceInterest.broadcaster_user_id == "22222",
                )
            )
            assert row is None

    _run(_assert_cleaned())


@pytest.mark.integration
def test_heartbeat_interest_touches_channel_tuple(app_factory, db_session_factory) -> None:
    app = app_factory()
    import app.main as main_module

    suffix = uuid.uuid4().hex[:8]
    service, headers = _run(_create_service_and_headers(db_session_factory, suffix=suffix))
    bot = _run(_create_bot(db_session_factory, suffix=suffix))
    main_module.eventsub_manager.on_interest_added = AsyncMock(return_value=None)

    base = _interest_payload(str(bot.id), event_type="channel.update", broadcaster="33333")
    with TestClient(app) as client:
        created = client.post("/v1/interests", headers=headers, json=base)
        assert created.status_code == 200
        interest_id = created.json()["id"]

        async def _mark_old():
            async with db_session_factory() as session:
                rows = list(
                    (
                        await session.scalars(
                            select(ServiceInterest).where(
                                ServiceInterest.service_account_id == service.id,
                                ServiceInterest.bot_account_id == bot.id,
                                ServiceInterest.broadcaster_user_id == "33333",
                            )
                        )
                    ).all()
                )
                old = datetime.now(UTC).replace(microsecond=0)
                for row in rows:
                    row.updated_at = old
                    row.last_heartbeat_at = old
                await session.commit()

        _run(_mark_old())
        heartbeat = client.post(f"/v1/interests/{interest_id}/heartbeat", headers=headers)
        assert heartbeat.status_code == 200
        assert heartbeat.json()["touched"] >= 3


@pytest.mark.integration
def test_heartbeat_all_interests_touches_all_for_service(app_factory, db_session_factory) -> None:
    app = app_factory()
    import app.main as main_module

    suffix = uuid.uuid4().hex[:8]
    service, headers = _run(_create_service_and_headers(db_session_factory, suffix=suffix))
    bot_a = _run(_create_bot(db_session_factory, suffix=f"{suffix}a"))
    bot_b = _run(_create_bot(db_session_factory, suffix=f"{suffix}b"))
    main_module.eventsub_manager.on_interest_added = AsyncMock(return_value=None)

    with TestClient(app) as client:
        resp_a = client.post(
            "/v1/interests",
            headers=headers,
            json=_interest_payload(str(bot_a.id), event_type="channel.update", broadcaster="77701"),
        )
        resp_b = client.post(
            "/v1/interests",
            headers=headers,
            json=_interest_payload(str(bot_b.id), event_type="channel.update", broadcaster="77702"),
        )
        assert resp_a.status_code == 200
        assert resp_b.status_code == 200

        async def _count():
            async with db_session_factory() as session:
                return len(
                    list(
                        (
                            await session.scalars(
                                select(ServiceInterest).where(ServiceInterest.service_account_id == service.id)
                            )
                        ).all()
                    )
                )

        before_count = _run(_count())
        heartbeat_all = client.post("/v1/interests/heartbeat", headers=headers)
        assert heartbeat_all.status_code == 200
        assert heartbeat_all.json()["touched"] == before_count
