from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from datetime import UTC, datetime, timedelta
from collections.abc import Awaitable, Callable

from fastapi import Depends, FastAPI, HTTPException
from sqlalchemy import select

from app.bot_auth import ensure_bot_access_token
from app.models import BotAccount, ChannelState, ServiceAccount, ServiceEventTrace, ServiceInterest
from app.schemas import (
    CreateClipRequest,
    CreateClipResponse,
    DeleteChatMessageRequest,
    DeleteChatMessageResponse,
    ModerateUserRequest,
    ModerateUserResponse,
    SendChatMessageRequest,
    SendChatMessageResponse,
)
from app.twitch import TwitchApiError

logger = logging.getLogger(__name__)


def register_twitch_routes(
    app: FastAPI,
    *,
    session_factory,
    twitch_client,
    chat_assets,
    service_auth,
    split_csv: Callable[[str | None], list[str]],
    ensure_service_can_access_bot: Callable[[object, uuid.UUID, uuid.UUID], Awaitable[None]],
    normalize_broadcaster_id_or_login: Callable[[str], str],
) -> None:
    live_test_refresh_min_interval = timedelta(seconds=20)
    login_cache_ttl = timedelta(hours=6)
    login_cache: dict[str, tuple[str, str, datetime]] = {}
    login_cache_guard = asyncio.Lock()
    login_lookup_locks: dict[str, asyncio.Lock] = {}
    trace_tasks: set[asyncio.Task] = set()
    trace_task_limit = 2000
    token_info_cache_ttl = timedelta(minutes=5)
    token_info_cache: dict[uuid.UUID, tuple[str, set[str], str, datetime]] = {}
    token_info_cache_lock = asyncio.Lock()

    def _schedule_trace(task: asyncio.Task) -> None:
        if trace_task_limit and len(trace_tasks) >= trace_task_limit:
            return
        trace_tasks.add(task)
        task.add_done_callback(lambda t: trace_tasks.discard(t))

    async def _write_twitch_action(
        *,
        service_account_id: uuid.UUID,
        direction: str,
        local_transport: str,
        event_type: str,
        target: str,
        payload: object,
    ) -> None:
        try:
            payload_dict = payload if isinstance(payload, dict) else {"value": payload}
            payload_json = json.dumps(payload_dict, default=str)
            if len(payload_json) > 12000:
                payload_json = payload_json[:12000] + "... [truncated]"
            async with session_factory() as session:
                service_row = await session.get(ServiceAccount, service_account_id)
                if not service_row:
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

    async def _record_twitch_action(
        *,
        service_account_id: uuid.UUID,
        direction: str,
        local_transport: str,
        event_type: str,
        target: str,
        payload: object,
    ) -> None:
        try:
            task = asyncio.create_task(
                _write_twitch_action(
                    service_account_id=service_account_id,
                    direction=direction,
                    local_transport=local_transport,
                    event_type=event_type,
                    target=target,
                    payload=payload,
                )
            )
            _schedule_trace(task)
        except Exception:
            return

    async def _get_cached_token_info(
        session,
        bot: BotAccount,
        access_token: str,
    ) -> tuple[set[str], str]:
        if not access_token:
            return set(), ""
        now = datetime.now(UTC)
        async with token_info_cache_lock:
            cached = token_info_cache.get(bot.id)
            if cached and cached[0] == access_token and cached[3] > now:
                return set(cached[1]), cached[2]
        try:
            token_info = await twitch_client.validate_user_token(access_token)
        except TwitchApiError as exc:
            message = str(exc).lower()
            if "invalid access token" in message or "unauthorized" in message or "401" in message:
                logger.warning(
                    "Token validation failed for bot %s; attempting refresh.",
                    bot.id,
                )
                try:
                    refreshed = await twitch_client.refresh_token(bot.refresh_token)
                except TwitchApiError as refresh_exc:
                    raise HTTPException(
                        status_code=502,
                        detail=(
                            "Bot token invalid or expired; re-run Guided bot setup to refresh OAuth tokens."
                        ),
                    ) from refresh_exc
                bot.access_token = refreshed.access_token
                bot.refresh_token = refreshed.refresh_token
                bot.token_expires_at = refreshed.expires_at
                await session.commit()
                access_token = bot.access_token
                token_info = await twitch_client.validate_user_token(access_token)
            else:
                raise
        scopes = set(token_info.get("scopes", []))
        user_id = str(token_info.get("user_id", "")).strip()
        async with token_info_cache_lock:
            token_info_cache[bot.id] = (access_token, scopes, user_id, now + token_info_cache_ttl)
        return scopes, user_id

    async def _resolve_login_with_cache(token: str, login: str) -> tuple[str, str]:
        normalized = login.strip().lower()
        if not normalized:
            raise HTTPException(status_code=422, detail="Broadcaster login is required")
        now = datetime.now(UTC)
        async with login_cache_guard:
            cached = login_cache.get(normalized)
            if cached and cached[2] > now:
                return cached[0], cached[1]
            lookup_lock = login_lookup_locks.get(normalized)
            if not lookup_lock:
                lookup_lock = asyncio.Lock()
                login_lookup_locks[normalized] = lookup_lock
        async with lookup_lock:
            now = datetime.now(UTC)
            async with login_cache_guard:
                cached = login_cache.get(normalized)
                if cached and cached[2] > now:
                    return cached[0], cached[1]
            users = await twitch_client.get_users_by_query(token, logins=[normalized])
            if not users:
                raise HTTPException(status_code=404, detail="Broadcaster login not found")
            resolved_user_id = str(users[0].get("id", "")).strip()
            if not resolved_user_id:
                raise HTTPException(status_code=502, detail="Twitch user lookup returned empty id")
            resolved_login = str(users[0].get("login", normalized)).strip().lower()
            async with login_cache_guard:
                login_cache[normalized] = (
                    resolved_user_id,
                    resolved_login,
                    now + login_cache_ttl,
                )
            return resolved_user_id, resolved_login

    @app.get("/v1/twitch/profiles")
    async def twitch_profiles(
        bot_account_id: uuid.UUID,
        user_ids: str | None = None,
        logins: str | None = None,
        service: ServiceAccount = Depends(service_auth),
    ):
        started = time.perf_counter()
        action_id = str(uuid.uuid4())
        ids = split_csv(user_ids)
        login_values = split_csv(logins)
        if not ids and not login_values:
            raise HTTPException(status_code=422, detail="Provide user_ids and/or logins")
        if len(ids) + len(login_values) > 100:
            raise HTTPException(status_code=422, detail="At most 100 ids/logins per request")

        async with session_factory() as session:
            await ensure_service_can_access_bot(session, service.id, bot_account_id)
            bot = await session.get(BotAccount, bot_account_id)
            if not bot:
                raise HTTPException(status_code=404, detail="Bot not found")
            if not bot.enabled:
                raise HTTPException(status_code=409, detail="Bot is disabled")
            token = await ensure_bot_access_token(session, twitch_client, bot)
            users = await twitch_client.get_users_by_query(token, user_ids=ids, logins=login_values)
        await _record_twitch_action(
            service_account_id=service.id,
            direction="incoming",
            local_transport="service_api",
            event_type="service.twitch.profiles.lookup",
            target="/v1/twitch/profiles",
            payload={
                "_action_id": action_id,
                "_action_status": "pending",
                "bot_account_id": str(bot_account_id),
                "user_ids": ids,
                "logins": login_values,
            },
        )
        await _record_twitch_action(
            service_account_id=service.id,
            direction="outgoing",
            local_transport="twitch_api",
            event_type="twitch.profiles.lookup",
            target="helix:/users",
            payload={
                "_action_id": action_id,
                "_action_status": "completed",
                "bot_account_id": str(bot_account_id),
                "user_ids": ids,
                "logins": login_values,
                "result_count": len(users),
                "duration_ms": int((time.perf_counter() - started) * 1000),
            },
        )
        return {"data": users}

    @app.get("/v1/twitch/streams/status")
    async def twitch_stream_status(
        bot_account_id: uuid.UUID,
        broadcaster_user_ids: str,
        service: ServiceAccount = Depends(service_auth),
    ):
        started = time.perf_counter()
        action_id = str(uuid.uuid4())
        ids = split_csv(broadcaster_user_ids)
        if not ids:
            raise HTTPException(status_code=422, detail="Provide broadcaster_user_ids")
        if len(ids) > 100:
            raise HTTPException(status_code=422, detail="At most 100 broadcaster ids per request")

        async with session_factory() as session:
            await ensure_service_can_access_bot(session, service.id, bot_account_id)
            bot = await session.get(BotAccount, bot_account_id)
            if not bot:
                raise HTTPException(status_code=404, detail="Bot not found")
            if not bot.enabled:
                raise HTTPException(status_code=409, detail="Bot is disabled")
            token = await ensure_bot_access_token(session, twitch_client, bot)
            streams = await twitch_client.get_streams_by_user_ids(token, ids)
        await _record_twitch_action(
            service_account_id=service.id,
            direction="incoming",
            local_transport="service_api",
            event_type="service.twitch.streams.status",
            target="/v1/twitch/streams/status",
            payload={
                "_action_id": action_id,
                "_action_status": "pending",
                "bot_account_id": str(bot_account_id),
                "broadcaster_user_ids": ids,
            },
        )
        await _record_twitch_action(
            service_account_id=service.id,
            direction="outgoing",
            local_transport="twitch_api",
            event_type="twitch.streams.status",
            target="helix:/streams",
            payload={
                "_action_id": action_id,
                "_action_status": "completed",
                "bot_account_id": str(bot_account_id),
                "broadcaster_user_ids": ids,
                "result_count": len(streams),
                "duration_ms": int((time.perf_counter() - started) * 1000),
            },
        )
        async with session_factory() as session:
            bot = await session.get(BotAccount, bot_account_id)
            by_uid = {str(s.get("user_id", "")): s for s in streams}
            now = datetime.now(UTC)
            for uid in ids:
                stream = by_uid.get(uid)
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
            await session.commit()
            rows = []
            for uid in ids:
                state = await session.scalar(
                    select(ChannelState).where(
                        ChannelState.bot_account_id == bot_account_id,
                        ChannelState.broadcaster_user_id == uid,
                    )
                )
                if not state:
                    rows.append(
                        {
                            "bot_account_id": str(bot_account_id),
                            "broadcaster_user_id": uid,
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

    @app.get("/v1/twitch/streams/status/interested")
    async def interested_stream_status(
        refresh: bool = False,
        service: ServiceAccount = Depends(service_auth),
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
            if refresh:
                for bot_id, broadcaster_user_id in pairs:
                    bot = await session.get(BotAccount, bot_id)
                    if not bot or not bot.enabled:
                        continue
                    token = await ensure_bot_access_token(session, twitch_client, bot)
                    streams = await twitch_client.get_streams_by_user_ids(token, [broadcaster_user_id])
                    stream = streams[0] if streams else None
                    now = datetime.now(UTC)
                    state = await session.scalar(
                        select(ChannelState).where(
                            ChannelState.bot_account_id == bot_id,
                            ChannelState.broadcaster_user_id == broadcaster_user_id,
                        )
                    )
                    if not state:
                        state = ChannelState(
                            bot_account_id=bot_id,
                            broadcaster_user_id=broadcaster_user_id,
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
        service: ServiceAccount = Depends(service_auth),
    ):
        resolved_user_id = (broadcaster_user_id or "").strip()
        resolved_login = (broadcaster_login or "").strip().lower()
        if not resolved_user_id and not resolved_login:
            raise HTTPException(
                status_code=422,
                detail="Provide broadcaster_user_id or broadcaster_login",
            )

        async with session_factory() as session:
            await ensure_service_can_access_bot(session, service.id, bot_account_id)
            bot = await session.get(BotAccount, bot_account_id)
            if not bot:
                raise HTTPException(status_code=404, detail="Bot not found")
            if not bot.enabled:
                raise HTTPException(status_code=409, detail="Bot is disabled")
            token = await ensure_bot_access_token(session, twitch_client, bot)

            if resolved_login and not resolved_user_id:
                resolved_user_id, resolved_login = await _resolve_login_with_cache(token, resolved_login)

            state = await session.scalar(
                select(ChannelState).where(
                    ChannelState.bot_account_id == bot_account_id,
                    ChannelState.broadcaster_user_id == resolved_user_id,
                )
            )
            now = datetime.now(UTC)
            should_refresh = refresh
            if (
                refresh
                and state
                and state.last_checked_at
                and (now - state.last_checked_at) < live_test_refresh_min_interval
            ):
                should_refresh = False
            if should_refresh:
                streams = await twitch_client.get_streams_by_user_ids(token, [resolved_user_id])
                stream = streams[0] if streams else None
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
                "source": "twitch" if should_refresh else "cache",
            }

    @app.get("/v1/twitch/streams/live-public")
    async def twitch_stream_live_public(
        broadcaster: str,
        service: ServiceAccount = Depends(service_auth),
    ):
        _ = service
        token = await twitch_client.app_access_token()

        raw = normalize_broadcaster_id_or_login(broadcaster)
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

    @app.get("/v1/twitch/chat/assets")
    async def twitch_chat_assets(
        broadcaster: str,
        refresh: bool = False,
        service: ServiceAccount = Depends(service_auth),
    ):
        started = time.perf_counter()
        action_id = str(uuid.uuid4())
        _ = service
        token = await twitch_client.app_access_token()

        raw = normalize_broadcaster_id_or_login(broadcaster)
        if not raw:
            raise HTTPException(status_code=422, detail="Provide broadcaster (id/login/url)")

        if raw.isdigit():
            broadcaster_user_id = raw
            broadcaster_login = None
        else:
            login = raw.lower()
            users = await twitch_client.get_users_by_query(token, logins=[login])
            if not users:
                raise HTTPException(status_code=404, detail="Broadcaster login not found")
            broadcaster_user_id = str(users[0].get("id", "")).strip()
            broadcaster_login = str(users[0].get("login", login)).strip().lower()
            if not broadcaster_user_id:
                raise HTTPException(status_code=502, detail="Twitch user lookup returned empty id")

        if refresh:
            await chat_assets.refresh(broadcaster_user_id)
        else:
            chat_assets.prefetch(broadcaster_user_id)
        await _record_twitch_action(
            service_account_id=service.id,
            direction="incoming",
            local_transport="service_api",
            event_type="service.twitch.chat.assets",
            target="/v1/twitch/chat/assets",
            payload={
                "_action_id": action_id,
                "_action_status": "pending",
                "broadcaster_user_id": broadcaster_user_id,
                "broadcaster_login": broadcaster_login,
                "refresh": bool(refresh),
            },
        )
        await _record_twitch_action(
            service_account_id=service.id,
            direction="outgoing",
            local_transport="cache",
            event_type="twitch.chat.assets",
            target="cache:/chat-assets",
            payload={
                "_action_id": action_id,
                "_action_status": "completed",
                "broadcaster_user_id": broadcaster_user_id,
                "broadcaster_login": broadcaster_login,
                "refresh": bool(refresh),
                "duration_ms": int((time.perf_counter() - started) * 1000),
            },
        )

        snapshot = await chat_assets.snapshot(broadcaster_user_id)
        return {
            "broadcaster_user_id": broadcaster_user_id,
            "broadcaster_login": broadcaster_login,
            **snapshot,
        }

    @app.post("/v1/twitch/chat/messages", response_model=SendChatMessageResponse)
    async def send_twitch_chat_message(
        req: SendChatMessageRequest,
        service: ServiceAccount = Depends(service_auth),
    ):
        started = time.perf_counter()
        action_id = str(uuid.uuid4())
        broadcaster_user_id = req.broadcaster_user_id.strip()
        await _record_twitch_action(
            service_account_id=service.id,
            direction="incoming",
            local_transport="service_api",
            event_type="service.twitch.chat.send",
            target="/v1/twitch/chat/messages",
            payload={
                "_action_id": action_id,
                "_action_status": "pending",
                "bot_account_id": str(req.bot_account_id),
                "broadcaster_user_id": broadcaster_user_id,
                "auth_mode_requested": req.auth_mode,
                "reply_parent_message_id": req.reply_parent_message_id,
            },
        )
        async with session_factory() as session:
            await ensure_service_can_access_bot(session, service.id, req.bot_account_id)
            bot = await session.get(BotAccount, req.bot_account_id)
            if not bot:
                raise HTTPException(status_code=404, detail="Bot not found")
            if not bot.enabled:
                raise HTTPException(status_code=409, detail="Bot is disabled")
            token = await ensure_bot_access_token(session, twitch_client, bot)
            scopes, token_user_id = await _get_cached_token_info(session, bot, token)
            token = bot.access_token
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
            if token_user_id and token_user_id != bot.twitch_user_id:
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
            await _record_twitch_action(
                service_account_id=service.id,
                direction="outgoing",
                local_transport="twitch_api",
                event_type="twitch.chat.send",
                target="helix:/chat/messages",
                payload={
                    "_action_id": action_id,
                    "_action_status": "failed",
                    "bot_account_id": str(req.bot_account_id),
                    "broadcaster_user_id": broadcaster_user_id,
                    "auth_mode_requested": req.auth_mode,
                    "duration_ms": int((time.perf_counter() - started) * 1000),
                    "is_sent": False,
                    "error": str(exc),
                },
            )
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
        response = SendChatMessageResponse(
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
        logger.info(
            "Chat send completed: service=%s client_id=%s bot=%s broadcaster=%s auth_mode_req=%s auth_mode_used=%s duration_ms=%d",
            service.id,
            service.client_id,
            req.bot_account_id,
            broadcaster_user_id,
            req.auth_mode,
            auth_mode_used,
            int((time.perf_counter() - started) * 1000),
        )
        await _record_twitch_action(
            service_account_id=service.id,
            direction="outgoing",
            local_transport="twitch_api",
            event_type="twitch.chat.send",
            target="helix:/chat/messages",
            payload={
                "_action_id": action_id,
                "_action_status": "completed" if bool(result.get("is_sent", False)) else "not_sent",
                "bot_account_id": str(req.bot_account_id),
                "broadcaster_user_id": broadcaster_user_id,
                "auth_mode_requested": req.auth_mode,
                "auth_mode_used": auth_mode_used,
                "duration_ms": int((time.perf_counter() - started) * 1000),
                "is_sent": bool(result.get("is_sent", False)),
                "drop_reason": result.get("drop_reason"),
            },
        )
        return response

    @app.post("/v1/twitch/moderation/timeout", response_model=ModerateUserResponse)
    async def timeout_twitch_user(
        req: ModerateUserRequest,
        service: ServiceAccount = Depends(service_auth),
    ):
        started = time.perf_counter()
        action_id = str(uuid.uuid4())
        broadcaster_user_id = req.broadcaster_user_id.strip()
        target_user_id = req.target_user_id.strip()
        await _record_twitch_action(
            service_account_id=service.id,
            direction="incoming",
            local_transport="service_api",
            event_type="service.twitch.moderation.timeout",
            target="/v1/twitch/moderation/timeout",
            payload={
                "_action_id": action_id,
                "_action_status": "pending",
                "bot_account_id": str(req.bot_account_id),
                "broadcaster_user_id": broadcaster_user_id,
                "target_user_id": target_user_id,
                "duration": req.duration,
                "reason": req.reason,
            },
        )
        async with session_factory() as session:
            await ensure_service_can_access_bot(session, service.id, req.bot_account_id)
            bot = await session.get(BotAccount, req.bot_account_id)
            if not bot:
                raise HTTPException(status_code=404, detail="Bot not found")
            if not bot.enabled:
                raise HTTPException(status_code=409, detail="Bot is disabled")
            token = await ensure_bot_access_token(session, twitch_client, bot)
            scopes, token_user_id = await _get_cached_token_info(session, bot, token)
            token = bot.access_token
            if "moderator:manage:banned_users" not in scopes:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Bot token missing required scope 'moderator:manage:banned_users'. "
                        "Re-run Guided bot setup to refresh OAuth scopes."
                    ),
                )
            if token_user_id and token_user_id != bot.twitch_user_id:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Stored bot token does not belong to this bot account. "
                        "Re-run Guided bot setup and update the bot credentials."
                    ),
                )
        try:
            await twitch_client.moderate_user(
                access_token=token,
                broadcaster_id=broadcaster_user_id,
                moderator_id=bot.twitch_user_id,
                target_user_id=target_user_id,
                duration=req.duration,
                reason=req.reason,
            )
        except Exception as exc:
            await _record_twitch_action(
                service_account_id=service.id,
                direction="outgoing",
                local_transport="twitch_api",
                event_type="twitch.moderation.timeout",
                target="helix:/moderation/bans",
                payload={
                    "_action_id": action_id,
                    "_action_status": "failed",
                    "bot_account_id": str(req.bot_account_id),
                    "broadcaster_user_id": broadcaster_user_id,
                    "target_user_id": target_user_id,
                    "duration_ms": int((time.perf_counter() - started) * 1000),
                    "error": str(exc),
                },
            )
            raise HTTPException(status_code=502, detail=f"Failed timing out user: {exc}") from exc
        await _record_twitch_action(
            service_account_id=service.id,
            direction="outgoing",
            local_transport="twitch_api",
            event_type="twitch.moderation.timeout",
            target="helix:/moderation/bans",
            payload={
                "_action_id": action_id,
                "_action_status": "completed",
                "bot_account_id": str(req.bot_account_id),
                "broadcaster_user_id": broadcaster_user_id,
                "target_user_id": target_user_id,
                "duration_ms": int((time.perf_counter() - started) * 1000),
            },
        )
        return ModerateUserResponse(
            broadcaster_user_id=broadcaster_user_id,
            moderator_user_id=bot.twitch_user_id,
            target_user_id=target_user_id,
            action="timeout",
            duration=req.duration,
            reason=req.reason,
        )

    @app.post("/v1/twitch/moderation/ban", response_model=ModerateUserResponse)
    async def ban_twitch_user(
        req: ModerateUserRequest,
        service: ServiceAccount = Depends(service_auth),
    ):
        started = time.perf_counter()
        action_id = str(uuid.uuid4())
        broadcaster_user_id = req.broadcaster_user_id.strip()
        target_user_id = req.target_user_id.strip()
        await _record_twitch_action(
            service_account_id=service.id,
            direction="incoming",
            local_transport="service_api",
            event_type="service.twitch.moderation.ban",
            target="/v1/twitch/moderation/ban",
            payload={
                "_action_id": action_id,
                "_action_status": "pending",
                "bot_account_id": str(req.bot_account_id),
                "broadcaster_user_id": broadcaster_user_id,
                "target_user_id": target_user_id,
                "reason": req.reason,
            },
        )
        async with session_factory() as session:
            await ensure_service_can_access_bot(session, service.id, req.bot_account_id)
            bot = await session.get(BotAccount, req.bot_account_id)
            if not bot:
                raise HTTPException(status_code=404, detail="Bot not found")
            if not bot.enabled:
                raise HTTPException(status_code=409, detail="Bot is disabled")
            token = await ensure_bot_access_token(session, twitch_client, bot)
            scopes, token_user_id = await _get_cached_token_info(session, bot, token)
            token = bot.access_token
            if "moderator:manage:banned_users" not in scopes:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Bot token missing required scope 'moderator:manage:banned_users'. "
                        "Re-run Guided bot setup to refresh OAuth scopes."
                    ),
                )
            if token_user_id and token_user_id != bot.twitch_user_id:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Stored bot token does not belong to this bot account. "
                        "Re-run Guided bot setup and update the bot credentials."
                    ),
                )
        try:
            await twitch_client.moderate_user(
                access_token=token,
                broadcaster_id=broadcaster_user_id,
                moderator_id=bot.twitch_user_id,
                target_user_id=target_user_id,
                duration=None,
                reason=req.reason,
            )
        except Exception as exc:
            await _record_twitch_action(
                service_account_id=service.id,
                direction="outgoing",
                local_transport="twitch_api",
                event_type="twitch.moderation.ban",
                target="helix:/moderation/bans",
                payload={
                    "_action_id": action_id,
                    "_action_status": "failed",
                    "bot_account_id": str(req.bot_account_id),
                    "broadcaster_user_id": broadcaster_user_id,
                    "target_user_id": target_user_id,
                    "duration_ms": int((time.perf_counter() - started) * 1000),
                    "error": str(exc),
                },
            )
            raise HTTPException(status_code=502, detail=f"Failed banning user: {exc}") from exc
        await _record_twitch_action(
            service_account_id=service.id,
            direction="outgoing",
            local_transport="twitch_api",
            event_type="twitch.moderation.ban",
            target="helix:/moderation/bans",
            payload={
                "_action_id": action_id,
                "_action_status": "completed",
                "bot_account_id": str(req.bot_account_id),
                "broadcaster_user_id": broadcaster_user_id,
                "target_user_id": target_user_id,
                "duration_ms": int((time.perf_counter() - started) * 1000),
            },
        )
        return ModerateUserResponse(
            broadcaster_user_id=broadcaster_user_id,
            moderator_user_id=bot.twitch_user_id,
            target_user_id=target_user_id,
            action="ban",
            reason=req.reason,
        )

    @app.post("/v1/twitch/moderation/unban", response_model=ModerateUserResponse)
    async def unban_twitch_user(
        req: ModerateUserRequest,
        service: ServiceAccount = Depends(service_auth),
    ):
        started = time.perf_counter()
        action_id = str(uuid.uuid4())
        broadcaster_user_id = req.broadcaster_user_id.strip()
        target_user_id = req.target_user_id.strip()
        await _record_twitch_action(
            service_account_id=service.id,
            direction="incoming",
            local_transport="service_api",
            event_type="service.twitch.moderation.unban",
            target="/v1/twitch/moderation/unban",
            payload={
                "_action_id": action_id,
                "_action_status": "pending",
                "bot_account_id": str(req.bot_account_id),
                "broadcaster_user_id": broadcaster_user_id,
                "target_user_id": target_user_id,
            },
        )
        async with session_factory() as session:
            await ensure_service_can_access_bot(session, service.id, req.bot_account_id)
            bot = await session.get(BotAccount, req.bot_account_id)
            if not bot:
                raise HTTPException(status_code=404, detail="Bot not found")
            if not bot.enabled:
                raise HTTPException(status_code=409, detail="Bot is disabled")
            token = await ensure_bot_access_token(session, twitch_client, bot)
            scopes, token_user_id = await _get_cached_token_info(session, bot, token)
            token = bot.access_token
            if "moderator:manage:banned_users" not in scopes:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Bot token missing required scope 'moderator:manage:banned_users'. "
                        "Re-run Guided bot setup to refresh OAuth scopes."
                    ),
                )
            if token_user_id and token_user_id != bot.twitch_user_id:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Stored bot token does not belong to this bot account. "
                        "Re-run Guided bot setup and update the bot credentials."
                    ),
                )
        try:
            await twitch_client.unban_user(
                access_token=token,
                broadcaster_id=broadcaster_user_id,
                moderator_id=bot.twitch_user_id,
                target_user_id=target_user_id,
            )
        except Exception as exc:
            await _record_twitch_action(
                service_account_id=service.id,
                direction="outgoing",
                local_transport="twitch_api",
                event_type="twitch.moderation.unban",
                target="helix:/moderation/bans",
                payload={
                    "_action_id": action_id,
                    "_action_status": "failed",
                    "bot_account_id": str(req.bot_account_id),
                    "broadcaster_user_id": broadcaster_user_id,
                    "target_user_id": target_user_id,
                    "duration_ms": int((time.perf_counter() - started) * 1000),
                    "error": str(exc),
                },
            )
            raise HTTPException(status_code=502, detail=f"Failed unbanning user: {exc}") from exc
        await _record_twitch_action(
            service_account_id=service.id,
            direction="outgoing",
            local_transport="twitch_api",
            event_type="twitch.moderation.unban",
            target="helix:/moderation/bans",
            payload={
                "_action_id": action_id,
                "_action_status": "completed",
                "bot_account_id": str(req.bot_account_id),
                "broadcaster_user_id": broadcaster_user_id,
                "target_user_id": target_user_id,
                "duration_ms": int((time.perf_counter() - started) * 1000),
            },
        )
        return ModerateUserResponse(
            broadcaster_user_id=broadcaster_user_id,
            moderator_user_id=bot.twitch_user_id,
            target_user_id=target_user_id,
            action="unban",
        )

    @app.post("/v1/twitch/moderation/delete", response_model=DeleteChatMessageResponse)
    async def delete_twitch_chat_message(
        req: DeleteChatMessageRequest,
        service: ServiceAccount = Depends(service_auth),
    ):
        started = time.perf_counter()
        action_id = str(uuid.uuid4())
        broadcaster_user_id = req.broadcaster_user_id.strip()
        message_id = req.message_id.strip()
        await _record_twitch_action(
            service_account_id=service.id,
            direction="incoming",
            local_transport="service_api",
            event_type="service.twitch.moderation.delete",
            target="/v1/twitch/moderation/delete",
            payload={
                "_action_id": action_id,
                "_action_status": "pending",
                "bot_account_id": str(req.bot_account_id),
                "broadcaster_user_id": broadcaster_user_id,
                "message_id": message_id,
            },
        )
        async with session_factory() as session:
            await ensure_service_can_access_bot(session, service.id, req.bot_account_id)
            bot = await session.get(BotAccount, req.bot_account_id)
            if not bot:
                raise HTTPException(status_code=404, detail="Bot not found")
            if not bot.enabled:
                raise HTTPException(status_code=409, detail="Bot is disabled")
            token = await ensure_bot_access_token(session, twitch_client, bot)
            scopes, token_user_id = await _get_cached_token_info(session, bot, token)
            token = bot.access_token
            if "moderator:manage:chat_messages" not in scopes:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Bot token missing required scope 'moderator:manage:chat_messages'. "
                        "Re-run Guided bot setup to refresh OAuth scopes."
                    ),
                )
            if token_user_id and token_user_id != bot.twitch_user_id:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Stored bot token does not belong to this bot account. "
                        "Re-run Guided bot setup and update the bot credentials."
                    ),
                )
        try:
            await twitch_client.delete_chat_message(
                access_token=token,
                broadcaster_id=broadcaster_user_id,
                moderator_id=bot.twitch_user_id,
                message_id=message_id,
            )
        except Exception as exc:
            await _record_twitch_action(
                service_account_id=service.id,
                direction="outgoing",
                local_transport="twitch_api",
                event_type="twitch.moderation.delete",
                target="helix:/moderation/chat",
                payload={
                    "_action_id": action_id,
                    "_action_status": "failed",
                    "bot_account_id": str(req.bot_account_id),
                    "broadcaster_user_id": broadcaster_user_id,
                    "message_id": message_id,
                    "duration_ms": int((time.perf_counter() - started) * 1000),
                    "error": str(exc),
                },
            )
            raise HTTPException(status_code=502, detail=f"Failed deleting chat message: {exc}") from exc
        await _record_twitch_action(
            service_account_id=service.id,
            direction="outgoing",
            local_transport="twitch_api",
            event_type="twitch.moderation.delete",
            target="helix:/moderation/chat",
            payload={
                "_action_id": action_id,
                "_action_status": "completed",
                "bot_account_id": str(req.bot_account_id),
                "broadcaster_user_id": broadcaster_user_id,
                "message_id": message_id,
                "duration_ms": int((time.perf_counter() - started) * 1000),
            },
        )
        return DeleteChatMessageResponse(
            broadcaster_user_id=broadcaster_user_id,
            moderator_user_id=bot.twitch_user_id,
            message_id=message_id,
        )

    @app.post("/v1/twitch/clips", response_model=CreateClipResponse)
    async def create_twitch_clip(
        req: CreateClipRequest,
        service: ServiceAccount = Depends(service_auth),
    ):
        started = time.perf_counter()
        action_id = str(uuid.uuid4())
        broadcaster_user_id = req.broadcaster_user_id.strip()
        await _record_twitch_action(
            service_account_id=service.id,
            direction="incoming",
            local_transport="service_api",
            event_type="service.twitch.clip.create",
            target="/v1/twitch/clips",
            payload={
                "_action_id": action_id,
                "_action_status": "pending",
                "bot_account_id": str(req.bot_account_id),
                "broadcaster_user_id": broadcaster_user_id,
                "title": req.title,
                "duration": req.duration,
                "has_delay": req.has_delay,
            },
        )
        async with session_factory() as session:
            await ensure_service_can_access_bot(session, service.id, req.bot_account_id)
            bot = await session.get(BotAccount, req.bot_account_id)
            if not bot:
                raise HTTPException(status_code=404, detail="Bot not found")
            if not bot.enabled:
                raise HTTPException(status_code=409, detail="Bot is disabled")
            token = await ensure_bot_access_token(session, twitch_client, bot)
            scopes, token_user_id = await _get_cached_token_info(session, bot, token)
            token = bot.access_token
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
            await _record_twitch_action(
                service_account_id=service.id,
                direction="outgoing",
                local_transport="twitch_api",
                event_type="twitch.clip.create",
                target="helix:/clips",
                payload={
                    "_action_id": action_id,
                    "_action_status": "failed",
                    "bot_account_id": str(req.bot_account_id),
                    "broadcaster_user_id": broadcaster_user_id,
                    "duration_ms": int((time.perf_counter() - started) * 1000),
                    "error": str(exc),
                },
            )
            raise HTTPException(status_code=502, detail=f"Failed creating clip: {exc}") from exc

        clip_id = str(create_payload.get("id", ""))
        if not clip_id:
            raise HTTPException(status_code=502, detail="Clip API returned empty clip id")

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
            result = CreateClipResponse(
                clip_id=clip_id,
                edit_url=str(create_payload.get("edit_url", "")),
                status="processing",
                title=req.title,
                duration=req.duration,
                broadcaster_user_id=broadcaster_user_id,
            )
            logger.info(
                "Clip request completed (processing): service=%s client_id=%s bot=%s broadcaster=%s duration_ms=%d",
                service.id,
                service.client_id,
                req.bot_account_id,
                broadcaster_user_id,
                int((time.perf_counter() - started) * 1000),
            )
            await _record_twitch_action(
                service_account_id=service.id,
                direction="outgoing",
                local_transport="twitch_api",
                event_type="twitch.clip.create",
                target="helix:/clips",
                payload={
                    "_action_id": action_id,
                    "_action_status": "completed",
                    "bot_account_id": str(req.bot_account_id),
                    "broadcaster_user_id": broadcaster_user_id,
                    "clip_id": clip_id,
                    "status": "processing",
                    "duration_ms": int((time.perf_counter() - started) * 1000),
                },
            )
            return result

        result = CreateClipResponse(
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
        logger.info(
            "Clip request completed (ready): service=%s client_id=%s bot=%s broadcaster=%s duration_ms=%d",
            service.id,
            service.client_id,
            req.bot_account_id,
            broadcaster_user_id,
            int((time.perf_counter() - started) * 1000),
        )
        await _record_twitch_action(
            service_account_id=service.id,
            direction="outgoing",
            local_transport="twitch_api",
            event_type="twitch.clip.create",
            target="helix:/clips",
            payload={
                "_action_id": action_id,
                "_action_status": "completed",
                "bot_account_id": str(req.bot_account_id),
                "broadcaster_user_id": broadcaster_user_id,
                "clip_id": clip_id,
                "status": "ready",
                "duration_ms": int((time.perf_counter() - started) * 1000),
                "url": ready_clip.get("url"),
            },
        )
        return result
