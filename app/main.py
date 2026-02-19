from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import logging
import secrets
import socket
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
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
from sqlalchemy.exc import IntegrityError

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
    recommended_broadcaster_scopes,
    supported_twitch_transports,
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
    ServiceEventTrace,
    ServiceInterest,
    ServiceRuntimeStats,
    ServiceUserAuthRequest,
    TwitchSubscription,
)
from app.schemas import (
    ActiveTwitchSubscriptionItem,
    ActiveTwitchSubscriptionsResponse,
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
    StartMinimalBroadcasterAuthorizationRequest,
    StartBroadcasterAuthorizationResponse,
    StartUserAuthorizationRequest,
    StartUserAuthorizationResponse,
    ServiceSubscriptionsResponse,
    ServiceSubscriptionItem,
    ServiceSubscriptionTransportRow,
    ServiceSubscriptionTransportSummaryResponse,
    UserAuthorizationSessionResponse,
)
from app.routes import register_admin_routes, register_service_routes, register_twitch_routes
from app.twitch import TwitchClient
from app.twitch_chat_assets import TwitchChatAssetCache

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("twitch-eventsub-service")
eventsub_audit_logger = logging.getLogger("eventsub.audit")
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


def _parse_webhook_target_allowlist(raw: str) -> list[str]:
    hosts = [v.strip().lower().lstrip(".") for v in raw.split(",") if v.strip()]
    for host in hosts:
        if "://" in host or "/" in host:
            raise RuntimeError(
                f"Invalid APP_WEBHOOK_TARGET_ALLOWLIST entry '{host}'. Use hostnames only."
            )
    return hosts


def _host_matches_allowlist(host: str, allowlist: list[str]) -> bool:
    normalized = host.strip().lower().rstrip(".")
    if not allowlist:
        return True
    return any(normalized == allowed or normalized.endswith(f".{allowed}") for allowed in allowlist)


def _is_public_ip_address(value: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return not (
        value.is_private
        or value.is_loopback
        or value.is_link_local
        or value.is_multicast
        or value.is_reserved
        or value.is_unspecified
    )


async def _validate_webhook_target_url(raw_url: str) -> None:
    split = urlsplit(raw_url)
    if split.scheme not in {"http", "https"}:
        raise HTTPException(status_code=422, detail="webhook_url must use http or https")
    if split.username or split.password:
        raise HTTPException(status_code=422, detail="webhook_url must not contain userinfo credentials")
    host = (split.hostname or "").strip().lower().rstrip(".")
    if not host:
        raise HTTPException(status_code=422, detail="webhook_url host is required")
    if not _host_matches_allowlist(host, webhook_target_allowlist):
        raise HTTPException(status_code=422, detail="webhook_url host is not allowed by APP_WEBHOOK_TARGET_ALLOWLIST")
    if not settings.app_block_private_webhook_targets:
        return
    try:
        parsed = ipaddress.ip_address(host)
    except ValueError:
        parsed = None
    if parsed:
        if not _is_public_ip_address(parsed):
            raise HTTPException(status_code=422, detail="webhook_url target IP must be public")
        return
    if host.endswith((".localhost", ".local", ".internal")):
        raise HTTPException(status_code=422, detail="webhook_url target host is not public")
    port = split.port or (443 if split.scheme == "https" else 80)
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise HTTPException(status_code=422, detail=f"webhook_url host resolution failed: {exc}") from exc
    if not infos:
        raise HTTPException(status_code=422, detail="webhook_url host resolution returned no addresses")
    resolved_ips: set[str] = set()
    for family, _, _, _, sockaddr in infos:
        if family == socket.AF_INET:
            resolved_ips.add(str(sockaddr[0]))
        elif family == socket.AF_INET6:
            resolved_ips.add(str(sockaddr[0]))
    if not resolved_ips:
        raise HTTPException(status_code=422, detail="webhook_url host resolution returned no usable IP addresses")
    for raw_ip in resolved_ips:
        try:
            ip_value = ipaddress.ip_address(raw_ip)
        except ValueError:
            continue
        if not _is_public_ip_address(ip_value):
            raise HTTPException(
                status_code=422,
                detail="webhook_url target host resolves to non-public IP address",
            )


settings = load_settings()
_eventsub_log_path = Path(settings.app_eventsub_log_path)
_eventsub_log_path.parent.mkdir(parents=True, exist_ok=True)
if not any(isinstance(h, logging.FileHandler) for h in eventsub_audit_logger.handlers):
    _eventsub_audit_file_handler = logging.FileHandler(_eventsub_log_path, encoding="utf-8")
    _eventsub_audit_file_handler.setFormatter(logging.Formatter("%(message)s"))
    eventsub_audit_logger.addHandler(_eventsub_audit_file_handler)
eventsub_audit_logger.setLevel(logging.INFO)
eventsub_audit_logger.propagate = False

allowed_ip_networks = _parse_allowed_ip_networks(settings.app_allowed_ips)
if allowed_ip_networks:
    logger.info("IP allowlist enabled with %d entries", len(allowed_ip_networks))
webhook_target_allowlist = _parse_webhook_target_allowlist(settings.app_webhook_target_allowlist)
if webhook_target_allowlist:
    logger.info(
        "Webhook target allowlist enabled with %d host entries",
        len(webhook_target_allowlist),
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
_ws_tokens: dict[str, tuple[uuid.UUID, datetime]] = {}
_ws_tokens_lock = asyncio.Lock()
_eventsub_seen_message_ids: dict[str, datetime] = {}
_eventsub_seen_lock = asyncio.Lock()


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


async def _issue_ws_token(service_account_id: uuid.UUID) -> tuple[str, int]:
    token = secrets.token_urlsafe(32)
    expires_at = datetime.now(UTC) + WS_TOKEN_TTL
    async with _ws_tokens_lock:
        now = datetime.now(UTC)
        expired = [k for k, (_, exp) in _ws_tokens.items() if exp <= now]
        for key in expired:
            _ws_tokens.pop(key, None)
        _ws_tokens[token] = (service_account_id, expires_at)
    return token, int(WS_TOKEN_TTL.total_seconds())


async def _consume_ws_token(token: str) -> uuid.UUID | None:
    now = datetime.now(UTC)
    async with _ws_tokens_lock:
        expired = [k for k, (_, exp) in _ws_tokens.items() if exp <= now]
        for key in expired:
            _ws_tokens.pop(key, None)
        payload = _ws_tokens.pop(token, None)
    if not payload:
        return None
    service_account_id, expires_at = payload
    if expires_at <= now:
        return None
    return service_account_id


async def _is_new_eventsub_message_id(message_id: str) -> bool:
    if not message_id:
        return False
    now = datetime.now(UTC)
    async with _eventsub_seen_lock:
        threshold = now - EVENTSUB_MESSAGE_DEDUP_TTL
        expired = [k for k, seen_at in _eventsub_seen_message_ids.items() if seen_at < threshold]
        for key in expired:
            _eventsub_seen_message_ids.pop(key, None)
        if message_id in _eventsub_seen_message_ids:
            return False
        _eventsub_seen_message_ids[message_id] = now
        return True


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


def _is_sensitive_key(key: str) -> bool:
    normalized = key.strip().lower().replace("-", "_")
    return any(
        token in normalized
        for token in (
            "secret",
            "token",
            "authorization",
            "api_key",
            "password",
            "client_secret",
            "x_client_secret",
            "ws_token",
        )
    )


def _mask_secret(value: object) -> str:
    raw = str(value)
    if not raw or len(raw) <= 4:
        return "***"
    return "***" + raw[-4:]


def _redact_trace_payload(payload: object) -> object:
    if isinstance(payload, dict):
        out: dict[str, object] = {}
        for key, value in payload.items():
            if _is_sensitive_key(str(key)):
                out[str(key)] = _mask_secret(value)
            else:
                out[str(key)] = _redact_trace_payload(value)
        return out
    if isinstance(payload, list):
        return [_redact_trace_payload(item) for item in payload]
    return payload


async def _record_service_trace(
    service_account_id: uuid.UUID,
    direction: str,
    local_transport: str,
    event_type: str,
    target: str | None,
    payload: object,
) -> None:
    try:
        payload_json = json.dumps(_redact_trace_payload(payload), default=str)
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
    message_id = request.headers.get("Twitch-Eventsub-Message-Id", "")
    message_type = request.headers.get("Twitch-Eventsub-Message-Type", "").lower()
    payload = await request.json()
    if not await _is_new_eventsub_message_id(message_id):
        if message_type == "webhook_callback_verification":
            challenge = payload.get("challenge", "")
            return PlainTextResponse(content=challenge, status_code=200)
        return Response(status_code=204)

    if message_type == "webhook_callback_verification":
        challenge = payload.get("challenge", "")
        return PlainTextResponse(content=challenge, status_code=200)

    if message_type == "notification":
        asyncio.create_task(
            eventsub_manager.handle_webhook_notification(
                payload, message_id
            )
        )
        return Response(status_code=204)

    if message_type == "revocation":
        asyncio.create_task(eventsub_manager.handle_webhook_revocation(payload))
        return Response(status_code=204)

    return Response(status_code=204)



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
    validate_webhook_target_url=_validate_webhook_target_url,
    normalize_broadcaster_id_or_login=_normalize_broadcaster_id_or_login,
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
    normalize_broadcaster_id_or_login=_normalize_broadcaster_id_or_login,
)
@app.websocket("/ws/events")
async def ws_events(
    websocket: WebSocket,
    ws_token: str | None = Query(default=None),
):
    client_ip = _resolve_client_ip(
        websocket.client.host if websocket.client else None,
        websocket.headers.get("x-forwarded-for"),
    )
    if not _is_ip_allowed(client_ip):
        logger.warning("Blocked WebSocket connection from IP %s", client_ip or "unknown")
        await websocket.close(code=4403)
        return
    raw_ws_token = (ws_token or "").strip()
    token_value = raw_ws_token if raw_ws_token and raw_ws_token.lower() not in {"undefined", "null"} else ""
    if not token_value:
        await websocket.close(code=4401)
        return
    service_account_id = await _consume_ws_token(token_value)
    if not service_account_id:
        await websocket.close(code=4401)
        return
    async with session_factory() as session:
        service = await session.get(ServiceAccount, service_account_id)
    if not service or not service.enabled:
        await websocket.close(code=4401)
        return
    logger.info("Incoming /ws/events connection accepted for service_id=%s", service.id)
    await _record_service_trace(
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
        await _record_service_trace(
            service_account_id=service.id,
            direction="incoming",
            local_transport="websocket",
            event_type="service.ws.disconnect",
            target="/ws/events",
            payload={"client_ip": client_ip},
        )


_SOCKET_IO_MISMATCH_MESSAGE = (
    "Socket.IO is not supported by this service. Use plain WebSocket endpoint "
    "/ws/events?ws_token=<short_lived_token>."
)
_WS_ENDPOINT_MISMATCH_MESSAGE = (
    "Invalid WebSocket endpoint. Use /ws/events?ws_token=<short_lived_token>. "
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

