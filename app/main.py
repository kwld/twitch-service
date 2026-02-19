from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import secrets
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlunsplit

import uvicorn
from fastapi import (
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Request,
    status,
)
from fastapi.responses import Response
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.auth import authenticate_service
from app.bot_auth import ensure_bot_access_token
from app.config import RuntimeState, load_settings
from app.core.network_security import (
    WebhookTargetValidator,
    is_ip_allowed,
    parse_allowed_ip_networks,
    parse_webhook_target_allowlist,
    resolve_client_ip,
)
from app.core.normalization import normalize_broadcaster_id_or_login
from app.core.redaction import redact_payload
from app.core.runtime_tokens import EventSubMessageDeduper, WsTokenStore
from app.db import create_engine_and_session
from app.event_router import InterestRegistry, LocalEventHub
from app.eventsub_manager import EventSubManager
from app.models import (
    Base,
    BotAccount,
    ChannelState,
    ServiceAccount,
    ServiceBotAccess,
    ServiceEventTrace,
    ServiceInterest,
    ServiceRuntimeStats,
    TwitchSubscription,
)
from app.routes import (
    register_admin_routes,
    register_service_routes,
    register_system_routes,
    register_twitch_routes,
    register_ws_routes,
)
from app.twitch import TwitchClient
from app.twitch_chat_assets import TwitchChatAssetCache

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("twitch-eventsub-service")
eventsub_audit_logger = logging.getLogger("eventsub.audit")
TWITCH_WEBHOOK_PATH = "/webhooks/twitch/eventsub"


settings = load_settings()
_eventsub_log_path = Path(settings.app_eventsub_log_path)
_eventsub_log_path.parent.mkdir(parents=True, exist_ok=True)
if not any(isinstance(h, logging.FileHandler) for h in eventsub_audit_logger.handlers):
    _eventsub_audit_file_handler = logging.FileHandler(_eventsub_log_path, encoding="utf-8")
    _eventsub_audit_file_handler.setFormatter(logging.Formatter("%(message)s"))
    eventsub_audit_logger.addHandler(_eventsub_audit_file_handler)
eventsub_audit_logger.setLevel(logging.INFO)
eventsub_audit_logger.propagate = False

allowed_ip_networks = parse_allowed_ip_networks(settings.app_allowed_ips)
if allowed_ip_networks:
    logger.info("IP allowlist enabled with %d entries", len(allowed_ip_networks))
webhook_target_allowlist = parse_webhook_target_allowlist(settings.app_webhook_target_allowlist)
if webhook_target_allowlist:
    logger.info(
        "Webhook target allowlist enabled with %d host entries",
        len(webhook_target_allowlist),
    )
webhook_target_validator = WebhookTargetValidator(
    allowlist=webhook_target_allowlist,
    block_private_targets=settings.app_block_private_webhook_targets,
)
engine, session_factory = create_engine_and_session(settings)
twitch_client = TwitchClient(
    client_id=settings.twitch_client_id,
    client_secret=settings.twitch_client_secret,
    redirect_uri=settings.twitch_redirect_uri,
    scopes=settings.twitch_scopes,
    eventsub_ws_url=settings.twitch_eventsub_ws_url,
)
chat_assets = TwitchChatAssetCache(twitch_client)
interest_registry = InterestRegistry()
event_hub = LocalEventHub()
eventsub_manager = EventSubManager(
    twitch_client,
    session_factory,
    interest_registry,
    event_hub,
    chat_assets=chat_assets,
    webhook_event_types={
        x.strip()
        for x in settings.twitch_eventsub_webhook_event_types.split(",")
        if x.strip()
    },
    webhook_callback_url=settings.twitch_eventsub_webhook_callback_url,
    webhook_secret=settings.twitch_eventsub_webhook_secret,
)
runtime_state = RuntimeState(settings=settings)
DEFAULT_STREAM_EVENTS = ("stream.online", "stream.offline")
BROADCASTER_AUTH_SCOPES = ("channel:bot",)
SERVICE_USER_AUTH_SCOPES = ("user:read:email",)
WS_TOKEN_TTL = timedelta(minutes=2)
EVENTSUB_MESSAGE_DEDUP_TTL = timedelta(minutes=10)
ws_token_store = WsTokenStore(ttl=WS_TOKEN_TTL)
eventsub_message_deduper = EventSubMessageDeduper(ttl=EVENTSUB_MESSAGE_DEDUP_TTL)


def _counter(value: int | None) -> int:
    return value or 0


def _request_client_ip(request: Request) -> str | None:
    direct_host = request.client.host if request.client else None
    return resolve_client_ip(
        direct_host,
        request.headers.get("x-forwarded-for"),
        trust_x_forwarded_for=settings.app_trust_x_forwarded_for,
    )


async def _issue_ws_token(service_account_id: uuid.UUID) -> tuple[str, int]:
    return await ws_token_store.issue(service_account_id)


async def _consume_ws_token(token: str) -> uuid.UUID | None:
    return await ws_token_store.consume(token)


async def _is_new_eventsub_message_id(message_id: str) -> bool:
    return await eventsub_message_deduper.is_new(message_id)


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
        stats.total_api_requests = _counter(stats.total_api_requests) + 1
        stats.last_api_request_at = now
        await session.commit()
        return service


async def _update_runtime_stats(service_account_id: uuid.UUID, mutator) -> None:
    async with session_factory() as session:
        service_exists = await session.scalar(
            select(ServiceAccount.id).where(ServiceAccount.id == service_account_id)
        )
        if not service_exists:
            logger.info("Skipping runtime stats update for missing service account %s", service_account_id)
            return
        stats = await session.get(ServiceRuntimeStats, service_account_id)
        if not stats:
            stats = ServiceRuntimeStats(service_account_id=service_account_id)
            session.add(stats)
        mutator(stats)
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            logger.warning(
                "Runtime stats update failed due to FK race for service account %s",
                service_account_id,
            )


async def _on_service_connect(service_account_id: uuid.UUID) -> None:
    now = datetime.now(UTC)

    def _mutator(stats: ServiceRuntimeStats) -> None:
        stats.active_ws_connections = _counter(stats.active_ws_connections) + 1
        stats.total_ws_connects = _counter(stats.total_ws_connects) + 1
        stats.is_connected = stats.active_ws_connections > 0
        stats.last_connected_at = now

    await _update_runtime_stats(service_account_id, _mutator)
    active = await event_hub.active_connections(service_account_id)
    logger.info("Service websocket connected: service_id=%s active_connections=%d", service_account_id, active)


async def _on_service_disconnect(service_account_id: uuid.UUID) -> None:
    now = datetime.now(UTC)

    def _mutator(stats: ServiceRuntimeStats) -> None:
        stats.active_ws_connections = max(0, _counter(stats.active_ws_connections) - 1)
        stats.is_connected = stats.active_ws_connections > 0
        stats.last_disconnected_at = now

    await _update_runtime_stats(service_account_id, _mutator)
    active = await event_hub.active_connections(service_account_id)
    logger.info("Service websocket disconnected: service_id=%s active_connections=%d", service_account_id, active)


async def _on_service_ws_event(service_account_id: uuid.UUID) -> None:
    now = datetime.now(UTC)

    def _mutator(stats: ServiceRuntimeStats) -> None:
        stats.total_events_sent_ws = _counter(stats.total_events_sent_ws) + 1
        stats.last_event_sent_at = now

    await _update_runtime_stats(service_account_id, _mutator)


async def _on_service_webhook_event(service_account_id: uuid.UUID) -> None:
    now = datetime.now(UTC)

    def _mutator(stats: ServiceRuntimeStats) -> None:
        stats.total_events_sent_webhook = _counter(stats.total_events_sent_webhook) + 1
        stats.last_event_sent_at = now

    await _update_runtime_stats(service_account_id, _mutator)


def _split_csv(values: str | None) -> list[str]:
    if not values:
        return []
    return [v.strip() for v in values.split(",") if v.strip()]


def _append_query(url: str, params: dict[str, str]) -> str:
    split = urlsplit(url)
    query = dict(parse_qsl(split.query, keep_blank_values=True))
    query.update(params)
    return urlunsplit(
        (split.scheme, split.netloc, split.path, urlencode(query), split.fragment)
    )


async def _record_service_trace(
    service_account_id: uuid.UUID,
    direction: str,
    local_transport: str,
    event_type: str,
    target: str | None,
    payload: object,
) -> None:
    try:
        payload_json = json.dumps(redact_payload(payload), default=str)
        if len(payload_json) > 12000:
            payload_json = payload_json[:12000] + "... [truncated]"
        async with session_factory() as session:
            service = await session.get(ServiceAccount, service_account_id)
            if not service:
                return
            session.add(
                ServiceEventTrace(
                    service_account_id=service_account_id,
                    direction=direction,
                    local_transport=local_transport,
                    event_type=event_type,
                    target=target,
                    payload_json=payload_json,
                )
            )
            await session.commit()
    except Exception:
        return


async def _filter_working_interests(session, interests: list[ServiceInterest]) -> list[ServiceInterest]:
    if not interests:
        return []
    active_subs = list(
        (
            await session.scalars(
                select(TwitchSubscription).where(TwitchSubscription.status.startswith("enabled"))
            )
        ).all()
    )
    active_keys = {
        (row.bot_account_id, row.event_type, row.broadcaster_user_id)
        for row in active_subs
    }
    return [
        interest
        for interest in interests
        if (interest.bot_account_id, interest.event_type, interest.broadcaster_user_id) in active_keys
    ]


async def _service_allowed_bot_ids(session, service_account_id: uuid.UUID) -> set[uuid.UUID]:
    rows = list(
        (
            await session.scalars(
                select(ServiceBotAccess.bot_account_id).where(
                    ServiceBotAccess.service_account_id == service_account_id
                )
            )
        ).all()
    )
    return set(rows)


async def _ensure_service_can_access_bot(
    session,
    service_account_id: uuid.UUID,
    bot_account_id: uuid.UUID,
) -> None:
    allowed_ids = await _service_allowed_bot_ids(session, service_account_id)
    if allowed_ids and bot_account_id not in allowed_ids:
        raise HTTPException(
            status_code=403,
            detail="Service is not allowed to access this bot account",
        )


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
    now = datetime.now(UTC)
    async with session_factory() as session:
        for event_type in DEFAULT_STREAM_EVENTS:
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
                last_heartbeat_at=now,
            )
            session.add(interest)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                continue
            await session.refresh(interest)
            created.append(interest)
    return created


@asynccontextmanager
async def lifespan(_: FastAPI):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Legacy compatibility: older builds stored invalid EventSub types.
        await conn.execute(
            text(
                "UPDATE service_interests SET event_type = 'stream.online' "
                "WHERE event_type = 'channel.online'"
            )
        )
        await conn.execute(
            text(
                "UPDATE service_interests SET event_type = 'stream.offline' "
                "WHERE event_type = 'channel.offline'"
            )
        )
        await conn.execute(
            text(
                "UPDATE twitch_subscriptions SET event_type = 'stream.online' "
                "WHERE event_type = 'channel.online'"
            )
        )
        await conn.execute(
            text(
                "UPDATE twitch_subscriptions SET event_type = 'stream.offline' "
                "WHERE event_type = 'channel.offline'"
            )
        )
        try:
            await conn.execute(
                text(
                    "ALTER TABLE broadcaster_authorization_requests "
                    "ADD COLUMN IF NOT EXISTS redirect_url TEXT"
                )
            )
        except Exception as exc:
            logger.warning("Skipping redirect_url compatibility migration: %s", exc)
        try:
            await conn.execute(
                text(
                    "ALTER TABLE service_interests "
                    "ADD COLUMN IF NOT EXISTS last_heartbeat_at TIMESTAMPTZ"
                )
            )
            await conn.execute(
                text(
                    "ALTER TABLE service_interests "
                    "ADD COLUMN IF NOT EXISTS stale_marked_at TIMESTAMPTZ"
                )
            )
            await conn.execute(
                text(
                    "ALTER TABLE service_interests "
                    "ADD COLUMN IF NOT EXISTS delete_after TIMESTAMPTZ"
                )
            )
            await conn.execute(
                text(
                    "UPDATE service_interests "
                    "SET last_heartbeat_at = COALESCE(last_heartbeat_at, updated_at)"
                )
            )
        except Exception as exc:
            logger.warning("Skipping service_interests lease compatibility migration: %s", exc)
    async with session_factory() as session:
        await session.execute(
            text(
                "UPDATE service_runtime_stats "
                "SET active_ws_connections = 0, is_connected = false"
            )
        )
        await session.commit()
    await eventsub_manager.start()
    try:
        yield
    finally:
        await eventsub_manager.stop()
        await event_hub.close()
        await twitch_client.close()
        await engine.dispose()


app = FastAPI(
    title="Twitch EventSub Service",
    lifespan=lifespan,
    docs_url="/api-docs",
    openapi_url="/api-docs/openapi.json",
    redoc_url="/api-redoc",
)


@app.middleware("http")
async def enforce_ip_allowlist(request: Request, call_next):
    if request.url.path == TWITCH_WEBHOOK_PATH:
        return await call_next(request)
    if not allowed_ip_networks:
        return await call_next(request)
    client_ip = _request_client_ip(request)
    if not is_ip_allowed(client_ip, allowed_ip_networks):
        logger.warning("Blocked HTTP request from IP %s to %s", client_ip or "unknown", request.url.path)
        return Response(status_code=403, content="Client IP not allowed")
    return await call_next(request)


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


register_system_routes(
    app,
    session_factory=session_factory,
    twitch_client=twitch_client,
    eventsub_manager=eventsub_manager,
    append_query=_append_query,
    verify_twitch_signature=_verify_twitch_signature,
    is_new_eventsub_message_id=_is_new_eventsub_message_id,
    broadcaster_auth_scopes=BROADCASTER_AUTH_SCOPES,
    service_user_auth_scopes=SERVICE_USER_AUTH_SCOPES,
)

register_admin_routes(
    app,
    session_factory=session_factory,
    require_admin=_require_admin,
    service_auth=_service_auth,
    service_allowed_bot_ids=_service_allowed_bot_ids,
)
register_service_routes(
    app,
    session_factory=session_factory,
    twitch_client=twitch_client,
    eventsub_manager=eventsub_manager,
    service_auth=_service_auth,
    interest_registry=interest_registry,
    logger=logger,
    issue_ws_token=_issue_ws_token,
    record_service_trace=_record_service_trace,
    split_csv=_split_csv,
    filter_working_interests=_filter_working_interests,
    service_allowed_bot_ids=_service_allowed_bot_ids,
    ensure_service_can_access_bot=_ensure_service_can_access_bot,
    ensure_default_stream_interests=_ensure_default_stream_interests,
    validate_webhook_target_url=webhook_target_validator.validate,
    normalize_broadcaster_id_or_login=normalize_broadcaster_id_or_login,
    broadcaster_auth_scopes=BROADCASTER_AUTH_SCOPES,
    service_user_auth_scopes=SERVICE_USER_AUTH_SCOPES,
)
register_twitch_routes(
    app,
    session_factory=session_factory,
    twitch_client=twitch_client,
    chat_assets=chat_assets,
    service_auth=_service_auth,
    split_csv=_split_csv,
    ensure_service_can_access_bot=_ensure_service_can_access_bot,
    normalize_broadcaster_id_or_login=normalize_broadcaster_id_or_login,
)
register_ws_routes(
    app,
    settings=settings,
    logger=logger,
    session_factory=session_factory,
    consume_ws_token=_consume_ws_token,
    record_service_trace=_record_service_trace,
    event_hub=event_hub,
    resolve_client_ip=resolve_client_ip,
    is_ip_allowed=lambda client_ip: is_ip_allowed(client_ip, allowed_ip_networks),
)


def run() -> None:
    uvicorn.run(
        "app.main:app",
        host=settings.app_host,
        port=settings.app_port,
        reload=False,
        log_level=settings.app_log_level,
    )

