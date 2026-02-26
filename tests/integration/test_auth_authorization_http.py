from __future__ import annotations

import asyncio
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.websockets import WebSocketDisconnect

from app.models import ServiceBotAccess
from tests.fixtures.factories import create_bot_account, create_service_account


def _run(coro):
    return asyncio.run(coro)


async def _create_service_and_headers(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    suffix: str,
) -> dict[str, str]:
    async with session_factory() as session:
        service, raw_secret = await create_service_account(
            session,
            name=f"svc-{suffix}",
            client_id=f"cid-{suffix}",
            client_secret=f"secret-{suffix}",
        )
    return {
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
            name=f"bot-{suffix}",
            twitch_user_id=str(100000000 + int(suffix[-4:], 16) % 899999999),
            twitch_login=f"botlogin{suffix[:8]}",
        )


@pytest.mark.integration
def test_admin_auth_rejects_invalid_key(app_factory) -> None:
    app = app_factory()
    with TestClient(app) as client:
        resp = client.get("/v1/admin/service-accounts", headers={"X-Admin-Key": "wrong"})
    assert resp.status_code == 401


@pytest.mark.integration
def test_admin_auth_allows_valid_key(app_factory) -> None:
    app = app_factory()
    with TestClient(app) as client:
        resp = client.get("/v1/admin/service-accounts", headers={"X-Admin-Key": "test-admin-key"})
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


@pytest.mark.integration
def test_service_auth_rejects_invalid_credentials(app_factory) -> None:
    app = app_factory()
    with TestClient(app) as client:
        resp = client.get(
            "/v1/interests",
            headers={"X-Client-Id": "wrong", "X-Client-Secret": "wrong"},
        )
    assert resp.status_code == 401


@pytest.mark.integration
def test_service_auth_allows_valid_credentials(app_factory, db_session_factory) -> None:
    app = app_factory()
    suffix = uuid.uuid4().hex[:8]
    headers = _run(_create_service_and_headers(db_session_factory, suffix=suffix))
    with TestClient(app) as client:
        resp = client.get("/v1/interests", headers=headers)
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


@pytest.mark.integration
def test_ws_token_route_allows_single_use_token(app_factory, db_session_factory) -> None:
    app = app_factory()
    suffix = uuid.uuid4().hex[:8]
    headers = _run(_create_service_and_headers(db_session_factory, suffix=suffix))
    with TestClient(app) as client:
        token_resp = client.post("/v1/ws-token", headers=headers)
        assert token_resp.status_code == 200
        token = token_resp.json()["ws_token"]

        with client.websocket_connect(f"/ws/events?ws_token={token}"):
            pass

        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect(f"/ws/events?ws_token={token}"):
                pass
        assert exc.value.code == 4401


@pytest.mark.integration
def test_bot_access_unrestricted_mode_allows_operation(app_factory, db_session_factory) -> None:
    app = app_factory()
    suffix = uuid.uuid4().hex[:8]
    headers = _run(_create_service_and_headers(db_session_factory, suffix=suffix))
    bot = _run(_create_bot(db_session_factory, suffix=suffix))
    with TestClient(app) as client:
        resp = client.post(
            "/v1/broadcaster-authorizations/start-minimal",
            headers=headers,
            json={"bot_account_id": str(bot.id)},
        )
    assert resp.status_code == 200
    assert "authorize_url" in resp.json()


@pytest.mark.integration
def test_bot_access_restricted_mode_allows_listed_bot(app_factory, db_session_factory) -> None:
    app = app_factory()
    suffix = uuid.uuid4().hex[:8]
    headers = _run(_create_service_and_headers(db_session_factory, suffix=suffix))
    bot = _run(_create_bot(db_session_factory, suffix=suffix))

    async def _grant_access():
        from sqlalchemy import select

        from app.models import ServiceAccount

        async with db_session_factory() as session:
            service = await session.scalar(
                select(ServiceAccount).where(ServiceAccount.client_id == headers["X-Client-Id"])
            )
            session.add(ServiceBotAccess(service_account_id=service.id, bot_account_id=bot.id))
            await session.commit()

    _run(_grant_access())
    with TestClient(app) as client:
        resp = client.post(
            "/v1/broadcaster-authorizations/start-minimal",
            headers=headers,
            json={"bot_account_id": str(bot.id)},
        )
    assert resp.status_code == 200


@pytest.mark.integration
def test_bot_access_restricted_mode_forbidden_for_unlisted_bot(app_factory, db_session_factory) -> None:
    app = app_factory()
    suffix = uuid.uuid4().hex[:8]
    headers = _run(_create_service_and_headers(db_session_factory, suffix=suffix))
    allowed_bot = _run(_create_bot(db_session_factory, suffix=f"{suffix}a"))
    denied_bot = _run(_create_bot(db_session_factory, suffix=f"{suffix}b"))

    async def _grant_access():
        from sqlalchemy import select

        from app.models import ServiceAccount

        async with db_session_factory() as session:
            service = await session.scalar(
                select(ServiceAccount).where(ServiceAccount.client_id == headers["X-Client-Id"])
            )
            session.add(ServiceBotAccess(service_account_id=service.id, bot_account_id=allowed_bot.id))
            await session.commit()

    _run(_grant_access())
    with TestClient(app) as client:
        resp = client.post(
            "/v1/broadcaster-authorizations/start-minimal",
            headers=headers,
            json={"bot_account_id": str(denied_bot.id)},
        )
    assert resp.status_code == 403
    assert "not allowed" in resp.text.lower()
