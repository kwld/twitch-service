from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import secrets
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
from app.eventsub_catalog import (
    EVENTSUB_CATALOG,
    KNOWN_EVENT_TYPES,
    SOURCE_SNAPSHOT_DATE,
    SOURCE_URL,
    best_transport_for_service,
)
from app.event_router import InterestRegistry, LocalEventHub
from app.eventsub_manager import EventSubManager
from app.models import (
    Base,
    BotAccount,
    BroadcasterAuthorization,
    BroadcasterAuthorizationRequest,
    ChannelState,
    OAuthCallback,
    ServiceAccount,
    ServiceInterest,
    ServiceRuntimeStats,
    TwitchSubscription,
)
from app.schemas import (
    BroadcasterAuthorizationResponse,
    CreateInterestRequest,
    EventSubCatalogItem,
    EventSubCatalogResponse,
    InterestResponse,
    SendChatMessageRequest,
    SendChatMessageResponse,
    StartBroadcasterAuthorizationRequest,
    StartBroadcasterAuthorizationResponse,
)
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
BROADCASTER_AUTH_SCOPES = ("channel:bot",)


async def _require_admin(x_admin_key: str = Header(default="")) -> None:
    if x_admin_key != settings.admin_api_key:
        raise HTTPException(status_code=401, detail="Invalid admin key")


async def _service_auth(
    x_client_id: str = Header(default=""),
    x_client_secret: str = Header(default=""),
) -> ServiceAccount:
    async with session_factory() as session:
        service = await authenticate_service(session, x_client_id, x_client_secret)
        stats = await session.get(ServiceRuntimeStats, service.id)
        now = datetime.now(UTC)
        if not stats:
            stats = ServiceRuntimeStats(service_account_id=service.id)
            session.add(stats)
        stats.total_api_requests += 1
        stats.last_api_request_at = now
        await session.commit()
        return service


async def _update_runtime_stats(service_account_id: uuid.UUID, mutator) -> None:
    async with session_factory() as session:
        stats = await session.get(ServiceRuntimeStats, service_account_id)
        if not stats:
            stats = ServiceRuntimeStats(service_account_id=service_account_id)
            session.add(stats)
        mutator(stats)
        await session.commit()


async def _on_service_connect(service_account_id: uuid.UUID) -> None:
    now = datetime.now(UTC)

    def _mutator(stats: ServiceRuntimeStats) -> None:
        stats.active_ws_connections += 1
        stats.total_ws_connects += 1
        stats.is_connected = stats.active_ws_connections > 0
        stats.last_connected_at = now

    await _update_runtime_stats(service_account_id, _mutator)


async def _on_service_disconnect(service_account_id: uuid.UUID) -> None:
    now = datetime.now(UTC)

    def _mutator(stats: ServiceRuntimeStats) -> None:
        stats.active_ws_connections = max(0, stats.active_ws_connections - 1)
        stats.is_connected = stats.active_ws_connections > 0
        stats.last_disconnected_at = now

    await _update_runtime_stats(service_account_id, _mutator)


async def _on_service_ws_event(service_account_id: uuid.UUID) -> None:
    now = datetime.now(UTC)

    def _mutator(stats: ServiceRuntimeStats) -> None:
        stats.total_events_sent_ws += 1
        stats.last_event_sent_at = now

    await _update_runtime_stats(service_account_id, _mutator)


async def _on_service_webhook_event(service_account_id: uuid.UUID) -> None:
    now = datetime.now(UTC)

    def _mutator(stats: ServiceRuntimeStats) -> None:
        stats.total_events_sent_webhook += 1
        stats.last_event_sent_at = now

    await _update_runtime_stats(service_account_id, _mutator)


def _split_csv(values: str | None) -> list[str]:
    if not values:
        return []
    return [v.strip() for v in values.split(",") if v.strip()]


event_hub.on_service_connect = _on_service_connect
event_hub.on_service_disconnect = _on_service_disconnect
event_hub.on_service_ws_event = _on_service_ws_event
event_hub.on_service_webhook_event = _on_service_webhook_event


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
            auth_request = await session.get(BroadcasterAuthorizationRequest, state)
            if auth_request:
                now = datetime.now(UTC)
                if error:
                    auth_request.status = "failed"
                    auth_request.error = error
                    auth_request.completed_at = now
                    await session.commit()
                    return {
                        "ok": False,
                        "error": error,
                        "message": "Broadcaster authorization failed.",
                    }
                if not code:
                    auth_request.status = "failed"
                    auth_request.error = "missing_code"
                    auth_request.completed_at = now
                    await session.commit()
                    raise HTTPException(status_code=400, detail="Missing OAuth code")

                try:
                    token = await twitch_client.exchange_code(code)
                    token_info = await twitch_client.validate_user_token(token.access_token)
                except Exception as exc:
                    auth_request.status = "failed"
                    auth_request.error = str(exc)
                    auth_request.completed_at = now
                    await session.commit()
                    raise HTTPException(status_code=502, detail=f"OAuth exchange failed: {exc}") from exc

                granted_scopes = sorted(set(token_info.get("scopes", [])))
                required = set(BROADCASTER_AUTH_SCOPES)
                if not required.issubset(set(granted_scopes)):
                    auth_request.status = "failed"
                    auth_request.error = (
                        "missing_required_scopes:"
                        + ",".join(sorted(required - set(granted_scopes)))
                    )
                    auth_request.completed_at = now
                    await session.commit()
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            "Broadcaster authorization succeeded but required scopes are missing: "
                            + ", ".join(sorted(required - set(granted_scopes)))
                        ),
                    )

                broadcaster_user_id = str(token_info.get("user_id", ""))
                broadcaster_login = str(token_info.get("login", ""))
                if not broadcaster_user_id or not broadcaster_login:
                    auth_request.status = "failed"
                    auth_request.error = "missing_broadcaster_identity"
                    auth_request.completed_at = now
                    await session.commit()
                    raise HTTPException(status_code=400, detail="Could not resolve broadcaster identity")

                existing_auth = await session.scalar(
                    select(BroadcasterAuthorization).where(
                        BroadcasterAuthorization.service_account_id == auth_request.service_account_id,
                        BroadcasterAuthorization.bot_account_id == auth_request.bot_account_id,
                        BroadcasterAuthorization.broadcaster_user_id == broadcaster_user_id,
                    )
                )
                scopes_csv = ",".join(granted_scopes)
                if existing_auth:
                    existing_auth.broadcaster_login = broadcaster_login
                    existing_auth.scopes_csv = scopes_csv
                    existing_auth.authorized_at = now
                else:
                    session.add(
                        BroadcasterAuthorization(
                            service_account_id=auth_request.service_account_id,
                            bot_account_id=auth_request.bot_account_id,
                            broadcaster_user_id=broadcaster_user_id,
                            broadcaster_login=broadcaster_login,
                            scopes_csv=scopes_csv,
                            authorized_at=now,
                        )
                    )
                auth_request.status = "completed"
                auth_request.broadcaster_user_id = broadcaster_user_id
                auth_request.error = None
                auth_request.completed_at = now
                await session.commit()
                return {
                    "ok": True,
                    "message": "Broadcaster authorization completed.",
                    "service_connected": True,
                    "broadcaster_user_id": broadcaster_user_id,
                    "broadcaster_login": broadcaster_login,
                    "scopes": granted_scopes,
                }

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


@app.post(
    "/v1/broadcaster-authorizations/start",
    response_model=StartBroadcasterAuthorizationResponse,
)
async def start_broadcaster_authorization(
    req: StartBroadcasterAuthorizationRequest,
    service: ServiceAccount = Depends(_service_auth),
):
    async with session_factory() as session:
        bot = await session.get(BotAccount, req.bot_account_id)
        if not bot:
            raise HTTPException(status_code=404, detail="Bot not found")
        if not bot.enabled:
            raise HTTPException(status_code=409, detail="Bot is disabled")

        state = secrets.token_urlsafe(24)
        scopes_csv = ",".join(BROADCASTER_AUTH_SCOPES)
        session.add(
            BroadcasterAuthorizationRequest(
                state=state,
                service_account_id=service.id,
                bot_account_id=req.bot_account_id,
                requested_scopes_csv=scopes_csv,
                status="pending",
            )
        )
        await session.commit()

    scopes_str = " ".join(BROADCASTER_AUTH_SCOPES)
    authorize_url = twitch_client.build_authorize_url_with_scopes(
        state=state,
        scopes=scopes_str,
        force_verify=True,
    )
    return StartBroadcasterAuthorizationResponse(
        state=state,
        authorize_url=authorize_url,
        requested_scopes=list(BROADCASTER_AUTH_SCOPES),
        expires_in_seconds=600,
    )


@app.get(
    "/v1/broadcaster-authorizations",
    response_model=list[BroadcasterAuthorizationResponse],
)
async def list_broadcaster_authorizations(service: ServiceAccount = Depends(_service_auth)):
    async with session_factory() as session:
        items = list(
            (
                await session.scalars(
                    select(BroadcasterAuthorization).where(
                        BroadcasterAuthorization.service_account_id == service.id
                    )
                )
            ).all()
        )
    return [
        BroadcasterAuthorizationResponse(
            id=item.id,
            service_account_id=item.service_account_id,
            bot_account_id=item.bot_account_id,
            broadcaster_user_id=item.broadcaster_user_id,
            broadcaster_login=item.broadcaster_login,
            scopes=_split_csv(item.scopes_csv),
            authorized_at=item.authorized_at,
            updated_at=item.updated_at,
        )
        for item in items
    ]


@app.get("/v1/eventsub/subscription-types", response_model=EventSubCatalogResponse)
async def list_eventsub_subscription_types(_: ServiceAccount = Depends(_service_auth)):
    webhook_preferred: list[EventSubCatalogItem] = []
    websocket_preferred: list[EventSubCatalogItem] = []
    all_items: list[EventSubCatalogItem] = []

    for entry in EVENTSUB_CATALOG:
        best_transport, reason = best_transport_for_service(
            event_type=entry.event_type,
            webhook_event_types=eventsub_manager.webhook_event_types,
        )
        item = EventSubCatalogItem(
            title=entry.title,
            event_type=entry.event_type,
            version=entry.version,
            description=entry.description,
            status=entry.status,
            twitch_transports=["webhook", "websocket"],
            best_transport=best_transport,
            best_transport_reason=reason,
        )
        all_items.append(item)
        if best_transport == "webhook":
            webhook_preferred.append(item)
        else:
            websocket_preferred.append(item)

    return EventSubCatalogResponse(
        source_url=SOURCE_URL,
        source_snapshot_date=SOURCE_SNAPSHOT_DATE,
        total_items=len(all_items),
        total_unique_event_types=len(KNOWN_EVENT_TYPES),
        webhook_preferred=webhook_preferred,
        websocket_preferred=websocket_preferred,
        all_items=all_items,
    )


@app.post("/v1/interests", response_model=InterestResponse)
async def create_interest(
    req: CreateInterestRequest,
    service: ServiceAccount = Depends(_service_auth),
):
    event_type = req.event_type.strip().lower()
    broadcaster_user_id = req.broadcaster_user_id.strip()
    webhook_url = str(req.webhook_url) if req.webhook_url else None

    if req.transport == "webhook" and not req.webhook_url:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="webhook_url is required for webhook transport",
        )
    if event_type not in KNOWN_EVENT_TYPES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"Unsupported event_type '{req.event_type}'. "
                "See GET /v1/eventsub/subscription-types."
            ),
        )
    async with session_factory() as session:
        bot = await session.get(BotAccount, req.bot_account_id)
        if not bot:
            raise HTTPException(status_code=404, detail="Bot not found")
        existing = await session.scalar(
            select(ServiceInterest).where(
                ServiceInterest.service_account_id == service.id,
                ServiceInterest.bot_account_id == req.bot_account_id,
                ServiceInterest.event_type == event_type,
                ServiceInterest.broadcaster_user_id == broadcaster_user_id,
                ServiceInterest.transport == req.transport,
                ServiceInterest.webhook_url == webhook_url,
            )
        )
        if existing:
            interest = existing
        else:
            interest = ServiceInterest(
                service_account_id=service.id,
                bot_account_id=req.bot_account_id,
                event_type=event_type,
                broadcaster_user_id=broadcaster_user_id,
                transport=req.transport,
                webhook_url=webhook_url,
            )
            session.add(interest)
            await session.commit()
            await session.refresh(interest)

    key = await interest_registry.add(interest)
    await eventsub_manager.on_interest_added(key)
    for default_interest in await _ensure_default_stream_interests(
        service=service,
        bot_account_id=req.bot_account_id,
        broadcaster_user_id=broadcaster_user_id,
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


@app.post("/v1/twitch/chat/messages", response_model=SendChatMessageResponse)
async def send_twitch_chat_message(
    req: SendChatMessageRequest,
    _: ServiceAccount = Depends(_service_auth),
):
    broadcaster_user_id = req.broadcaster_user_id.strip()
    async with session_factory() as session:
        bot = await session.get(BotAccount, req.bot_account_id)
        if not bot:
            raise HTTPException(status_code=404, detail="Bot not found")
        if not bot.enabled:
            raise HTTPException(status_code=409, detail="Bot is disabled")
        token = await ensure_bot_access_token(session, twitch_client, bot)
        token_info = await twitch_client.validate_user_token(token)
        scopes = set(token_info.get("scopes", []))
        if "user:write:chat" not in scopes:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Bot token missing required scope 'user:write:chat'. "
                    "Re-run Guided bot setup to refresh OAuth scopes."
                ),
            )
        if req.auth_mode in {"auto", "app"} and "user:bot" not in scopes:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Bot token missing required scope 'user:bot' for app-token chat mode. "
                    "Re-run Guided bot setup to refresh OAuth scopes."
                ),
            )
        if str(token_info.get("user_id", "")) != bot.twitch_user_id:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Stored bot token does not belong to this bot account. "
                    "Re-run Guided bot setup and update the bot credentials."
                ),
            )

    async def _send_with_mode(mode: str) -> tuple[dict, str]:
        if mode == "app":
            app_token = await twitch_client.app_access_token()
            payload = await twitch_client.send_chat_message(
                access_token=app_token,
                broadcaster_id=broadcaster_user_id,
                sender_id=bot.twitch_user_id,
                message=req.message,
                reply_parent_message_id=req.reply_parent_message_id,
            )
            return payload, "app"
        payload = await twitch_client.send_chat_message(
            access_token=token,
            broadcaster_id=broadcaster_user_id,
            sender_id=bot.twitch_user_id,
            message=req.message,
            reply_parent_message_id=req.reply_parent_message_id,
        )
        return payload, "user"

    send_error: Exception | None = None
    result: dict | None = None
    auth_mode_used: str | None = None
    try:
        if req.auth_mode == "auto":
            try:
                result, auth_mode_used = await _send_with_mode("app")
            except Exception as app_exc:
                send_error = app_exc
                result, auth_mode_used = await _send_with_mode("user")
        else:
            result, auth_mode_used = await _send_with_mode(req.auth_mode)
    except Exception as exc:
        extra = ""
        if req.auth_mode == "auto" and send_error is not None:
            extra = f" (app-token attempt failed first: {send_error})"
        raise HTTPException(status_code=502, detail=f"{exc}{extra}") from exc

    assert result is not None
    assert auth_mode_used is not None
    bot_badge_eligible = auth_mode_used == "app" and broadcaster_user_id != bot.twitch_user_id
    if auth_mode_used != "app":
        bot_badge_reason = "User token used; Twitch bot badge requires app-token send path."
    elif broadcaster_user_id == bot.twitch_user_id:
        bot_badge_reason = "Bot is chatting in its own broadcaster channel; Twitch does not show bot badge here."
    else:
        bot_badge_reason = "App-token send path used; badge eligibility depends on channel authorization/mod status."

    drop_reason = result.get("drop_reason") or {}
    return SendChatMessageResponse(
        broadcaster_user_id=broadcaster_user_id,
        sender_user_id=bot.twitch_user_id,
        message_id=result.get("message_id", ""),
        is_sent=bool(result.get("is_sent", False)),
        auth_mode_used=auth_mode_used,
        bot_badge_eligible=bot_badge_eligible,
        bot_badge_reason=bot_badge_reason,
        drop_reason_code=drop_reason.get("code"),
        drop_reason_message=drop_reason.get("message"),
    )


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
