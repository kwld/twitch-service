from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import uvicorn
from fastapi import (
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.responses import PlainTextResponse, Response
from sqlalchemy import select

from app.auth import authenticate_service, generate_client_id, generate_client_secret, hash_secret
from app.config import RuntimeState, load_settings
from app.db import create_engine_and_session
from app.event_router import InterestRegistry, LocalEventHub
from app.eventsub_manager import EventSubManager
from app.models import Base, BotAccount, ServiceAccount, ServiceInterest
from app.schemas import CreateInterestRequest, InterestResponse
from app.twitch import TwitchClient

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("twitch-eventsub-service")


settings = load_settings()
engine, session_factory = create_engine_and_session(settings)
twitch_client = TwitchClient(
    client_id=settings.twitch_client_id,
    client_secret=settings.twitch_client_secret,
    redirect_uri=settings.twitch_redirect_uri,
    scopes=settings.twitch_scopes,
    eventsub_ws_url=settings.twitch_eventsub_ws_url,
)
interest_registry = InterestRegistry()
event_hub = LocalEventHub()
eventsub_manager = EventSubManager(
    twitch_client,
    session_factory,
    interest_registry,
    event_hub,
    upstream_transport=settings.twitch_eventsub_transport,
    webhook_callback_url=settings.twitch_eventsub_webhook_callback_url,
    webhook_secret=settings.twitch_eventsub_webhook_secret,
)
runtime_state = RuntimeState(settings=settings)


async def _require_admin(x_admin_key: str = Header(default="")) -> None:
    if x_admin_key != settings.admin_api_key:
        raise HTTPException(status_code=401, detail="Invalid admin key")


async def _service_auth(
    x_client_id: str = Header(default=""),
    x_client_secret: str = Header(default=""),
) -> ServiceAccount:
    async with session_factory() as session:
        return await authenticate_service(session, x_client_id, x_client_secret)


@asynccontextmanager
async def lifespan(_: FastAPI):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await eventsub_manager.start()
    try:
        yield
    finally:
        await eventsub_manager.stop()
        await engine.dispose()


app = FastAPI(title="Twitch EventSub Service", lifespan=lifespan)


def _verify_twitch_signature(request: Request, raw_body: bytes) -> bool:
    message_id = request.headers.get("Twitch-Eventsub-Message-Id", "")
    message_timestamp = request.headers.get("Twitch-Eventsub-Message-Timestamp", "")
    message_signature = request.headers.get("Twitch-Eventsub-Message-Signature", "")
    if not message_id or not message_timestamp or not message_signature:
        return False

    try:
        ts = datetime.fromisoformat(message_timestamp.replace("Z", "+00:00"))
    except ValueError:
        return False
    if abs((datetime.now(UTC) - ts).total_seconds()) > timedelta(minutes=10).total_seconds():
        return False

    signed = message_id.encode("utf-8") + message_timestamp.encode("utf-8") + raw_body
    digest = hmac.new(
        settings.twitch_eventsub_webhook_secret.encode("utf-8"),
        signed,
        hashlib.sha256,
    ).hexdigest()
    expected = f"sha256={digest}"
    return hmac.compare_digest(expected, message_signature)


@app.get("/health")
async def health():
    return {"ok": True}


@app.post("/webhooks/twitch/eventsub")
async def twitch_eventsub_webhook(request: Request):
    raw_body = await request.body()
    if not _verify_twitch_signature(request, raw_body):
        raise HTTPException(status_code=403, detail="Invalid Twitch signature")

    message_type = request.headers.get("Twitch-Eventsub-Message-Type", "").lower()
    payload = await request.json()

    if message_type == "webhook_callback_verification":
        challenge = payload.get("challenge", "")
        return PlainTextResponse(content=challenge, status_code=200)

    if message_type == "notification":
        asyncio.create_task(
            eventsub_manager.handle_webhook_notification(
                payload, request.headers.get("Twitch-Eventsub-Message-Id", "")
            )
        )
        return Response(status_code=204)

    if message_type == "revocation":
        asyncio.create_task(eventsub_manager.handle_webhook_revocation(payload))
        return Response(status_code=204)

    return Response(status_code=204)


@app.get("/v1/bots")
async def list_bots(_: None = Depends(_require_admin)):
    async with session_factory() as session:
        bots = list((await session.scalars(select(BotAccount))).all())
    return [
        {
            "id": str(bot.id),
            "name": bot.name,
            "twitch_user_id": bot.twitch_user_id,
            "twitch_login": bot.twitch_login,
            "enabled": bot.enabled,
            "token_expires_at": bot.token_expires_at.isoformat(),
        }
        for bot in bots
    ]


@app.post("/v1/admin/service-accounts")
async def create_service_account(
    name: str,
    _: None = Depends(_require_admin),
):
    client_id = generate_client_id()
    client_secret = generate_client_secret()
    async with session_factory() as session:
        account = ServiceAccount(
            name=name,
            client_id=client_id,
            client_secret_hash=hash_secret(client_secret),
        )
        session.add(account)
        await session.commit()
    return {"name": name, "client_id": client_id, "client_secret": client_secret}


@app.get("/v1/admin/service-accounts")
async def list_service_accounts(_: None = Depends(_require_admin)):
    async with session_factory() as session:
        accounts = list((await session.scalars(select(ServiceAccount))).all())
    return [
        {
            "name": acc.name,
            "client_id": acc.client_id,
            "enabled": acc.enabled,
            "created_at": acc.created_at.isoformat(),
        }
        for acc in accounts
    ]


@app.post("/v1/admin/service-accounts/{client_id}/regenerate")
async def regenerate_service_secret(client_id: str, _: None = Depends(_require_admin)):
    new_secret = generate_client_secret()
    async with session_factory() as session:
        account = await session.scalar(select(ServiceAccount).where(ServiceAccount.client_id == client_id))
        if not account:
            raise HTTPException(status_code=404, detail="Service account not found")
        account.client_secret_hash = hash_secret(new_secret)
        await session.commit()
    return {"client_id": client_id, "client_secret": new_secret}


@app.get("/v1/interests", response_model=list[InterestResponse])
async def list_interests(service: ServiceAccount = Depends(_service_auth)):
    async with session_factory() as session:
        interests = list(
            (
                await session.scalars(
                    select(ServiceInterest).where(ServiceInterest.service_account_id == service.id)
                )
            ).all()
        )
    return interests


@app.post("/v1/interests", response_model=InterestResponse)
async def create_interest(
    req: CreateInterestRequest,
    service: ServiceAccount = Depends(_service_auth),
):
    if req.transport == "webhook" and not req.webhook_url:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="webhook_url is required for webhook transport",
        )
    async with session_factory() as session:
        bot = await session.get(BotAccount, req.bot_account_id)
        if not bot:
            raise HTTPException(status_code=404, detail="Bot not found")
        interest = ServiceInterest(
            service_account_id=service.id,
            bot_account_id=req.bot_account_id,
            event_type=req.event_type,
            broadcaster_user_id=req.broadcaster_user_id,
            transport=req.transport,
            webhook_url=str(req.webhook_url) if req.webhook_url else None,
        )
        session.add(interest)
        await session.commit()
        await session.refresh(interest)
    key = await interest_registry.add(interest)
    await eventsub_manager.on_interest_added(key)
    return interest


@app.delete("/v1/interests/{interest_id}")
async def delete_interest(interest_id: uuid.UUID, service: ServiceAccount = Depends(_service_auth)):
    async with session_factory() as session:
        interest = await session.get(ServiceInterest, interest_id)
        if not interest or interest.service_account_id != service.id:
            raise HTTPException(status_code=404, detail="Interest not found")
        await session.delete(interest)
        await session.commit()
    key, still_used = await interest_registry.remove(interest)
    await eventsub_manager.on_interest_removed(key, still_used)
    return {"deleted": True}


@app.websocket("/ws/events")
async def ws_events(websocket: WebSocket, client_id: str = Query(), client_secret: str = Query()):
    try:
        async with session_factory() as session:
            service = await authenticate_service(session, client_id, client_secret)
    except HTTPException:
        await websocket.close(code=4401)
        return
    await event_hub.connect(service.id, websocket)
    try:
        while True:
            # Keepalive for proxies; inbound messages are ignored for now.
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        await event_hub.disconnect(service.id, websocket)


def run() -> None:
    uvicorn.run(
        "app.main:app",
        host=settings.app_host,
        port=settings.app_port,
        reload=False,
        log_level=settings.app_log_level,
    )
