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
from app.bot_auth import ensure_bot_access_token
from app.config import RuntimeState, load_settings
from app.db import create_engine_and_session
from app.event_router import InterestRegistry, LocalEventHub
from app.eventsub_manager import EventSubManager
from app.models import Base, BotAccount, ChannelState, OAuthCallback, ServiceAccount, ServiceInterest
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
    webhook_event_types={
        x.strip()
        for x in settings.twitch_eventsub_webhook_event_types.split(",")
        if x.strip()
    },
    webhook_callback_url=settings.twitch_eventsub_webhook_callback_url,
    webhook_secret=settings.twitch_eventsub_webhook_secret,
)
runtime_state = RuntimeState(settings=settings)
DEFAULT_CHANNEL_EVENTS = ("channel.online", "channel.offline")


async def _require_admin(x_admin_key: str = Header(default="")) -> None:
    if x_admin_key != settings.admin_api_key:
        raise HTTPException(status_code=401, detail="Invalid admin key")


async def _service_auth(
    x_client_id: str = Header(default=""),
    x_client_secret: str = Header(default=""),
) -> ServiceAccount:
    async with session_factory() as session:
        return await authenticate_service(session, x_client_id, x_client_secret)


def _split_csv(values: str | None) -> list[str]:
    if not values:
        return []
    return [v.strip() for v in values.split(",") if v.strip()]


async def _ensure_default_stream_interests(
    service: ServiceAccount,
    bot_account_id: uuid.UUID,
    broadcaster_user_id: str,
) -> list[ServiceInterest]:
    created: list[ServiceInterest] = []
    async with session_factory() as session:
        for event_type in DEFAULT_CHANNEL_EVENTS:
            existing = await session.scalar(
                select(ServiceInterest).where(
                    ServiceInterest.service_account_id == service.id,
                    ServiceInterest.bot_account_id == bot_account_id,
                    ServiceInterest.event_type == event_type,
                    ServiceInterest.broadcaster_user_id == broadcaster_user_id,
                )
            )
            if existing:
                continue
            interest = ServiceInterest(
                service_account_id=service.id,
                bot_account_id=bot_account_id,
                event_type=event_type,
                broadcaster_user_id=broadcaster_user_id,
                transport="websocket",
                webhook_url=None,
            )
            session.add(interest)
            created.append(interest)
        await session.commit()
        for interest in created:
            await session.refresh(interest)
    return created


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


@app.get("/oauth/callback")
async def oauth_callback(code: str | None = None, state: str | None = None, error: str | None = None):
    if state:
        async with session_factory() as session:
            callback = await session.get(OAuthCallback, state)
            if callback is None:
                callback = OAuthCallback(state=state)
                session.add(callback)
            callback.code = code
            callback.error = error
            await session.commit()

    if error:
        return {
            "ok": False,
            "error": error,
            "message": "OAuth authorization returned an error.",
        }
    if not code:
        raise HTTPException(status_code=400, detail="Missing OAuth code")
    return {
        "ok": True,
        "message": "OAuth callback received. You can return to CLI and continue setup.",
        "code_received": True,
        "state_received": bool(state),
    }


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
    for default_interest in await _ensure_default_stream_interests(
        service=service,
        bot_account_id=req.bot_account_id,
        broadcaster_user_id=req.broadcaster_user_id,
    ):
        default_key = await interest_registry.add(default_interest)
        await eventsub_manager.on_interest_added(default_key)
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


@app.post("/v1/interests/{interest_id}/heartbeat")
async def heartbeat_interest(interest_id: uuid.UUID, service: ServiceAccount = Depends(_service_auth)):
    async with session_factory() as session:
        interest = await session.get(ServiceInterest, interest_id)
        if not interest or interest.service_account_id != service.id:
            raise HTTPException(status_code=404, detail="Interest not found")
        touch_targets = list(
            (
                await session.scalars(
                    select(ServiceInterest).where(
                        ServiceInterest.service_account_id == service.id,
                        ServiceInterest.bot_account_id == interest.bot_account_id,
                        ServiceInterest.broadcaster_user_id == interest.broadcaster_user_id,
                    )
                )
            ).all()
        )
        now = datetime.now(UTC)
        for target in touch_targets:
            target.updated_at = now
        await session.commit()
    return {"ok": True, "touched": len(touch_targets)}


@app.get("/v1/twitch/profiles")
async def twitch_profiles(
    bot_account_id: uuid.UUID,
    user_ids: str | None = None,
    logins: str | None = None,
    _: ServiceAccount = Depends(_service_auth),
):
    ids = _split_csv(user_ids)
    login_values = _split_csv(logins)
    if not ids and not login_values:
        raise HTTPException(status_code=422, detail="Provide user_ids and/or logins")
    if len(ids) + len(login_values) > 100:
        raise HTTPException(status_code=422, detail="At most 100 ids/logins per request")

    async with session_factory() as session:
        bot = await session.get(BotAccount, bot_account_id)
        if not bot:
            raise HTTPException(status_code=404, detail="Bot not found")
        if not bot.enabled:
            raise HTTPException(status_code=409, detail="Bot is disabled")
        token = await ensure_bot_access_token(session, twitch_client, bot)
        users = await twitch_client.get_users_by_query(token, user_ids=ids, logins=login_values)
    return {"data": users}


@app.get("/v1/twitch/streams/status")
async def twitch_stream_status(
    bot_account_id: uuid.UUID,
    broadcaster_user_ids: str,
    _: ServiceAccount = Depends(_service_auth),
):
    ids = _split_csv(broadcaster_user_ids)
    if not ids:
        raise HTTPException(status_code=422, detail="Provide broadcaster_user_ids")
    if len(ids) > 100:
        raise HTTPException(status_code=422, detail="At most 100 broadcaster ids per request")

    async with session_factory() as session:
        bot = await session.get(BotAccount, bot_account_id)
        if not bot:
            raise HTTPException(status_code=404, detail="Bot not found")
        if not bot.enabled:
            raise HTTPException(status_code=409, detail="Bot is disabled")
        token = await ensure_bot_access_token(session, twitch_client, bot)
        streams = await twitch_client.get_streams_by_user_ids(token, ids)
        live_by_id = {stream.get("user_id"): stream for stream in streams}

        out = []
        now = datetime.now(UTC)
        for uid in ids:
            stream = live_by_id.get(uid)
            state = await session.scalar(
                select(ChannelState).where(
                    ChannelState.bot_account_id == bot_account_id,
                    ChannelState.broadcaster_user_id == uid,
                )
            )
            if not state:
                state = ChannelState(
                    bot_account_id=bot_account_id,
                    broadcaster_user_id=uid,
                    is_live=False,
                )
                session.add(state)
            if stream:
                state.is_live = True
                state.title = stream.get("title")
                state.game_name = stream.get("game_name")
                raw_started = stream.get("started_at")
                if raw_started:
                    try:
                        state.started_at = datetime.fromisoformat(raw_started.replace("Z", "+00:00"))
                    except ValueError:
                        state.started_at = None
                else:
                    state.started_at = None
            else:
                state.is_live = False
                state.title = None
                state.game_name = None
                state.started_at = None
            state.last_checked_at = now
            out.append(
                {
                    "broadcaster_user_id": uid,
                    "is_live": state.is_live,
                    "title": state.title,
                    "game_name": state.game_name,
                    "started_at": state.started_at.isoformat() if state.started_at else None,
                    "last_checked_at": state.last_checked_at.isoformat(),
                }
            )
        await session.commit()
    return {"data": out}


@app.get("/v1/twitch/streams/status/interested")
async def interested_stream_status(service: ServiceAccount = Depends(_service_auth)):
    async with session_factory() as session:
        interests = list(
            (
                await session.scalars(
                    select(ServiceInterest).where(ServiceInterest.service_account_id == service.id)
                )
            ).all()
        )
        pairs = {(i.bot_account_id, i.broadcaster_user_id) for i in interests}
        rows = []
        for bot_id, broadcaster_user_id in pairs:
            state = await session.scalar(
                select(ChannelState).where(
                    ChannelState.bot_account_id == bot_id,
                    ChannelState.broadcaster_user_id == broadcaster_user_id,
                )
            )
            if not state:
                rows.append(
                    {
                        "bot_account_id": str(bot_id),
                        "broadcaster_user_id": broadcaster_user_id,
                        "is_live": None,
                        "title": None,
                        "game_name": None,
                        "started_at": None,
                        "last_checked_at": None,
                    }
                )
                continue
            rows.append(
                {
                    "bot_account_id": str(state.bot_account_id),
                    "broadcaster_user_id": state.broadcaster_user_id,
                    "is_live": state.is_live,
                    "title": state.title,
                    "game_name": state.game_name,
                    "started_at": state.started_at.isoformat() if state.started_at else None,
                    "last_checked_at": state.last_checked_at.isoformat(),
                }
            )
    return {"data": rows}


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
