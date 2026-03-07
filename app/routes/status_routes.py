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

      <nav class="tab-bar" id="status-tabs">
        <button class="tab-button is-active" type="button" data-tab-target="overview">Overview</button>
        <button class="tab-button" type="button" data-tab-target="broadcasters">Broadcasters</button>
        <button class="tab-button" type="button" data-tab-target="events">Events</button>
        <button class="tab-button" type="button" data-tab-target="logs">Logs</button>
      </nav>

      <section class="tab-panel is-active" data-tab-panel="overview">
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
            <div id="eventsub-counters" class="summary-grid mini-summary-grid"></div>
            <div class="event-toolbar">
              <input id="eventsub-filter-text" class="field-input" type="search" placeholder="Filter by event, service, bot, broadcaster">
              <select id="eventsub-filter-service" class="field-input">
                <option value="">All services</option>
              </select>
              <select id="eventsub-filter-bot" class="field-input">
                <option value="">All bot accounts</option>
              </select>
              <select id="eventsub-page-size" class="field-input">
                <option value="10">10 / page</option>
                <option value="25" selected>25 / page</option>
                <option value="50">50 / page</option>
              </select>
            </div>
            <div id="eventsub-pagination" class="pagination-bar"></div>
            <div class="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Event</th>
                    <th>Broadcaster</th>
                    <th>Bot</th>
                    <th>Service</th>
                    <th>Transport</th>
                    <th>Status</th>
                    <th>Cost</th>
                    <th>Session</th>
                  </tr>
                </thead>
                <tbody id="eventsub-table"></tbody>
              </table>
            </div>
          </section>
        </section>

        <section class="panel">
          <div class="panel-head">
            <h2>Bot Accounts</h2>
            <div id="bots-meta" class="panel-meta"></div>
          </div>
          <div class="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Bot</th>
                  <th>State</th>
                  <th>Channels</th>
                  <th>Subscriptions</th>
                  <th>Enabled Subs</th>
                  <th>EventSub Cost</th>
                </tr>
              </thead>
              <tbody id="bots-table"></tbody>
            </table>
          </div>
        </section>
      </section>

      <section class="tab-panel" data-tab-panel="broadcasters">
        <section class="panel">
          <div class="panel-head">
            <h2>Broadcaster State</h2>
            <div id="broadcaster-meta" class="panel-meta"></div>
          </div>
          <div class="event-toolbar">
            <select id="broadcaster-filter-bot" class="field-input">
              <option value="">All bot accounts</option>
            </select>
          </div>
          <div class="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Broadcaster</th>
                  <th>Bot</th>
                  <th>Live</th>
                  <th>In</th>
                  <th>Out</th>
                  <th>EventSub</th>
                  <th>Title</th>
                  <th>Game</th>
                  <th>Updated</th>
                </tr>
              </thead>
              <tbody id="broadcaster-table"></tbody>
            </table>
          </div>
        </section>
      </section>

      <section class="tab-panel" data-tab-panel="events">
        <section class="panel">
          <div class="panel-head">
            <h2>Live Event Feed</h2>
            <div class="panel-actions">
              <div id="events-meta" class="panel-meta"></div>
              <button id="events-pause-toggle" class="ghost-button" type="button">Pause</button>
            </div>
          </div>
          <div class="event-toolbar">
            <input id="events-filter-text" class="field-input" type="search" placeholder="Filter by event, service, broadcaster, target">
            <select id="events-filter-direction" class="field-input">
              <option value="">All directions</option>
              <option value="incoming">Incoming</option>
              <option value="outgoing">Outgoing</option>
            </select>
            <select id="events-filter-origin" class="field-input">
              <option value="">All origins</option>
              <option value="twitch">From Twitch</option>
              <option value="service">From Service</option>
              <option value="websocket">Via Service WebSocket</option>
              <option value="webhook">Via Webhook</option>
            </select>
            <select id="events-filter-service" class="field-input">
              <option value="">All services</option>
            </select>
            <select id="events-filter-bot" class="field-input">
              <option value="">All bot accounts</option>
            </select>
            <select id="events-page-size" class="field-input">
              <option value="10">10 / page</option>
              <option value="25" selected>25 / page</option>
              <option value="50">50 / page</option>
            </select>
          </div>
          <div id="events-pagination" class="pagination-bar"></div>
          <div id="events-list" class="event-list"></div>
        </section>
      </section>

      <section class="tab-panel" data-tab-panel="logs">
        <section class="panel">
          <div class="panel-head">
            <h2>Recent Logs</h2>
            <div id="logs-meta" class="panel-meta"></div>
          </div>
          <div class="event-toolbar">
            <select id="logs-filter-bot" class="field-input">
              <option value="">All bot accounts</option>
            </select>
          </div>
          <div id="logs-list" class="log-list"></div>
        </section>
      </section>
    </div>
  </div>
  <div id="events-modal" class="modal-shell hidden" aria-hidden="true">
    <div id="events-modal-backdrop" class="modal-backdrop"></div>
    <section class="modal-panel" role="dialog" aria-modal="true" aria-labelledby="events-modal-title">
      <div class="modal-head">
        <div>
          <p class="eyebrow">Broadcaster EventSub</p>
          <h2 id="events-modal-title">Attached Event Types</h2>
        </div>
        <button id="events-modal-close" class="ghost-button" type="button">Close</button>
      </div>
      <div class="modal-meta">
        <strong id="events-modal-label">chan:unknown</strong>
        <span id="events-modal-id" class="muted mono">n/a</span>
      </div>
      <div id="events-modal-list" class="compact-list"></div>
    </section>
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


def _mask_id(value: str | None, *, left: int = 2, right: int = 2) -> str:
    raw = str(value or "").strip()
    if not raw:
        return "n/a"
    if len(raw) <= left + right:
        if len(raw) <= 2:
            return "*" * len(raw)
        return f"{raw[:1]}***{raw[-1:]}"
    return f"{raw[:left]}***{raw[-right:]}"


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


def _safe_json_loads(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _trace_event_payload(trace: ServiceEventTrace) -> dict[str, Any]:
    payload = _safe_json_loads(trace.payload_json)
    event = payload.get("event")
    if isinstance(event, dict):
        return event
    return payload


def _trace_broadcaster_user_id(trace: ServiceEventTrace) -> str | None:
    event = _trace_event_payload(trace)
    raw = event.get("broadcaster_user_id")
    if raw is None:
        subscription = _safe_json_loads(trace.payload_json).get("subscription")
        if isinstance(subscription, dict):
            condition = subscription.get("condition")
            if isinstance(condition, dict):
                raw = condition.get("broadcaster_user_id")
    value = str(raw or "").strip()
    return value or None


def _trace_broadcaster_login(trace: ServiceEventTrace) -> str | None:
    event = _trace_event_payload(trace)
    for key in ("broadcaster_user_login", "broadcaster_login", "broadcaster"):
        value = str(event.get(key, "")).strip()
        if value:
            return value
    return None


def _format_trace_body(payload_json: str | None) -> str:
    payload = _safe_json_loads(payload_json)
    if not payload:
        return "{}"
    try:
        return json.dumps(payload, indent=2, ensure_ascii=True, sort_keys=True)
    except Exception:
        return str(payload_json or "{}")


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


def _find_bot_for_message(message: str, bots: list[dict[str, str]]) -> dict[str, str] | None:
    text = str(message or "")
    lower = text.lower()
    for bot in bots:
        bot_id = bot.get("id", "")
        bot_name = bot.get("name", "")
        bot_login = bot.get("login", "")
        if bot_id and bot_id in text:
            return bot
        if bot_name and bot_name.lower() in lower:
            return bot
        if bot_login and bot_login.lower() in lower:
            return bot
    return None


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
                        select(ServiceEventTrace).order_by(ServiceEventTrace.created_at.desc()).limit(400)
                    )
                ).all()
            )
            sub_rows = list((await session.scalars(select(TwitchSubscription))).all())

        stats_by_service = {row.service_account_id: row for row in stats_rows}
        service_name_by_id = {str(service.id): service.name for service in services}
        bot_by_id = {str(bot.id): bot for bot in bot_rows}
        bot_meta_rows = [
            {
                "id": str(bot.id),
                "name": bot.name,
                "login": bot.twitch_login,
                "name_masked": _mask_name(bot.name),
                "login_masked": _mask_name(bot.twitch_login),
                "twitch_user_id_masked": _mask_id(bot.twitch_user_id),
                "enabled": bool(bot.enabled),
            }
            for bot in bot_rows
        ]
        working_by_service: dict[str, int] = {}
        interests_by_service: dict[str, int] = {}
        access_by_service: dict[str, int] = {}
        subs_by_bot: dict[str, list[TwitchSubscription]] = {}
        channel_count_by_bot: dict[str, int] = {}
        bot_ids_by_broadcaster: dict[str, set[str]] = {}
        service_names_by_eventsub_key: dict[tuple[str, str, str], set[str]] = {}

        for row in interests:
            key = str(row.service_account_id)
            interests_by_service[key] = interests_by_service.get(key, 0) + 1
            eventsub_key = (
                str(row.broadcaster_user_id).strip(),
                str(row.event_type).strip(),
                str(row.bot_account_id),
            )
            service_name = service_name_by_id.get(str(row.service_account_id))
            if service_name:
                service_names_by_eventsub_key.setdefault(eventsub_key, set()).add(service_name)
        for row in working_interests:
            key = str(row.service_account_id)
            working_by_service[key] = working_by_service.get(key, 0) + 1
            eventsub_key = (
                str(row.broadcaster_user_id).strip(),
                str(row.event_type).strip(),
                str(row.bot_account_id),
            )
            service_name = service_name_by_id.get(str(row.service_account_id))
            if service_name:
                service_names_by_eventsub_key.setdefault(eventsub_key, set()).add(service_name)
        for row in access_rows:
            key = str(row.service_account_id)
            access_by_service[key] = access_by_service.get(key, 0) + 1
        for row in sub_rows:
            key = str(row.bot_account_id)
            subs_by_bot.setdefault(key, []).append(row)
            bot_ids_by_broadcaster.setdefault(str(row.broadcaster_user_id), set()).add(key)
        for state in channel_states:
            key = str(state.bot_account_id)
            channel_count_by_bot[key] = channel_count_by_bot.get(key, 0) + 1
            bot_ids_by_broadcaster.setdefault(str(state.broadcaster_user_id), set()).add(key)

        try:
            active_snapshot, active_cached_at, active_snapshot_from_cache = await eventsub_manager.get_active_subscriptions_snapshot(
                force_refresh=False
            )
            active_snapshot_source = "live-cache" if active_snapshot_from_cache else "live"
        except Exception:
            active_snapshot, active_cached_at = await eventsub_manager.get_db_active_subscriptions_snapshot()
            active_snapshot_source = "db-fallback"
        eventsub_summary = await eventsub_manager.get_status_summary()

        broadcaster_names: dict[str, str] = {}
        message_counts_by_broadcaster: dict[str, dict[str, int]] = {}
        for trace in traces:
            broadcaster_user_id = _trace_broadcaster_user_id(trace)
            if not broadcaster_user_id:
                continue
            broadcaster_login = _trace_broadcaster_login(trace)
            if broadcaster_login and broadcaster_user_id not in broadcaster_names:
                broadcaster_names[broadcaster_user_id] = broadcaster_login
            if trace.event_type != "channel.chat.message":
                continue
            counters = message_counts_by_broadcaster.setdefault(
                broadcaster_user_id,
                {"messages_received": 0, "messages_sent": 0},
            )
            if trace.direction == "incoming":
                counters["messages_received"] += 1
            elif trace.direction == "outgoing":
                counters["messages_sent"] += 1

        eventsub_names_by_broadcaster: dict[str, set[str]] = {}
        eventsub_cost_by_bot: dict[str, int] = {}
        eventsub_max_cost_by_bot: dict[str, int] = {
            str(key): int(value or 0)
            for key, value in (eventsub_summary.get("active_snapshot_max_cost_by_bot") or {}).items()
        }
        for row in active_snapshot:
            broadcaster_user_id = str(row.get("broadcaster_user_id", "")).strip()
            event_type = str(row.get("event_type", "")).strip()
            bot_account_id = str(row.get("bot_account_id", "")).strip()
            cost = int(row.get("cost", 0) or 0)
            if broadcaster_user_id and event_type:
                eventsub_names_by_broadcaster.setdefault(broadcaster_user_id, set()).add(event_type)
            if bot_account_id:
                eventsub_cost_by_bot[bot_account_id] = eventsub_cost_by_bot.get(bot_account_id, 0) + cost
        for row in working_interests:
            broadcaster_user_id = str(row.broadcaster_user_id).strip()
            event_type = str(row.event_type).strip()
            if broadcaster_user_id and event_type:
                eventsub_names_by_broadcaster.setdefault(broadcaster_user_id, set()).add(event_type)
                bot_ids_by_broadcaster.setdefault(broadcaster_user_id, set()).add(str(row.bot_account_id))

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
            broadcaster_user_id = str(state.broadcaster_user_id).strip()
            broadcaster_login = broadcaster_names.get(broadcaster_user_id)
            eventsub_names = sorted(eventsub_names_by_broadcaster.get(broadcaster_user_id, set()))
            message_counts = message_counts_by_broadcaster.get(
                broadcaster_user_id,
                {"messages_received": 0, "messages_sent": 0},
            )
            bot = bot_by_id.get(str(state.bot_account_id))
            broadcaster_rows.append(
                {
                    "broadcaster_user_id_masked": _mask_id(broadcaster_user_id),
                    "broadcaster_label": f"chan:{_mask_name(broadcaster_login or broadcaster_user_id)}",
                    "bot_account_id": str(state.bot_account_id),
                    "bot_account_id_masked": _short_id(str(state.bot_account_id)),
                    "bot_name": bot.name if bot else "unknown",
                    "bot_name_masked": _mask_name(bot.name if bot else "unknown"),
                    "bot_login_masked": _mask_name(bot.twitch_login if bot else "unknown"),
                    "is_live": bool(state.is_live),
                    "title_masked": _mask_title(state.title),
                    "game_name": state.game_name or "-",
                    "last_checked_at": _fmt_dt(state.last_checked_at),
                    "last_checked_human": _relative_age(state.last_checked_at),
                    "messages_received": int(message_counts["messages_received"]),
                    "messages_sent": int(message_counts["messages_sent"]),
                    "eventsub_count": len(eventsub_names),
                    "eventsub_names": eventsub_names,
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

        recent_event_rows = []
        for trace in traces[:80]:
            broadcaster_user_id = _trace_broadcaster_user_id(trace)
            broadcaster_login = _trace_broadcaster_login(trace)
            candidate_bot_ids = sorted(bot_ids_by_broadcaster.get(str(broadcaster_user_id or ""), set()))
            bot = bot_by_id.get(candidate_bot_ids[0]) if len(candidate_bot_ids) == 1 else None
            recent_event_rows.append(
                {
                    "timestamp": _fmt_dt(trace.created_at),
                    "service_name": service_name_by_id.get(str(trace.service_account_id), "unknown"),
                    "service_account_id_masked": _short_id(str(trace.service_account_id)),
                    "direction": trace.direction,
                    "transport": trace.local_transport,
                    "event_type": trace.event_type,
                    "target": trace.target or "-",
                    "broadcaster_label": f"chan:{_mask_name(broadcaster_login or broadcaster_user_id)}"
                    if (broadcaster_login or broadcaster_user_id)
                    else "chan:unknown",
                    "broadcaster_user_id_masked": _mask_id(broadcaster_user_id),
                    "bot_account_id": str(bot.id) if bot else "",
                    "bot_account_id_masked": _short_id(str(bot.id)) if bot else ("multiple" if len(candidate_bot_ids) > 1 else "n/a"),
                    "bot_name": bot.name if bot else ("multiple" if len(candidate_bot_ids) > 1 else "unknown"),
                    "bot_name_masked": _mask_name(bot.name) if bot else ("multiple" if len(candidate_bot_ids) > 1 else "unknown"),
                    "body_pretty": _format_trace_body(trace.payload_json),
                }
            )

        bot_summary = []
        for bot in bot_rows:
            bot_subs = subs_by_bot.get(str(bot.id), [])
            bot_summary.append(
                {
                    "bot_account_id": str(bot.id),
                    "bot_name": bot.name,
                    "bot_name_masked": _mask_name(bot.name),
                    "twitch_login_masked": _mask_name(bot.twitch_login),
                    "enabled": bot.enabled,
                    "channel_count": channel_count_by_bot.get(str(bot.id), 0),
                    "subscription_count": len(bot_subs),
                    "enabled_subscription_count": sum(1 for row in bot_subs if str(row.status).startswith("enabled")),
                    "eventsub_cost_total": eventsub_cost_by_bot.get(str(bot.id), 0),
                    "eventsub_cost_max": eventsub_max_cost_by_bot.get(
                        str(bot.id),
                        int(eventsub_summary.get("active_snapshot_max_total_cost", 0) or 0),
                    ),
                }
            )

        recent_logs = []
        for row in status_runtime.get_recent_logs(80):
            bot_match = _find_bot_for_message(
                str(row.get("message", "")),
                bot_meta_rows,
            )
            recent_logs.append(
                {
                    **row,
                    "bot_account_id": bot_match.get("id", "") if bot_match else "",
                    "bot_account_id_masked": _short_id(bot_match.get("id", "")) if bot_match else "n/a",
                    "bot_name": bot_match.get("name", "") if bot_match else "",
                    "bot_name_masked": bot_match.get("name_masked", "unknown") if bot_match else "unknown",
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
                "value": len(recent_logs),
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
                "active_snapshot_source": active_snapshot_source,
                "active_snapshot_total": len(active_snapshot),
                "active_snapshot_cost_total": sum(int(row.get("cost", 0) or 0) for row in active_snapshot),
                "active_snapshot_max_total_cost": int(eventsub_summary.get("active_snapshot_max_total_cost", 0) or 0),
                "active_snapshot_by_status": _group_counts(active_snapshot, "status"),
                "active_snapshot_by_transport": _group_counts(active_snapshot, "upstream_transport"),
                "active_snapshot_rows": [
                    {
                        "subscription_id": _short_id(str(row.get("twitch_subscription_id"))),
                        "subscription_id_full": str(row.get("twitch_subscription_id", "")),
                        "status": str(row.get("status", "unknown")),
                        "cost": int(row.get("cost", 0) or 0),
                        "event_type": str(row.get("event_type", "")),
                        "broadcaster_masked": _short_id(str(row.get("broadcaster_user_id", ""))),
                        "broadcaster_user_id_masked": _mask_id(str(row.get("broadcaster_user_id", ""))),
                        "bot_account_id_masked": _short_id(str(row.get("bot_account_id", ""))),
                        "bot_account_id": str(row.get("bot_account_id", "")),
                        "bot_name_masked": _mask_name(bot_by_id.get(str(row.get("bot_account_id", ""))).name)
                        if bot_by_id.get(str(row.get("bot_account_id", "")))
                        else "unknown",
                        "transport": str(row.get("upstream_transport", "")),
                        "session_id_masked": _short_id(str(row.get("session_id") or "")),
                        "service_names": service_names,
                        "service_count": len(service_names),
                        "service_names_display": ", ".join(service_names) if service_names else "none",
                    }
                    for row in active_snapshot
                    for service_names in [
                        sorted(
                            service_names_by_eventsub_key.get(
                                (
                                    str(row.get("broadcaster_user_id", "")).strip(),
                                    str(row.get("event_type", "")).strip(),
                                    str(row.get("bot_account_id", "")),
                                ),
                                set(),
                            )
                        )
                    ]
                ],
            },
            "services": {
                "rows": service_rows,
                "recent_traces": recent_trace_rows,
            },
            "bots": bot_summary,
            "broadcasters": broadcaster_rows,
            "recent_events": recent_event_rows,
            "logs": recent_logs,
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
