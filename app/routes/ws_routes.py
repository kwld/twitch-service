from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import FastAPI, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import PlainTextResponse

from app.models import ServiceAccount

SOCKET_IO_MISMATCH_MESSAGE = (
    "Socket.IO is not supported by this service. Use plain WebSocket endpoint "
    "/ws/events?ws_token=<short_lived_token>."
)
WS_ENDPOINT_MISMATCH_MESSAGE = (
    "Invalid WebSocket endpoint. Use /ws/events?ws_token=<short_lived_token>. "
    "Socket.IO is not supported."
)


def register_ws_routes(
    app: FastAPI,
    *,
    settings: Any,
    logger,
    session_factory,
    consume_ws_token: Callable[[str], Awaitable[Any]],
    record_service_trace: Callable[..., Awaitable[None]],
    event_hub,
    resolve_client_ip: Callable[..., str | None],
    is_ip_allowed: Callable[[str | None], bool],
) -> None:
    @app.websocket("/ws/events")
    async def ws_events(
        websocket: WebSocket,
        ws_token: str | None = Query(default=None),
    ):
        client_ip = resolve_client_ip(
            websocket.client.host if websocket.client else None,
            websocket.headers.get("x-forwarded-for"),
            trust_x_forwarded_for=settings.app_trust_x_forwarded_for,
        )
        if not is_ip_allowed(client_ip):
            logger.warning("Blocked WebSocket connection from IP %s", client_ip or "unknown")
            await websocket.close(code=4403)
            return
        raw_ws_token = (ws_token or "").strip()
        token_value = raw_ws_token if raw_ws_token and raw_ws_token.lower() not in {"undefined", "null"} else ""
        if not token_value:
            await websocket.close(code=4401)
            return
        service_account_id = await consume_ws_token(token_value)
        if not service_account_id:
            await websocket.close(code=4401)
            return
        async with session_factory() as session:
            service = await session.get(ServiceAccount, service_account_id)
        if not service or not service.enabled:
            await websocket.close(code=4401)
            return
        logger.info("Incoming /ws/events connection accepted for service_id=%s", service.id)
        await record_service_trace(
            service_account_id=service.id,
            direction="incoming",
            local_transport="websocket",
            event_type="service.ws.connect",
            target="/ws/events",
            payload={
                "ws_token_present": bool(token_value),
                "auth_mode": "ws_token",
                "client_ip": client_ip,
            },
        )
        await event_hub.connect(service.id, websocket)
        try:
            while True:
                # Keepalive for proxies; inbound messages are ignored for now.
                await websocket.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            await event_hub.disconnect(service.id, websocket)
            await record_service_trace(
                service_account_id=service.id,
                direction="incoming",
                local_transport="websocket",
                event_type="service.ws.disconnect",
                target="/ws/events",
                payload={"client_ip": client_ip},
            )

    @app.get("/socket.io")
    @app.get("/socket.io/")
    async def socketio_http_mismatch() -> PlainTextResponse:
        return PlainTextResponse(SOCKET_IO_MISMATCH_MESSAGE, status_code=426)

    @app.websocket("/socket.io")
    @app.websocket("/socket.io/")
    async def socketio_ws_mismatch(websocket: WebSocket):
        client_ip = resolve_client_ip(
            websocket.client.host if websocket.client else None,
            websocket.headers.get("x-forwarded-for"),
            trust_x_forwarded_for=settings.app_trust_x_forwarded_for,
        )
        if not is_ip_allowed(client_ip):
            await websocket.close(code=4403)
            return
        await websocket.accept()
        await websocket.send_text(SOCKET_IO_MISMATCH_MESSAGE)
        await websocket.close(code=4400)

    @app.websocket("/{full_path:path}")
    async def websocket_path_mismatch(websocket: WebSocket, full_path: str):
        if full_path == "ws/events":
            await websocket.close(code=4404)
            return
        client_ip = resolve_client_ip(
            websocket.client.host if websocket.client else None,
            websocket.headers.get("x-forwarded-for"),
            trust_x_forwarded_for=settings.app_trust_x_forwarded_for,
        )
        if not is_ip_allowed(client_ip):
            await websocket.close(code=4403)
            return
        await websocket.accept()
        await websocket.send_text(WS_ENDPOINT_MISMATCH_MESSAGE)
        await websocket.close(code=4404)

