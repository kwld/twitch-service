from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import logging
import secrets
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

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
from fastapi.responses import PlainTextResponse, RedirectResponse, Response
from sqlalchemy import select, text

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
    ServiceBotAccess,
    ServiceInterest,
    ServiceRuntimeStats,
    ServiceUserAuthRequest,
    TwitchSubscription,
)
from app.schemas import (
    BroadcasterAuthorizationResponse,
    CreateClipRequest,
    CreateClipResponse,
    CreateInterestRequest,
    EventSubCatalogItem,
    EventSubCatalogResponse,
    InterestResponse,
    SendChatMessageRequest,
    SendChatMessageResponse,
    StartBroadcasterAuthorizationRequest,
    StartBroadcasterAuthorizationResponse,
    StartUserAuthorizationRequest,
    StartUserAuthorizationResponse,
    UserAuthorizationSessionResponse,
)
from app.twitch import TwitchClient

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("twitch-eventsub-service")
TWITCH_WEBHOOK_PATH = "/webhooks/twitch/eventsub"


def _normalize_broadcaster_id_or_login(raw: str) -> str:
    """
    Accept either a Twitch user id, a login, or a twitch.tv URL, and normalize to
    a single token (id/login) without surrounding punctuation.
    """
    value = (raw or "").strip()
    if not value:
        return ""
    if value.startswith(("http://", "https://")):
        try:
            split = urlsplit(value)
            host = (split.netloc or "").lower()
            if host.endswith("twitch.tv"):
                path = (split.path or "").strip("/")
                if path:
                    value = path.split("/", 1)[0]
        except Exception:
            pass
    value = value.strip().lstrip("@")
    if "/" in value:
        value = value.split("/", 1)[0]
    if "?" in value:
        value = value.split("?", 1)[0]
    return value.strip()


def _parse_allowed_ip_networks(raw: str) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    values = [v.strip() for v in raw.split(",") if v.strip()]
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for value in values:
        try:
            if "/" in value:
                network = ipaddress.ip_network(value, strict=False)
            else:
                host = ipaddress.ip_address(value)
                network = ipaddress.ip_network(f"{host}/{host.max_prefixlen}", strict=False)
        except ValueError as exc:
            raise RuntimeError(
                f"Invalid APP_ALLOWED_IPS entry '{value}'. Use IPv4/IPv6 or CIDR values."
            ) from exc
        networks.append(network)
    return networks


settings = load_settings()
allowed_ip_networks = _parse_allowed_ip_networks(settings.app_allowed_ips)
if allowed_ip_networks:
    logger.info("IP allowlist enabled with %d entries", len(allowed_ip_networks))
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
DEFAULT_STREAM_EVENTS = ("stream.online", "stream.offline")
BROADCASTER_AUTH_SCOPES = ("channel:bot",)
SERVICE_USER_AUTH_SCOPES = ("user:read:email",)


def _counter(value: int | None) -> int:
    return value or 0


def _resolve_client_ip(
    direct_host: str | None,
    x_forwarded_for: str | None,
) -> str | None:
    if settings.app_trust_x_forwarded_for and x_forwarded_for:
        forwarded = x_forwarded_for.split(",", 1)[0].strip()
        if forwarded:
            return forwarded
    return direct_host


def _request_client_ip(request: Request) -> str | None:
    direct_host = request.client.host if request.client else None
    return _resolve_client_ip(direct_host, request.headers.get("x-forwarded-for"))


def _is_ip_allowed(client_ip: str | None) -> bool:
    if not allowed_ip_networks:
        return True
    if not client_ip:
        return False
    try:
        parsed_ip = ipaddress.ip_address(client_ip)
    except ValueError:
        return False
    return any(parsed_ip in network for network in allowed_ip_networks)


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
        stats = await session.get(ServiceRuntimeStats, service_account_id)
        if not stats:
            stats = ServiceRuntimeStats(service_account_id=service_account_id)
            session.add(stats)
        mutator(stats)
        await session.commit()


async def _on_service_connect(service_account_id: uuid.UUID) -> None:
    now = datetime.now(UTC)

    def _mutator(stats: ServiceRuntimeStats) -> None:
        stats.active_ws_connections = _counter(stats.active_ws_connections) + 1
        stats.total_ws_connects = _counter(stats.total_ws_connects) + 1
        stats.is_connected = stats.active_ws_connections > 0
        stats.last_connected_at = now

    await _update_runtime_stats(service_account_id, _mutator)


async def _on_service_disconnect(service_account_id: uuid.UUID) -> None:
    now = datetime.now(UTC)

    def _mutator(stats: ServiceRuntimeStats) -> None:
        stats.active_ws_connections = max(0, _counter(stats.active_ws_connections) - 1)
        stats.is_connected = stats.active_ws_connections > 0
        stats.last_disconnected_at = now

    await _update_runtime_stats(service_account_id, _mutator)


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
        await engine.dispose()


app = FastAPI(title="Twitch EventSub Service", lifespan=lifespan)


@app.middleware("http")
async def enforce_ip_allowlist(request: Request, call_next):
    if request.url.path == TWITCH_WEBHOOK_PATH:
        return await call_next(request)
    if not allowed_ip_networks:
        return await call_next(request)
    client_ip = _request_client_ip(request)
    if not _is_ip_allowed(client_ip):
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


@app.get("/health")
async def health():
    return {"ok": True}


@app.get("/oauth/callback")
async def oauth_callback(code: str | None = None, state: str | None = None, error: str | None = None):
    if state:
        async with session_factory() as session:
            auth_request = await session.get(BroadcasterAuthorizationRequest, state)
            if auth_request:
                redirect_url = auth_request.redirect_url
                now = datetime.now(UTC)
                if error:
                    auth_request.status = "failed"
                    auth_request.error = error
                    auth_request.completed_at = now
                    await session.commit()
                    if redirect_url:
                        return RedirectResponse(
                            url=_append_query(
                                redirect_url,
                                {
                                    "ok": "false",
                                    "error": error,
                                    "message": "Broadcaster authorization failed.",
                                },
                            ),
                            status_code=302,
                        )
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
                    if redirect_url:
                        return RedirectResponse(
                            url=_append_query(
                                redirect_url,
                                {
                                    "ok": "false",
                                    "error": "missing_code",
                                    "message": "Missing OAuth code",
                                },
                            ),
                            status_code=302,
                        )
                    raise HTTPException(status_code=400, detail="Missing OAuth code")

                try:
                    token = await twitch_client.exchange_code(code)
                    token_info = await twitch_client.validate_user_token(token.access_token)
                except Exception as exc:
                    auth_request.status = "failed"
                    auth_request.error = str(exc)
                    auth_request.completed_at = now
                    await session.commit()
                    if redirect_url:
                        return RedirectResponse(
                            url=_append_query(
                                redirect_url,
                                {
                                    "ok": "false",
                                    "error": "oauth_exchange_failed",
                                    "message": f"OAuth exchange failed: {exc}",
                                },
                            ),
                            status_code=302,
                        )
                    raise HTTPException(status_code=502, detail=f"OAuth exchange failed: {exc}") from exc

                granted_scopes = sorted(set(token_info.get("scopes", [])))
                required = set(BROADCASTER_AUTH_SCOPES)
                if not required.issubset(set(granted_scopes)):
                    missing_required = ",".join(sorted(required - set(granted_scopes)))
                    auth_request.status = "failed"
                    auth_request.error = "missing_required_scopes:" + missing_required
                    auth_request.completed_at = now
                    await session.commit()
                    if redirect_url:
                        return RedirectResponse(
                            url=_append_query(
                                redirect_url,
                                {
                                    "ok": "false",
                                    "error": "missing_required_scopes",
                                    "message": (
                                        "Broadcaster authorization succeeded but required scopes are missing: "
                                        + missing_required
                                    ),
                                },
                            ),
                            status_code=302,
                        )
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            "Broadcaster authorization succeeded but required scopes are missing: "
                            + missing_required
                        ),
                    )

                broadcaster_user_id = str(token_info.get("user_id", ""))
                broadcaster_login = str(token_info.get("login", ""))
                if not broadcaster_user_id or not broadcaster_login:
                    auth_request.status = "failed"
                    auth_request.error = "missing_broadcaster_identity"
                    auth_request.completed_at = now
                    await session.commit()
                    if redirect_url:
                        return RedirectResponse(
                            url=_append_query(
                                redirect_url,
                                {
                                    "ok": "false",
                                    "error": "missing_broadcaster_identity",
                                    "message": "Could not resolve broadcaster identity",
                                },
                            ),
                            status_code=302,
                        )
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
                if redirect_url:
                    return RedirectResponse(
                        url=_append_query(
                            redirect_url,
                            {
                                "ok": "true",
                                "message": "Broadcaster authorization completed.",
                                "service_connected": "true",
                                "broadcaster_user_id": broadcaster_user_id,
                                "broadcaster_login": broadcaster_login,
                                "scopes": ",".join(granted_scopes),
                            },
                        ),
                        status_code=302,
                    )
                return {
                    "ok": True,
                    "message": "Broadcaster authorization completed.",
                    "service_connected": True,
                    "broadcaster_user_id": broadcaster_user_id,
                    "broadcaster_login": broadcaster_login,
                    "scopes": granted_scopes,
                }
            user_auth_request = await session.get(ServiceUserAuthRequest, state)
            if user_auth_request:
                redirect_url = user_auth_request.redirect_url
                now = datetime.now(UTC)
                if error:
                    user_auth_request.status = "failed"
                    user_auth_request.error = error
                    user_auth_request.completed_at = now
                    await session.commit()
                    if redirect_url:
                        return RedirectResponse(
                            url=_append_query(
                                redirect_url,
                                {
                                    "ok": "false",
                                    "auth_type": "service_user",
                                    "state": state,
                                    "error": error,
                                    "message": "Service user authorization failed.",
                                },
                            ),
                            status_code=302,
                        )
                    return {
                        "ok": False,
                        "auth_type": "service_user",
                        "state": state,
                        "error": error,
                        "message": "Service user authorization failed.",
                    }
                if not code:
                    user_auth_request.status = "failed"
                    user_auth_request.error = "missing_code"
                    user_auth_request.completed_at = now
                    await session.commit()
                    if redirect_url:
                        return RedirectResponse(
                            url=_append_query(
                                redirect_url,
                                {
                                    "ok": "false",
                                    "auth_type": "service_user",
                                    "state": state,
                                    "error": "missing_code",
                                    "message": "Missing OAuth code",
                                },
                            ),
                            status_code=302,
                        )
                    raise HTTPException(status_code=400, detail="Missing OAuth code")

                try:
                    token = await twitch_client.exchange_code(code)
                    token_info = await twitch_client.validate_user_token(token.access_token)
                    users = await twitch_client.get_users(token.access_token)
                    user = users[0] if users else {}
                except Exception as exc:
                    user_auth_request.status = "failed"
                    user_auth_request.error = str(exc)
                    user_auth_request.completed_at = now
                    await session.commit()
                    if redirect_url:
                        return RedirectResponse(
                            url=_append_query(
                                redirect_url,
                                {
                                    "ok": "false",
                                    "auth_type": "service_user",
                                    "state": state,
                                    "error": "oauth_exchange_failed",
                                    "message": f"OAuth exchange failed: {exc}",
                                },
                            ),
                            status_code=302,
                        )
                    raise HTTPException(status_code=502, detail=f"OAuth exchange failed: {exc}") from exc

                granted_scopes = sorted(set(token_info.get("scopes", [])))
                required = set(SERVICE_USER_AUTH_SCOPES)
                if not required.issubset(set(granted_scopes)):
                    missing_required = ",".join(sorted(required - set(granted_scopes)))
                    user_auth_request.status = "failed"
                    user_auth_request.error = "missing_required_scopes:" + missing_required
                    user_auth_request.completed_at = now
                    await session.commit()
                    if redirect_url:
                        return RedirectResponse(
                            url=_append_query(
                                redirect_url,
                                {
                                    "ok": "false",
                                    "auth_type": "service_user",
                                    "state": state,
                                    "error": "missing_required_scopes",
                                    "message": (
                                        "Service user authorization succeeded but required scopes are missing: "
                                        + missing_required
                                    ),
                                },
                            ),
                            status_code=302,
                        )
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            "Service user authorization succeeded but required scopes are missing: "
                            + missing_required
                        ),
                    )

                twitch_user_id = str(token_info.get("user_id", "") or user.get("id", ""))
                twitch_login = str(token_info.get("login", "") or user.get("login", ""))
                if not twitch_user_id or not twitch_login:
                    user_auth_request.status = "failed"
                    user_auth_request.error = "missing_user_identity"
                    user_auth_request.completed_at = now
                    await session.commit()
                    if redirect_url:
                        return RedirectResponse(
                            url=_append_query(
                                redirect_url,
                                {
                                    "ok": "false",
                                    "auth_type": "service_user",
                                    "state": state,
                                    "error": "missing_user_identity",
                                    "message": "Could not resolve authenticated Twitch user identity",
                                },
                            ),
                            status_code=302,
                        )
                    raise HTTPException(
                        status_code=400, detail="Could not resolve authenticated Twitch user identity"
                    )

                user_auth_request.status = "completed"
                user_auth_request.error = None
                user_auth_request.twitch_user_id = twitch_user_id
                user_auth_request.twitch_login = twitch_login
                user_auth_request.twitch_display_name = str(user.get("display_name", twitch_login))
                user_auth_request.twitch_email = user.get("email")
                user_auth_request.access_token = token.access_token
                user_auth_request.refresh_token = token.refresh_token
                user_auth_request.token_expires_at = token.expires_at
                user_auth_request.completed_at = now
                await session.commit()
                if redirect_url:
                    return RedirectResponse(
                        url=_append_query(
                            redirect_url,
                            {
                                "ok": "true",
                                "auth_type": "service_user",
                                "state": state,
                                "message": "Service user authorization completed.",
                                "twitch_user_id": twitch_user_id,
                                "twitch_login": twitch_login,
                                "scopes": ",".join(granted_scopes),
                            },
                        ),
                        status_code=302,
                    )
                return {
                    "ok": True,
                    "auth_type": "service_user",
                    "state": state,
                    "message": "Service user authorization completed.",
                    "twitch_user_id": twitch_user_id,
                    "twitch_login": twitch_login,
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


@app.get("/v1/bots/accessible")
async def list_accessible_bots(service: ServiceAccount = Depends(_service_auth)):
    async with session_factory() as session:
        allowed_ids = await _service_allowed_bot_ids(session, service.id)
        if allowed_ids:
            bots = list(
                (
                    await session.scalars(
                        select(BotAccount).where(
                            BotAccount.id.in_(allowed_ids),
                            BotAccount.enabled.is_(True),
                        )
                    )
                ).all()
            )
            access_mode = "restricted"
        else:
            bots = list((await session.scalars(select(BotAccount).where(BotAccount.enabled.is_(True)))).all())
            access_mode = "all"
    return {
        "access_mode": access_mode,
        "bots": [
            {
                "id": str(bot.id),
                "name": bot.name,
                "twitch_user_id": bot.twitch_user_id,
                "twitch_login": bot.twitch_login,
                "enabled": bot.enabled,
            }
            for bot in bots
        ],
    }


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


@app.post("/v1/user-auth/start", response_model=StartUserAuthorizationResponse)
async def start_service_user_authorization(
    req: StartUserAuthorizationRequest,
    service: ServiceAccount = Depends(_service_auth),
):
    state = secrets.token_urlsafe(24)
    scopes_csv = ",".join(SERVICE_USER_AUTH_SCOPES)
    async with session_factory() as session:
        session.add(
            ServiceUserAuthRequest(
                state=state,
                service_account_id=service.id,
                requested_scopes_csv=scopes_csv,
                redirect_url=str(req.redirect_url) if req.redirect_url else None,
                status="pending",
            )
        )
        await session.commit()
    authorize_url = twitch_client.build_authorize_url_with_scopes(
        state=state,
        scopes=" ".join(SERVICE_USER_AUTH_SCOPES),
        force_verify=True,
    )
    return StartUserAuthorizationResponse(
        state=state,
        authorize_url=authorize_url,
        requested_scopes=list(SERVICE_USER_AUTH_SCOPES),
        expires_in_seconds=600,
    )


@app.get("/v1/user-auth/session/{state}", response_model=UserAuthorizationSessionResponse)
async def get_service_user_authorization_session(
    state: str,
    service: ServiceAccount = Depends(_service_auth),
):
    async with session_factory() as session:
        row = await session.get(ServiceUserAuthRequest, state)
        if not row or row.service_account_id != service.id:
            raise HTTPException(status_code=404, detail="User auth session not found")
    return UserAuthorizationSessionResponse(
        state=row.state,
        status=row.status,
        error=row.error,
        twitch_user_id=row.twitch_user_id,
        twitch_login=row.twitch_login,
        twitch_display_name=row.twitch_display_name,
        twitch_email=row.twitch_email,
        scopes=_split_csv(row.requested_scopes_csv),
        access_token=row.access_token,
        refresh_token=row.refresh_token,
        token_expires_at=row.token_expires_at,
        created_at=row.created_at,
        completed_at=row.completed_at,
    )


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
        await _ensure_service_can_access_bot(session, service.id, req.bot_account_id)

        state = secrets.token_urlsafe(24)
        scopes_csv = ",".join(BROADCASTER_AUTH_SCOPES)
        session.add(
            BroadcasterAuthorizationRequest(
                state=state,
                service_account_id=service.id,
                bot_account_id=req.bot_account_id,
                requested_scopes_csv=scopes_csv,
                redirect_url=str(req.redirect_url) if req.redirect_url else None,
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
    raw_broadcaster = _normalize_broadcaster_id_or_login(req.broadcaster_user_id)
    if not raw_broadcaster:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="broadcaster_user_id is required",
        )
    if raw_broadcaster.isdigit():
        broadcaster_user_id = raw_broadcaster
    else:
        login = raw_broadcaster.lower()
        try:
            token = await twitch_client.app_access_token()
            users = await twitch_client.get_users_by_query(token, logins=[login])
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Failed resolving broadcaster login: {exc}") from exc
        if not users:
            raise HTTPException(status_code=404, detail="Broadcaster login not found")
        broadcaster_user_id = str(users[0].get("id", "")).strip()
        if not broadcaster_user_id:
            raise HTTPException(status_code=502, detail="Twitch user lookup returned empty id")
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
        await _ensure_service_can_access_bot(session, service.id, req.bot_account_id)

        # Best-effort: migrate any interests/channel state that previously stored a login/URL
        # so the system doesn't stay permanently stale.
        if raw_broadcaster != broadcaster_user_id:
            legacy_id = raw_broadcaster
            legacy_interests = list(
                (
                    await session.scalars(
                        select(ServiceInterest).where(
                            ServiceInterest.service_account_id == service.id,
                            ServiceInterest.bot_account_id == req.bot_account_id,
                            ServiceInterest.broadcaster_user_id == legacy_id,
                        )
                    )
                ).all()
            )
            for legacy in legacy_interests:
                dupe = await session.scalar(
                    select(ServiceInterest).where(
                        ServiceInterest.service_account_id == legacy.service_account_id,
                        ServiceInterest.bot_account_id == legacy.bot_account_id,
                        ServiceInterest.event_type == legacy.event_type,
                        ServiceInterest.broadcaster_user_id == broadcaster_user_id,
                        ServiceInterest.transport == legacy.transport,
                        ServiceInterest.webhook_url == legacy.webhook_url,
                    )
                )
                if dupe:
                    await session.delete(legacy)
                else:
                    legacy.broadcaster_user_id = broadcaster_user_id

            legacy_state = await session.scalar(
                select(ChannelState).where(
                    ChannelState.bot_account_id == req.bot_account_id,
                    ChannelState.broadcaster_user_id == legacy_id,
                )
            )
            if legacy_state:
                dupe_state = await session.scalar(
                    select(ChannelState).where(
                        ChannelState.bot_account_id == req.bot_account_id,
                        ChannelState.broadcaster_user_id == broadcaster_user_id,
                    )
                )
                if dupe_state:
                    await session.delete(legacy_state)
                else:
                    legacy_state.broadcaster_user_id = broadcaster_user_id
            await session.commit()

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
    service: ServiceAccount = Depends(_service_auth),
):
    ids = _split_csv(user_ids)
    login_values = _split_csv(logins)
    if not ids and not login_values:
        raise HTTPException(status_code=422, detail="Provide user_ids and/or logins")
    if len(ids) + len(login_values) > 100:
        raise HTTPException(status_code=422, detail="At most 100 ids/logins per request")

    async with session_factory() as session:
        await _ensure_service_can_access_bot(session, service.id, bot_account_id)
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
    service: ServiceAccount = Depends(_service_auth),
):
    ids = _split_csv(broadcaster_user_ids)
    if not ids:
        raise HTTPException(status_code=422, detail="Provide broadcaster_user_ids")
    if len(ids) > 100:
        raise HTTPException(status_code=422, detail="At most 100 broadcaster ids per request")

    async with session_factory() as session:
        await _ensure_service_can_access_bot(session, service.id, bot_account_id)
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
async def interested_stream_status(
    refresh: bool = False,
    service: ServiceAccount = Depends(_service_auth),
):
    async with session_factory() as session:
        interests = list(
            (
                await session.scalars(
                    select(ServiceInterest).where(ServiceInterest.service_account_id == service.id)
                )
            ).all()
        )
        pairs = {(i.bot_account_id, i.broadcaster_user_id) for i in interests}
        if refresh and pairs:
            token = await twitch_client.app_access_token()
            unique_ids = sorted({str(uid) for _, uid in pairs if str(uid).isdigit()})
            streams = await twitch_client.get_streams_by_user_ids(token, unique_ids) if unique_ids else []
            live_by_id = {str(stream.get("user_id", "")): stream for stream in streams}
            now = datetime.now(UTC)

            for bot_id, broadcaster_user_id in pairs:
                uid = str(broadcaster_user_id)
                if not uid.isdigit():
                    continue
                stream = live_by_id.get(uid)
                state = await session.scalar(
                    select(ChannelState).where(
                        ChannelState.bot_account_id == bot_id,
                        ChannelState.broadcaster_user_id == uid,
                    )
                )
                if not state:
                    state = ChannelState(
                        bot_account_id=bot_id,
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
            await session.commit()
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


@app.get("/v1/twitch/streams/live-test")
async def twitch_stream_live_test(
    bot_account_id: uuid.UUID,
    broadcaster_user_id: str | None = None,
    broadcaster_login: str | None = None,
    refresh: bool = True,
    service: ServiceAccount = Depends(_service_auth),
):
    resolved_user_id = (broadcaster_user_id or "").strip()
    resolved_login = (broadcaster_login or "").strip().lower()
    if not resolved_user_id and not resolved_login:
        raise HTTPException(
            status_code=422,
            detail="Provide broadcaster_user_id or broadcaster_login",
        )

    async with session_factory() as session:
        await _ensure_service_can_access_bot(session, service.id, bot_account_id)
        bot = await session.get(BotAccount, bot_account_id)
        if not bot:
            raise HTTPException(status_code=404, detail="Bot not found")
        if not bot.enabled:
            raise HTTPException(status_code=409, detail="Bot is disabled")
        token = await ensure_bot_access_token(session, twitch_client, bot)

        if resolved_login and not resolved_user_id:
            users = await twitch_client.get_users_by_query(token, logins=[resolved_login])
            if not users:
                raise HTTPException(status_code=404, detail="Broadcaster login not found")
            resolved_user_id = str(users[0].get("id", "")).strip()
            if not resolved_user_id:
                raise HTTPException(status_code=502, detail="Twitch user lookup returned empty id")
            resolved_login = str(users[0].get("login", resolved_login)).strip().lower()

        state = await session.scalar(
            select(ChannelState).where(
                ChannelState.bot_account_id == bot_account_id,
                ChannelState.broadcaster_user_id == resolved_user_id,
            )
        )
        if refresh:
            streams = await twitch_client.get_streams_by_user_ids(token, [resolved_user_id])
            stream = streams[0] if streams else None
            now = datetime.now(UTC)
            if not state:
                state = ChannelState(
                    bot_account_id=bot_account_id,
                    broadcaster_user_id=resolved_user_id,
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
            await session.commit()

        if not state:
            raise HTTPException(
                status_code=404,
                detail="No cached stream state found. Retry with refresh=true.",
            )

        return {
            "bot_account_id": str(bot_account_id),
            "broadcaster_user_id": resolved_user_id,
            "broadcaster_login": resolved_login or None,
            "is_live": state.is_live,
            "title": state.title,
            "game_name": state.game_name,
            "started_at": state.started_at.isoformat() if state.started_at else None,
            "last_checked_at": state.last_checked_at.isoformat() if state.last_checked_at else None,
            "source": "twitch" if refresh else "cache",
        }


@app.get("/v1/twitch/streams/live-public")
async def twitch_stream_live_public(
    broadcaster: str,
    service: ServiceAccount = Depends(_service_auth),
):
    """
    Public live status check (no bot required).
    Uses the app token to resolve the broadcaster and check Helix streams.
    """
    _ = service  # service auth required; value not otherwise used here.
    token = await twitch_client.app_access_token()

    raw = _normalize_broadcaster_id_or_login(broadcaster)
    if not raw:
        raise HTTPException(status_code=422, detail="Provide broadcaster (id/login/url)")

    resolved_user_id = raw if raw.isdigit() else ""
    resolved_login = "" if raw.isdigit() else raw.lower()

    if resolved_login and not resolved_user_id:
        users = await twitch_client.get_users_by_query(token, logins=[resolved_login])
        if not users:
            raise HTTPException(status_code=404, detail="Broadcaster login not found")
        resolved_user_id = str(users[0].get("id", "")).strip()
        if not resolved_user_id:
            raise HTTPException(status_code=502, detail="Twitch user lookup returned empty id")
        resolved_login = str(users[0].get("login", resolved_login)).strip().lower()

    streams = await twitch_client.get_streams_by_user_ids(token, [resolved_user_id])
    stream = streams[0] if streams else None

    out: dict[str, object] = {
        "broadcaster_user_id": resolved_user_id,
        "broadcaster_login": resolved_login or None,
        "is_live": bool(stream),
        "source": "twitch",
    }
    if stream:
        out.update(
            {
                "title": stream.get("title"),
                "game_name": stream.get("game_name"),
                "started_at": stream.get("started_at"),
                "viewer_count": stream.get("viewer_count"),
                "stream_id": stream.get("id"),
            }
        )
    return out


@app.post("/v1/twitch/chat/messages", response_model=SendChatMessageResponse)
async def send_twitch_chat_message(
    req: SendChatMessageRequest,
    service: ServiceAccount = Depends(_service_auth),
):
    broadcaster_user_id = req.broadcaster_user_id.strip()
    async with session_factory() as session:
        await _ensure_service_can_access_bot(session, service.id, req.bot_account_id)
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


@app.post("/v1/twitch/clips", response_model=CreateClipResponse)
async def create_twitch_clip(
    req: CreateClipRequest,
    service: ServiceAccount = Depends(_service_auth),
):
    broadcaster_user_id = req.broadcaster_user_id.strip()
    async with session_factory() as session:
        await _ensure_service_can_access_bot(session, service.id, req.bot_account_id)
        bot = await session.get(BotAccount, req.bot_account_id)
        if not bot:
            raise HTTPException(status_code=404, detail="Bot not found")
        if not bot.enabled:
            raise HTTPException(status_code=409, detail="Bot is disabled")
        token = await ensure_bot_access_token(session, twitch_client, bot)
        token_info = await twitch_client.validate_user_token(token)
        scopes = set(token_info.get("scopes", []))
        if "clips:edit" not in scopes:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Bot token missing required scope 'clips:edit'. "
                    "Re-run Guided bot setup to refresh OAuth scopes."
                ),
            )

    try:
        create_payload = await twitch_client.create_clip(
            access_token=token,
            broadcaster_id=broadcaster_user_id,
            title=req.title,
            duration=req.duration,
            has_delay=req.has_delay,
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Failed creating clip: {exc}") from exc

    clip_id = str(create_payload.get("id", ""))
    if not clip_id:
        raise HTTPException(status_code=502, detail="Clip API returned empty clip id")

    # Create Clip is asynchronous; poll Get Clips for up to ~15s for final metadata.
    ready_clip: dict | None = None
    for _ in range(15):
        await asyncio.sleep(1)
        try:
            clips = await twitch_client.get_clips(access_token=token, clip_ids=[clip_id])
        except Exception:
            clips = []
        if clips:
            ready_clip = clips[0]
            break

    if not ready_clip:
        return CreateClipResponse(
            clip_id=clip_id,
            edit_url=str(create_payload.get("edit_url", "")),
            status="processing",
            title=req.title,
            duration=req.duration,
            broadcaster_user_id=broadcaster_user_id,
        )

    return CreateClipResponse(
        clip_id=clip_id,
        edit_url=str(create_payload.get("edit_url", "")),
        status="ready",
        title=req.title,
        duration=req.duration,
        broadcaster_user_id=broadcaster_user_id,
        created_at=ready_clip.get("created_at"),
        url=ready_clip.get("url"),
        embed_url=ready_clip.get("embed_url"),
        thumbnail_url=ready_clip.get("thumbnail_url"),
    )


@app.websocket("/ws/events")
async def ws_events(websocket: WebSocket, client_id: str = Query(), client_secret: str = Query()):
    client_ip = _resolve_client_ip(
        websocket.client.host if websocket.client else None,
        websocket.headers.get("x-forwarded-for"),
    )
    if not _is_ip_allowed(client_ip):
        logger.warning("Blocked WebSocket connection from IP %s", client_ip or "unknown")
        await websocket.close(code=4403)
        return
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


_SOCKET_IO_MISMATCH_MESSAGE = (
    "Socket.IO is not supported by this service. Use plain WebSocket endpoint "
    "/ws/events?client_id=<client_id>&client_secret=<client_secret>."
)
_WS_ENDPOINT_MISMATCH_MESSAGE = (
    "Invalid WebSocket endpoint. Use /ws/events?client_id=<client_id>&client_secret=<client_secret>. "
    "Socket.IO is not supported."
)


@app.get("/socket.io")
@app.get("/socket.io/")
async def socketio_http_mismatch() -> PlainTextResponse:
    return PlainTextResponse(_SOCKET_IO_MISMATCH_MESSAGE, status_code=426)


@app.websocket("/socket.io")
@app.websocket("/socket.io/")
async def socketio_ws_mismatch(websocket: WebSocket):
    client_ip = _resolve_client_ip(
        websocket.client.host if websocket.client else None,
        websocket.headers.get("x-forwarded-for"),
    )
    if not _is_ip_allowed(client_ip):
        await websocket.close(code=4403)
        return
    await websocket.accept()
    await websocket.send_text(_SOCKET_IO_MISMATCH_MESSAGE)
    await websocket.close(code=4400)


@app.websocket("/{full_path:path}")
async def websocket_path_mismatch(websocket: WebSocket, full_path: str):
    if full_path == "ws/events":
        await websocket.close(code=4404)
        return
    client_ip = _resolve_client_ip(
        websocket.client.host if websocket.client else None,
        websocket.headers.get("x-forwarded-for"),
    )
    if not _is_ip_allowed(client_ip):
        await websocket.close(code=4403)
        return
    await websocket.accept()
    await websocket.send_text(_WS_ENDPOINT_MISMATCH_MESSAGE)
    await websocket.close(code=4404)


def run() -> None:
    uvicorn.run(
        "app.main:app",
        host=settings.app_host,
        port=settings.app_port,
        reload=False,
        log_level=settings.app_log_level,
    )
