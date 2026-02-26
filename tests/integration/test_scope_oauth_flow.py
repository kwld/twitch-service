from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import BroadcasterAuthorization, BroadcasterAuthorizationRequest, ServiceAccount
from app.twitch import OAuthToken
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
            name=f"svc-scope-{suffix}",
            client_id=f"cid-scope-{suffix}",
            client_secret=f"secret-scope-{suffix}",
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
            name=f"bot-scope-{suffix}",
            twitch_user_id=str(200000000 + int(suffix[-4:], 16) % 700000000),
            twitch_login=f"botscope{suffix[:8]}",
        )


@pytest.mark.integration
def test_resolve_scopes_recommended_mode(app_factory, db_session_factory) -> None:
    app = app_factory()
    suffix = uuid.uuid4().hex[:8]
    _, headers = _run(_create_service_and_headers(db_session_factory, suffix=suffix))
    with TestClient(app) as client:
        resp = client.post(
            "/v1/eventsub/scopes/resolve",
            headers=headers,
            json={"scope_mode": "recommended", "event_types": ["channel.poll.begin"]},
        )
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["scope_mode"] == "recommended"
    assert "channel:bot" in payload["resolved_scopes"]
    assert "channel:read:polls" in payload["resolved_scopes"]


@pytest.mark.integration
def test_resolve_scopes_minimal_mode(app_factory, db_session_factory) -> None:
    app = app_factory()
    suffix = uuid.uuid4().hex[:8]
    _, headers = _run(_create_service_and_headers(db_session_factory, suffix=suffix))
    with TestClient(app) as client:
        resp = client.post(
            "/v1/eventsub/scopes/resolve",
            headers=headers,
            json={"scope_mode": "minimal", "event_types": ["channel.poll.begin"]},
        )
    assert resp.status_code == 200
    assert resp.json()["resolved_scopes"] == ["channel:bot"]


@pytest.mark.integration
def test_resolve_scopes_custom_mode(app_factory, db_session_factory) -> None:
    app = app_factory()
    suffix = uuid.uuid4().hex[:8]
    _, headers = _run(_create_service_and_headers(db_session_factory, suffix=suffix))
    with TestClient(app) as client:
        resp = client.post(
            "/v1/eventsub/scopes/resolve",
            headers=headers,
            json={
                "scope_mode": "custom",
                "custom_scopes": ["channel:read:polls"],
                "include_base_scope": False,
            },
        )
    assert resp.status_code == 200
    assert resp.json()["resolved_scopes"] == ["channel:read:polls"]


@pytest.mark.integration
def test_resolve_scopes_invalid_scope_format_rejected(app_factory, db_session_factory) -> None:
    app = app_factory()
    suffix = uuid.uuid4().hex[:8]
    _, headers = _run(_create_service_and_headers(db_session_factory, suffix=suffix))
    with TestClient(app) as client:
        resp = client.post(
            "/v1/eventsub/scopes/resolve",
            headers=headers,
            json={"scope_mode": "custom", "custom_scopes": ["bad scope"]},
        )
    assert resp.status_code == 422
    assert "invalid format" in resp.text


@pytest.mark.integration
def test_resolve_scopes_unknown_scope_rejected(app_factory, db_session_factory) -> None:
    app = app_factory()
    suffix = uuid.uuid4().hex[:8]
    _, headers = _run(_create_service_and_headers(db_session_factory, suffix=suffix))
    with TestClient(app) as client:
        resp = client.post(
            "/v1/eventsub/scopes/resolve",
            headers=headers,
            json={"scope_mode": "custom", "custom_scopes": ["channel:read:notreal"]},
        )
    assert resp.status_code == 422
    assert "Unsupported Twitch scope" in resp.text


@pytest.mark.integration
def test_resolve_scopes_unknown_event_type_rejected(app_factory, db_session_factory) -> None:
    app = app_factory()
    suffix = uuid.uuid4().hex[:8]
    _, headers = _run(_create_service_and_headers(db_session_factory, suffix=suffix))
    with TestClient(app) as client:
        resp = client.post(
            "/v1/eventsub/scopes/resolve",
            headers=headers,
            json={"scope_mode": "recommended", "event_types": ["unknown.type"]},
        )
    assert resp.status_code == 422
    assert "Unsupported event_types" in resp.text


@pytest.mark.integration
def test_start_authorization_scope_modes(app_factory, db_session_factory) -> None:
    app = app_factory()
    suffix = uuid.uuid4().hex[:8]
    _, headers = _run(_create_service_and_headers(db_session_factory, suffix=suffix))
    bot = _run(_create_bot(db_session_factory, suffix=suffix))

    with TestClient(app) as client:
        minimal = client.post(
            "/v1/broadcaster-authorizations/start",
            headers=headers,
            json={"bot_account_id": str(bot.id), "scope_mode": "minimal"},
        )
        recommended = client.post(
            "/v1/broadcaster-authorizations/start",
            headers=headers,
            json={
                "bot_account_id": str(bot.id),
                "scope_mode": "recommended",
                "event_types": ["channel.poll.begin"],
            },
        )
        custom = client.post(
            "/v1/broadcaster-authorizations/start",
            headers=headers,
            json={
                "bot_account_id": str(bot.id),
                "scope_mode": "custom",
                "custom_scopes": ["channel:read:polls"],
                "include_base_scope": False,
            },
        )

    assert minimal.status_code == 200
    assert minimal.json()["requested_scopes"] == ["channel:bot"]

    assert recommended.status_code == 200
    assert "channel:bot" in recommended.json()["requested_scopes"]
    assert "channel:read:polls" in recommended.json()["requested_scopes"]

    assert custom.status_code == 200
    assert custom.json()["requested_scopes"] == ["channel:read:polls"]


@pytest.mark.integration
def test_start_authorization_custom_without_scopes_rejected(app_factory, db_session_factory) -> None:
    app = app_factory()
    suffix = uuid.uuid4().hex[:8]
    _, headers = _run(_create_service_and_headers(db_session_factory, suffix=suffix))
    bot = _run(_create_bot(db_session_factory, suffix=suffix))
    with TestClient(app) as client:
        resp = client.post(
            "/v1/broadcaster-authorizations/start",
            headers=headers,
            json={
                "bot_account_id": str(bot.id),
                "scope_mode": "custom",
                "include_base_scope": False,
            },
        )
    assert resp.status_code == 422
    assert "custom scope_mode requires custom_scopes" in resp.text


@pytest.mark.integration
def test_oauth_callback_succeeds_when_requested_scopes_granted(app_factory, db_session_factory) -> None:
    app = app_factory()
    import app.main as main_module

    suffix = uuid.uuid4().hex[:8]
    service, _headers = _run(_create_service_and_headers(db_session_factory, suffix=suffix))
    bot = _run(_create_bot(db_session_factory, suffix=suffix))
    state = f"state-ok-{suffix}"

    async def _seed_request():
        async with db_session_factory() as session:
            session.add(
                BroadcasterAuthorizationRequest(
                    state=state,
                    service_account_id=service.id,
                    bot_account_id=bot.id,
                    requested_scopes_csv="channel:bot,channel:read:polls",
                    status="pending",
                )
            )
            await session.commit()

    _run(_seed_request())
    main_module.twitch_client.exchange_code = AsyncMock(
        return_value=OAuthToken(
            access_token="access-ok",
            refresh_token="refresh-ok",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
    )
    main_module.twitch_client.validate_user_token = AsyncMock(
        return_value={
            "user_id": bot.twitch_user_id,
            "login": bot.twitch_login,
            "scopes": ["channel:bot", "channel:read:polls"],
        }
    )

    with TestClient(app) as client:
        resp = client.get(f"/oauth/callback?code=ok-code&state={state}")

    assert resp.status_code == 200
    assert resp.json()["ok"] is True

    async def _assert_db():
        async with db_session_factory() as session:
            req = await session.get(BroadcasterAuthorizationRequest, state)
            assert req is not None
            assert req.status == "completed"
            auth_row = await session.scalar(
                select(BroadcasterAuthorization).where(
                    BroadcasterAuthorization.service_account_id == service.id,
                    BroadcasterAuthorization.bot_account_id == bot.id,
                    BroadcasterAuthorization.broadcaster_user_id == bot.twitch_user_id,
                )
            )
            assert auth_row is not None

    _run(_assert_db())


@pytest.mark.integration
def test_oauth_callback_fails_when_requested_scope_missing(app_factory, db_session_factory) -> None:
    app = app_factory()
    import app.main as main_module

    suffix = uuid.uuid4().hex[:8]
    service, _headers = _run(_create_service_and_headers(db_session_factory, suffix=suffix))
    bot = _run(_create_bot(db_session_factory, suffix=suffix))
    state = f"state-missing-{suffix}"

    async def _seed_request():
        async with db_session_factory() as session:
            session.add(
                BroadcasterAuthorizationRequest(
                    state=state,
                    service_account_id=service.id,
                    bot_account_id=bot.id,
                    requested_scopes_csv="channel:bot,channel:read:polls",
                    status="pending",
                )
            )
            await session.commit()

    _run(_seed_request())
    main_module.twitch_client.exchange_code = AsyncMock(
        return_value=OAuthToken(
            access_token="access-fail",
            refresh_token="refresh-fail",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
    )
    main_module.twitch_client.validate_user_token = AsyncMock(
        return_value={
            "user_id": bot.twitch_user_id,
            "login": bot.twitch_login,
            "scopes": ["channel:bot"],
        }
    )

    with TestClient(app) as client:
        resp = client.get(f"/oauth/callback?code=missing-code&state={state}")

    assert resp.status_code == 400
    assert "required scopes are missing" in resp.text

    async def _assert_db():
        async with db_session_factory() as session:
            req = await session.get(BroadcasterAuthorizationRequest, state)
            assert req is not None
            assert req.status == "failed"
            assert req.error and req.error.startswith("missing_required_scopes:")

    _run(_assert_db())


@pytest.mark.integration
def test_oauth_callback_redirect_success_query_payload(app_factory, db_session_factory) -> None:
    app = app_factory()
    import app.main as main_module

    suffix = uuid.uuid4().hex[:8]
    service, _headers = _run(_create_service_and_headers(db_session_factory, suffix=suffix))
    bot = _run(_create_bot(db_session_factory, suffix=suffix))
    state = f"state-redirect-ok-{suffix}"
    redirect_url = "https://service.example.com/oauth/done"

    async def _seed_request():
        async with db_session_factory() as session:
            session.add(
                BroadcasterAuthorizationRequest(
                    state=state,
                    service_account_id=service.id,
                    bot_account_id=bot.id,
                    requested_scopes_csv="channel:bot",
                    redirect_url=redirect_url,
                    status="pending",
                )
            )
            await session.commit()

    _run(_seed_request())
    main_module.twitch_client.exchange_code = AsyncMock(
        return_value=OAuthToken(
            access_token="access-redirect-ok",
            refresh_token="refresh-redirect-ok",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
    )
    main_module.twitch_client.validate_user_token = AsyncMock(
        return_value={
            "user_id": bot.twitch_user_id,
            "login": bot.twitch_login,
            "scopes": ["channel:bot"],
        }
    )

    with TestClient(app) as client:
        resp = client.get(f"/oauth/callback?code=ok-code&state={state}", follow_redirects=False)

    assert resp.status_code == 302
    location = resp.headers.get("location", "")
    assert location.startswith(redirect_url)
    assert "ok=true" in location
    assert "service_connected=true" in location
    assert "broadcaster_user_id=" in location


@pytest.mark.integration
def test_oauth_callback_redirect_failure_query_payload(app_factory, db_session_factory) -> None:
    app = app_factory()

    suffix = uuid.uuid4().hex[:8]
    service, _headers = _run(_create_service_and_headers(db_session_factory, suffix=suffix))
    bot = _run(_create_bot(db_session_factory, suffix=suffix))
    state = f"state-redirect-fail-{suffix}"
    redirect_url = "https://service.example.com/oauth/done"

    async def _seed_request():
        async with db_session_factory() as session:
            session.add(
                BroadcasterAuthorizationRequest(
                    state=state,
                    service_account_id=service.id,
                    bot_account_id=bot.id,
                    requested_scopes_csv="channel:bot",
                    redirect_url=redirect_url,
                    status="pending",
                )
            )
            await session.commit()

    _run(_seed_request())
    with TestClient(app) as client:
        resp = client.get(f"/oauth/callback?state={state}&error=access_denied", follow_redirects=False)

    assert resp.status_code == 302
    location = resp.headers.get("location", "")
    assert location.startswith(redirect_url)
    assert "ok=false" in location
    assert "error=access_denied" in location
