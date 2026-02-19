from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Awaitable, Callable

from prompt_toolkit import PromptSession
from sqlalchemy import func, select

from app.models import (
    BotAccount,
    BroadcasterAuthorization,
    ChannelState,
    ServiceAccount,
    ServiceEventTrace,
    ServiceInterest,
    ServiceRuntimeStats,
)


def format_duration_short(duration: timedelta) -> str:
    total_seconds = max(0, int(duration.total_seconds()))
    minutes, seconds = divmod(total_seconds, 60)
    return f"{minutes}m{seconds:02d}s"


async def websocket_listener_cooldown_remaining_cli(session_factory) -> timedelta | None:
    async with session_factory() as db:
        active = await db.scalar(select(func.coalesce(func.sum(ServiceRuntimeStats.active_ws_connections), 0)))
        latest_disconnect = await db.scalar(select(func.max(ServiceRuntimeStats.last_disconnected_at)))
    if (active or 0) > 0:
        return None
    now = datetime.now(UTC)
    cooldown = timedelta(minutes=5)
    baseline = latest_disconnect or now
    elapsed = now - baseline
    return cooldown - elapsed


async def list_service_status_menu(session_factory) -> None:
    async with session_factory() as db:
        services = list((await db.scalars(select(ServiceAccount))).all())
        if not services:
            print("No service accounts.")
            return
        runtime_by_id = {
            s.service_account_id: s for s in list((await db.scalars(select(ServiceRuntimeStats))).all())
        }
        counts = (
            await db.execute(
                select(
                    ServiceInterest.service_account_id,
                    func.count(ServiceInterest.id),
                ).group_by(ServiceInterest.service_account_id)
            )
        ).all()
        interest_count_by_id = {sid: cnt for sid, cnt in counts}

    cooldown_remaining = await websocket_listener_cooldown_remaining_cli(session_factory)
    if cooldown_remaining is None:
        print("\nService WS listener cooldown: active listeners connected")
    else:
        print(
            "\nService WS listener cooldown remaining: "
            f"{format_duration_short(cooldown_remaining)}"
        )

    print("\nService Status and Usage:")
    for svc in services:
        stats = runtime_by_id.get(svc.id)
        interest_count = interest_count_by_id.get(svc.id, 0)
        if not stats:
            print(
                f"- {svc.name} client_id={svc.client_id} enabled={svc.enabled} "
                f"connected=False active_ws=0 interests={interest_count} api_requests=0 ws_events=0 webhook_events=0"
            )
            continue
        print(
            f"- {svc.name} client_id={svc.client_id} enabled={svc.enabled} "
            f"connected={stats.is_connected} active_ws={stats.active_ws_connections} "
            f"interests={interest_count} api_requests={stats.total_api_requests} "
            f"ws_events={stats.total_events_sent_ws} webhook_events={stats.total_events_sent_webhook} "
            f"last_connected={stats.last_connected_at} last_disconnected={stats.last_disconnected_at} "
            f"last_api={stats.last_api_request_at} last_event={stats.last_event_sent_at}"
        )


async def list_broadcaster_authorizations_menu(session_factory) -> None:
    async with session_factory() as db:
        auths = list((await db.scalars(select(BroadcasterAuthorization))).all())
        if not auths:
            print("No broadcaster authorizations recorded.")
            return
        services = list((await db.scalars(select(ServiceAccount))).all())
        bots = list((await db.scalars(select(BotAccount))).all())
    service_name_by_id = {s.id: s.name for s in services}
    bot_name_by_id = {b.id: b.name for b in bots}
    print("\nBroadcaster Authorizations:")
    for auth in auths:
        scopes = [x for x in auth.scopes_csv.split(",") if x]
        print(
            f"- service={service_name_by_id.get(auth.service_account_id, str(auth.service_account_id))} "
            f"bot={bot_name_by_id.get(auth.bot_account_id, str(auth.bot_account_id))} "
            f"broadcaster={auth.broadcaster_login}/{auth.broadcaster_user_id} "
            f"authorized_at={auth.authorized_at} scopes={','.join(scopes)}"
        )


async def list_tracked_channels_menu(
    session: PromptSession,
    session_factory,
    ask_yes_no_fn: Callable[[PromptSession, str, bool], Awaitable[bool]],
) -> None:
    only_live = await ask_yes_no_fn(session, "Show only live channels?", default_yes=False)
    raw_limit = (await session.prompt_async("Limit [200]: ")).strip()
    try:
        limit = int(raw_limit) if raw_limit else 200
    except ValueError:
        print("Invalid limit; using 200.")
        limit = 200
    limit = max(1, min(limit, 5000))

    async with session_factory() as db:
        bots = list((await db.scalars(select(BotAccount))).all())
        bot_name_by_id = {b.id: b.name for b in bots}
        states = list((await db.scalars(select(ChannelState))).all())

    if only_live:
        states = [s for s in states if s.is_live]

    def _sort_key(row: ChannelState):
        # live first; then latest checked first (None last)
        ts = row.last_checked_at.timestamp() if row.last_checked_at else 0.0
        return (not row.is_live, -ts, str(row.bot_account_id), row.broadcaster_user_id)

    states = sorted(states, key=_sort_key)[:limit]
    live_count = sum(1 for s in states if s.is_live)

    print(
        f"\nTracked channels (channel_states): showing={len(states)} live={live_count} "
        f"filter={'live_only' if only_live else 'all'}"
    )
    if not states:
        return
    for state_row in states:
        bot_name = bot_name_by_id.get(state_row.bot_account_id, str(state_row.bot_account_id))
        started = state_row.started_at.isoformat() if state_row.started_at else "-"
        checked = state_row.last_checked_at.isoformat() if state_row.last_checked_at else "-"
        title = (state_row.title or "").replace("\n", " ").strip()
        game = (state_row.game_name or "").replace("\n", " ").strip()
        if title:
            title = title[:120]
        if game:
            game = game[:60]
        print(
            f"- bot={bot_name} broadcaster={state_row.broadcaster_user_id} live={state_row.is_live} "
            f"started_at={started} last_checked_at={checked} "
            f"title={title or '-'} game={game or '-'}"
        )


def format_trace_payload(payload_json: str, max_chars: int = 8000) -> str:
    text = payload_json or "{}"
    if len(text) > max_chars:
        text = text[:max_chars] + "... [truncated]"
    try:
        parsed = json.loads(text)
        return json.dumps(parsed, indent=2, ensure_ascii=False)
    except Exception:
        return text


async def live_service_event_tracking_menu(
    session: PromptSession,
    session_factory,
    select_service_account_fn: Callable[[PromptSession, object], Awaitable[ServiceAccount | None]],
) -> None:
    service = await select_service_account_fn(session, session_factory)
    if not service:
        return
    raw_limit = (await session.prompt_async("Recent buffer size [200]: ")).strip()
    raw_poll = (await session.prompt_async("Poll interval seconds [1.0]: ")).strip()
    try:
        limit = int(raw_limit) if raw_limit else 200
    except ValueError:
        limit = 200
    try:
        poll_seconds = float(raw_poll) if raw_poll else 1.0
    except ValueError:
        poll_seconds = 1.0
    limit = max(10, min(limit, 2000))
    poll_seconds = max(0.2, min(poll_seconds, 10.0))

    print(
        "\nLive service communication tracking\n"
        f"- service={service.name} client_id={service.client_id}\n"
        f"- showing incoming/outgoing event traces (payload already redacted)\n"
        "- press Ctrl+C to stop\n"
    )
    seen: set[str] = set()
    try:
        while True:
            async with session_factory() as db:
                rows = list(
                    (
                        await db.scalars(
                            select(ServiceEventTrace)
                            .where(ServiceEventTrace.service_account_id == service.id)
                            .order_by(ServiceEventTrace.created_at.desc())
                            .limit(limit)
                        )
                    ).all()
                )
            rows = list(reversed(rows))
            fresh = [row for row in rows if str(row.id) not in seen]
            for row in fresh:
                seen.add(str(row.id))
                print(
                    f"[{row.created_at.isoformat()}] direction={row.direction} "
                    f"transport={row.local_transport} event={row.event_type} target={row.target or '-'}"
                )
                print(format_trace_payload(row.payload_json))
                print("-" * 80)
            if len(seen) > 5000:
                seen = set(list(seen)[-3000:])
            await asyncio.sleep(poll_seconds)
    except KeyboardInterrupt:
        print("\nLive tracking stopped.")
