from __future__ import annotations

import asyncio
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.websockets import WebSocketDisconnect

from app.models import ServiceAccount, ServiceEventTrace, ServiceRuntimeStats
from tests.fixtures.factories import create_service_account


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
            name=f"svc-ws-{suffix}",
            client_id=f"cid-ws-{suffix}",
            client_secret=f"secret-ws-{suffix}",
        )
    return service, {"X-Client-Id": service.client_id, "X-Client-Secret": raw_secret}


@pytest.mark.integration
def test_ws_events_rejects_missing_token(app_factory) -> None:
    app = app_factory()
    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect("/ws/events"):
                pass
    assert exc.value.code == 4401


@pytest.mark.integration
def test_ws_events_rejects_invalid_token(app_factory) -> None:
    app = app_factory()
    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect("/ws/events?ws_token=invalid-token"):
                pass
    assert exc.value.code == 4401


@pytest.mark.integration
def test_ws_events_accepts_valid_token_and_tracks_connect_disconnect(app_factory, db_session_factory) -> None:
    app = app_factory()
    suffix = uuid.uuid4().hex[:8]
    service, headers = _run(_create_service_and_headers(db_session_factory, suffix=suffix))

    with TestClient(app) as client:
        token_resp = client.post("/v1/ws-token", headers=headers)
        assert token_resp.status_code == 200
        ws_token = token_resp.json()["ws_token"]

        with client.websocket_connect(f"/ws/events?ws_token={ws_token}") as ws:
            ws.send_text("ping")

    async def _assert_runtime_and_traces():
        async with db_session_factory() as session:
            stats = await session.get(ServiceRuntimeStats, service.id)
            assert stats is not None
            assert (stats.active_ws_connections or 0) == 0

        # Connect trace should always be present; disconnect trace can be flaky in in-process TestClient.
        for _ in range(20):
            async with db_session_factory() as session:
                traces = list(
                    (
                        await session.scalars(
                            select(ServiceEventTrace).where(ServiceEventTrace.service_account_id == service.id)
                        )
                    ).all()
                )
            event_types = {t.event_type for t in traces}
            if "service.ws.connect" in event_types:
                return
            await asyncio.sleep(0.1)
        assert False, f"Missing websocket connect trace event: {sorted(event_types)}"

    _run(_assert_runtime_and_traces())


@pytest.mark.integration
def test_socketio_http_mismatch_routes(app_factory) -> None:
    app = app_factory()
    with TestClient(app) as client:
        resp_a = client.get("/socket.io")
        resp_b = client.get("/socket.io/")
    assert resp_a.status_code == 426
    assert resp_b.status_code == 426
    assert "Socket.IO is not supported" in resp_a.text
    assert "Socket.IO is not supported" in resp_b.text


@pytest.mark.integration
def test_socketio_websocket_mismatch_route_returns_message(app_factory) -> None:
    app = app_factory()
    with TestClient(app) as client:
        with client.websocket_connect("/socket.io") as ws:
            msg = ws.receive_text()
            assert "Socket.IO is not supported" in msg
            with pytest.raises(WebSocketDisconnect) as exc:
                ws.receive_text()
    assert exc.value.code == 4400


@pytest.mark.integration
def test_websocket_catchall_mismatch_route_returns_message(app_factory) -> None:
    app = app_factory()
    with TestClient(app) as client:
        with client.websocket_connect("/ws/not-a-real-endpoint") as ws:
            msg = ws.receive_text()
            assert "Invalid WebSocket endpoint" in msg
            with pytest.raises(WebSocketDisconnect) as exc:
                ws.receive_text()
    assert exc.value.code == 4404
