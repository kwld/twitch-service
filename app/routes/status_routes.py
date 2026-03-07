from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from sqlalchemy import select

from app.models import (
    BotAccount,
    ChannelState,
    ServiceAccount,
    ServiceBotAccess,
    ServiceEventTrace,
    ServiceInterest,
    ServiceRuntimeStats,
    TwitchSubscription,
)


STATUS_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Twitch Service Status</title>
  <link rel="stylesheet" href="/static/status.css?v=1">
</head>
<body>
  <div id="app" data-status-endpoint="/status" data-status-ws="/ws/status">
    <div class="shell">
      <header class="hero">
        <div>
          <p class="eyebrow">Twitch Service</p>
          <h1>Runtime Status</h1>
          <p class="subline">EventSub, services, subscriptions, startup progress and live operational logs.</p>
        </div>
        <div class="hero-status">
          <span id="connection-pill" class="pill pill-wait">Connecting</span>
          <span id="generated-at" class="hero-meta">waiting for snapshot</span>
        </div>
      </header>

      <section class="grid summary-grid" id="summary-cards"></section>

      <section class="panel">
        <div class="panel-head">
          <h2>Startup Timeline</h2>
          <div id="startup-meta" class="panel-meta"></div>
        </div>
        <div id="startup-phases" class="phase-list"></div>
      </section>

      <section class="grid two-up">
        <section class="panel">
          <div class="panel-head">
            <h2>Service Accounts</h2>
            <div id="services-meta" class="panel-meta"></div>
          </div>
          <div class="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Service</th>
                  <th>State</th>
                  <th>WS</th>
                  <th>Interests</th>
                  <th>Working</th>
                  <th>Events Sent</th>
                  <th>Last Activity</th>
                </tr>
              </thead>
              <tbody id="services-table"></tbody>
            </table>
          </div>
        </section>

        <section class="panel">
          <div class="panel-head">
            <h2>EventSub Snapshot</h2>
            <div id="eventsub-meta" class="panel-meta"></div>
          </div>
          <div id="eventsub-groups" class="compact-list"></div>
        </section>
      </section>

      <section class="grid two-up">
        <section class="panel">
          <div class="panel-head">
            <h2>Broadcaster State</h2>
            <div id="broadcaster-meta" class="panel-meta"></div>
          </div>
          <div class="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Broadcaster</th>
                  <th>Live</th>
                  <th>Title</th>
                  <th>Game</th>
                  <th>Updated</th>
                </tr>
              </thead>
              <tbody id="broadcaster-table"></tbody>
            </table>
          </div>
        </section>

        <section class="panel">
          <div class="panel-head">
            <h2>Recent Logs</h2>
            <div id="logs-meta" class="panel-meta"></div>
          </div>
          <div id="logs-list" class="log-list"></div>
        </section>
      </section>
    </div>
  </div>
  <script src="/static/status.js?v=1" defer></script>
</body>
</html>
"""


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _fmt_dt(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _short_id(value: str | None, *, left: int = 4, right: int = 4) -> str:
    raw = str(value or "").strip()
    if len(raw) <= left + right + 1:
        return raw or "n/a"
    return f"{raw[:left]}...{raw[-right:]}"


def _mask_name(value: str | None) -> str:
    raw = str(value or "").strip()
    if not raw:
        return "unknown"
    if len(raw) <= 4:
        return f"{raw[0]}***"
    return f"{raw[:2]}***{raw[-2:]}"


def _mask_title(value: str | None) -> str:
    raw = str(value or "").strip()
    if not raw:
        return "-"
    if len(raw) <= 18:
        return f"{raw[:8]}…"
    return f"{raw[:12]}…{raw[-4:]}"


def _relative_age(value: datetime | None) -> str:
    if not value:
        return "-"
    delta = _utc_now() - value
    total = max(0, int(delta.total_seconds()))
    if total < 60:
        return f"{total}s ago"
    if total < 3600:
        return f"{total // 60}m ago"
    return f"{total // 3600}h ago"


def _group_counts(rows: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    counts: dict[str, int] = {}
    for row in rows:
        name = str(row.get(key, "unknown") or "unknown")
        counts[name] = counts.get(name, 0) + 1
    return [
        {"label": label, "count": count}
        for label, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    ]


def register_status_routes(
    app: FastAPI,
    *,
    settings,
    logger,
    session_factory,
    eventsub_manager,
    status_runtime,
    resolve_client_ip,
    is_ip_allowed,
    filter_working_interests,
) -> None:
    snapshot_cache: dict[str, Any] = {"generated_at": None, "data": None}
    snapshot_cache_ttl = 2.0
    snapshot_lock = asyncio.Lock()

    async def build_status_snapshot() -> dict[str, Any]:
        now = _utc_now()
        async with session_factory() as session:
            services = list((await session.scalars(select(ServiceAccount))).all())
            stats_rows = list((await session.scalars(select(ServiceRuntimeStats))).all())
            interests = list((await session.scalars(select(ServiceInterest))).all())
            working_interests = await filter_working_interests(session, interests)
            bot_rows = list((await session.scalars(select(BotAccount))).all())
            access_rows = list((await session.scalars(select(ServiceBotAccess))).all())
            channel_states = list((await session.scalars(select(ChannelState))).all())
            traces = list(
                (
                    await session.scalars(
                        select(ServiceEventTrace).order_by(ServiceEventTrace.created_at.desc()).limit(40)
                    )
                ).all()
            )
            sub_rows = list((await session.scalars(select(TwitchSubscription))).all())

        stats_by_service = {row.service_account_id: row for row in stats_rows}
        working_by_service: dict[str, int] = {}
        interests_by_service: dict[str, int] = {}
        access_by_service: dict[str, int] = {}
        subs_by_bot: dict[str, list[TwitchSubscription]] = {}

        for row in interests:
            key = str(row.service_account_id)
            interests_by_service[key] = interests_by_service.get(key, 0) + 1
        for row in working_interests:
            key = str(row.service_account_id)
            working_by_service[key] = working_by_service.get(key, 0) + 1
        for row in access_rows:
            key = str(row.service_account_id)
            access_by_service[key] = access_by_service.get(key, 0) + 1
        for row in sub_rows:
            key = str(row.bot_account_id)
            subs_by_bot.setdefault(key, []).append(row)

        active_snapshot, active_cached_at = await eventsub_manager.get_db_active_subscriptions_snapshot()
        eventsub_summary = await eventsub_manager.get_status_summary()

        service_rows = []
        for service in services:
            stats = stats_by_service.get(service.id)
            service_rows.append(
                {
                    "id": str(service.id),
                    "name": service.name,
                    "enabled": service.enabled,
                    "client_id_masked": _short_id(service.client_id),
                    "is_connected": bool(stats.is_connected) if stats else False,
                    "active_ws_connections": int(stats.active_ws_connections or 0) if stats else 0,
                    "total_api_requests": int(stats.total_api_requests or 0) if stats else 0,
                    "total_events_sent": int((stats.total_events_sent_ws or 0) + (stats.total_events_sent_webhook or 0)) if stats else 0,
                    "interests_total": interests_by_service.get(str(service.id), 0),
                    "working_interests": working_by_service.get(str(service.id), 0),
                    "bot_access_count": access_by_service.get(str(service.id), 0),
                    "last_connected_at": _fmt_dt(stats.last_connected_at if stats else None),
                    "last_disconnected_at": _fmt_dt(stats.last_disconnected_at if stats else None),
                    "last_api_request_at": _fmt_dt(stats.last_api_request_at if stats else None),
                    "last_event_sent_at": _fmt_dt(stats.last_event_sent_at if stats else None),
                    "last_activity_human": _relative_age(
                        max(
                            [dt for dt in [
                                stats.last_api_request_at if stats else None,
                                stats.last_event_sent_at if stats else None,
                                stats.last_connected_at if stats else None,
                            ] if dt],
                            default=None,
                        )
                    ),
                }
            )

        broadcaster_rows = []
        for state in sorted(channel_states, key=lambda item: (not item.is_live, item.broadcaster_user_id))[:30]:
            broadcaster_rows.append(
                {
                    "broadcaster_user_id_masked": _short_id(state.broadcaster_user_id),
                    "broadcaster_label": f"chan:{_mask_name(state.broadcaster_user_id)}",
                    "is_live": bool(state.is_live),
                    "title_masked": _mask_title(state.title),
                    "game_name": state.game_name or "-",
                    "last_checked_at": _fmt_dt(state.last_checked_at),
                    "last_checked_human": _relative_age(state.last_checked_at),
                }
            )

        recent_trace_rows = []
        for trace in traces[:20]:
            recent_trace_rows.append(
                {
                    "timestamp": _fmt_dt(trace.created_at),
                    "service_account_id": _short_id(str(trace.service_account_id)),
                    "direction": trace.direction,
                    "transport": trace.local_transport,
                    "event_type": trace.event_type,
                    "target": trace.target,
                }
            )

        bot_summary = []
        for bot in bot_rows:
            bot_subs = subs_by_bot.get(str(bot.id), [])
            bot_summary.append(
                {
                    "bot_account_id": str(bot.id),
                    "bot_name": bot.name,
                    "twitch_login_masked": _mask_name(bot.twitch_login),
                    "enabled": bot.enabled,
                    "subscription_count": len(bot_subs),
                    "enabled_subscription_count": sum(1 for row in bot_subs if str(row.status).startswith("enabled")),
                }
            )

        summary_cards = [
            {"label": "Service Accounts", "value": len(services), "tone": "neutral"},
            {
                "label": "Connected Services",
                "value": sum(1 for row in service_rows if row["is_connected"]),
                "tone": "good",
            },
            {
                "label": "Working Interests",
                "value": len(working_interests),
                "tone": "good" if working_interests else "warn",
            },
            {
                "label": "Active EventSub Rows",
                "value": len(active_snapshot),
                "tone": "neutral",
            },
            {
                "label": "Live Channels",
                "value": sum(1 for row in channel_states if row.is_live),
                "tone": "good",
            },
            {
                "label": "Recent Logs",
                "value": len(status_runtime.get_recent_logs(80)),
                "tone": "neutral",
            },
        ]

        return {
            "schema_version": "twitch-service-status.v1",
            "generated_at": now.isoformat(),
            "app": {
                "env": settings.app_env,
                "uptime_seconds": int((now - status_runtime.started_at).total_seconds()),
                "started_at": status_runtime.started_at.isoformat(),
                "ip_allowlist_enabled": bool(settings.app_allowed_ips),
            },
            "summary_cards": summary_cards,
            "eventsub": {
                **eventsub_summary,
                "active_snapshot_cached_at": active_cached_at.isoformat(),
                "active_snapshot_total": len(active_snapshot),
                "active_snapshot_by_status": _group_counts(active_snapshot, "status"),
                "active_snapshot_by_transport": _group_counts(active_snapshot, "upstream_transport"),
                "active_snapshot_sample": [
                    {
                        "subscription_id": _short_id(str(row.get("twitch_subscription_id"))),
                        "status": str(row.get("status", "unknown")),
                        "event_type": str(row.get("event_type", "")),
                        "broadcaster_masked": _short_id(str(row.get("broadcaster_user_id", ""))),
                        "transport": str(row.get("upstream_transport", "")),
                        "session_id_masked": _short_id(str(row.get("session_id") or "")),
                    }
                    for row in active_snapshot[:16]
                ],
            },
            "services": {
                "rows": service_rows,
                "recent_traces": recent_trace_rows,
            },
            "bots": bot_summary,
            "broadcasters": broadcaster_rows,
            "logs": status_runtime.get_recent_logs(80),
        }

    async def get_cached_snapshot(force: bool = False) -> dict[str, Any]:
        async with snapshot_lock:
            generated_at = snapshot_cache.get("generated_at")
            if (
                not force
                and generated_at
                and (_utc_now() - generated_at).total_seconds() < snapshot_cache_ttl
                and snapshot_cache.get("data") is not None
            ):
                return snapshot_cache["data"]
            data = await build_status_snapshot()
            snapshot_cache["generated_at"] = _utc_now()
            snapshot_cache["data"] = data
            return data

    @app.get("/status", include_in_schema=False)
    async def status_page():
        return HTMLResponse(STATUS_HTML)

    @app.post("/status", include_in_schema=False)
    async def status_json():
        return await get_cached_snapshot(force=True)

    @app.websocket("/ws/status")
    async def status_ws(websocket: WebSocket):
        client_ip = resolve_client_ip(
            websocket.client.host if websocket.client else None,
            websocket.headers.get("x-forwarded-for"),
            trust_x_forwarded_for=settings.app_trust_x_forwarded_for,
        )
        if not is_ip_allowed(client_ip):
            logger.warning("Blocked status websocket connection from IP %s", client_ip or "unknown")
            await websocket.close(code=4403)
            return
        await status_runtime.connect(websocket)
        last_hash = ""
        last_sent_at = 0.0
        try:
            while True:
                snapshot = await get_cached_snapshot(force=True)
                payload_hash = json.dumps(snapshot, sort_keys=True, default=str)
                now = asyncio.get_running_loop().time()
                if payload_hash != last_hash or (now - last_sent_at) >= 10.0:
                    await websocket.send_text(
                        json.dumps(
                            {
                                "type": "status_snapshot",
                                "generated_at": snapshot.get("generated_at"),
                                "payload": snapshot,
                            },
                            default=str,
                        )
                    )
                    last_hash = payload_hash
                    last_sent_at = now
                await asyncio.sleep(1.5)
        except WebSocketDisconnect:
            pass
        finally:
            await status_runtime.disconnect(websocket)
