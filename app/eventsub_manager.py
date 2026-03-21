from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import Literal

import websockets
from websockets.exceptions import ConnectionClosedError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.bot_auth import ensure_bot_access_token
from app.eventsub_catalog import (
    best_transport_for_service,
)
from app.event_router import InterestKey, InterestRegistry, LocalEventHub
from app.eventsub_manager_parts.notification_mixin import EventSubNotificationMixin
from app.eventsub_manager_parts.subscription_mixin import EventSubSubscriptionMixin
from app.models import (
    BotAccount,
    ChannelState,
    ServiceInterest,
    ServiceRuntimeStats,
    TwitchSubscription,
)
from app.twitch import TwitchApiError, TwitchClient
from app.twitch_chat_assets import TwitchChatAssetCache

logger = logging.getLogger(__name__)
eventsub_audit_logger = logging.getLogger("eventsub.audit")
WS_LISTENER_COOLDOWN = timedelta(minutes=15)
INTEREST_DISCONNECT_GRACE = timedelta(minutes=15)
INTEREST_HEARTBEAT_TIMEOUT = timedelta(minutes=30)
INTEREST_UNSUBSCRIBE_AFTER_STALE = timedelta(hours=24)
INTEREST_CLEANUP_INTERVAL_SECONDS = 60


class EventSubManager(EventSubNotificationMixin, EventSubSubscriptionMixin):
    def __init__(
        self,
        twitch_client: TwitchClient,
        session_factory: async_sessionmaker,
        registry: InterestRegistry,
        event_hub: LocalEventHub,
        chat_assets: TwitchChatAssetCache | None = None,
        webhook_event_types: set[str] | None = None,
        webhook_callback_url: str | None = None,
        webhook_secret: str | None = None,
        extension_client_id: str | None = None,
        drop_organization_id: str | None = None,
        raid_direction: str = "incoming",
    ) -> None:
        self.twitch = twitch_client
        self.ws_url: str = getattr(self.twitch, "eventsub_ws_url", "wss://eventsub.wss.twitch.tv/ws")
        self.session_factory = session_factory
        self.registry = registry
        self.event_hub = event_hub
        self.chat_assets = chat_assets
        self.webhook_event_types = {event.strip() for event in (webhook_event_types or set()) if event.strip()}
        self.webhook_callback_url = webhook_callback_url
        self.webhook_secret = webhook_secret
        self.extension_client_id = extension_client_id
        self.drop_organization_id = drop_organization_id
        self.raid_direction = raid_direction
        self._task: asyncio.Task | None = None
        self._cleanup_task: asyncio.Task | None = None
        self._pending_retry_task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._session_id: str | None = None
        self._zero_listener_since: datetime | None = None
        self._subscription_key_locks: dict[InterestKey, asyncio.Lock] = {}
        self._subscription_key_locks_guard = asyncio.Lock()
        self._subscription_ensure_concurrency = 8
        self._subscription_error_cooldown = timedelta(minutes=1)
        self._pending_subscription_ttl = timedelta(minutes=10)
        self._pending_retry_interval = timedelta(minutes=5)
        self._pending_retry_max = 5
        self._pending_retry_counts: dict[InterestKey, int] = {}
        self._subscription_rate_limit_max_retries = 3
        self._subscription_rate_limit_base_delay = 1.0
        self._subscription_rate_limit_max_delay = 20.0
        self._subscription_error_last_sent: dict[
            tuple[uuid.UUID, uuid.UUID, str, str, str], datetime
        ] = {}
        self._subscription_error_lock = asyncio.Lock()
        self._fanout_concurrency = 32
        self._fanout_semaphore = asyncio.Semaphore(self._fanout_concurrency)
        self._background_tasks: set[asyncio.Task] = set()
        self._background_tasks_limit = 2000
        self._trace_payload_max_chars = 12000
        self._audit_payload_max_chars = 8000
        self._active_subscriptions_cache_ttl = timedelta(seconds=30)
        self._active_subscriptions_cache_lock = asyncio.Lock()
        self._active_subscriptions_cached_at: datetime | None = None
        self._active_subscriptions_cache: list[dict] = []
        self._active_subscriptions_total_cost = 0
        self._active_subscriptions_max_total_cost = 0
        self._active_subscriptions_total_cost_by_bot: dict[str, int] = {}
        self._active_subscriptions_max_cost_by_bot: dict[str, int] = {}
        self._name_cache_ttl = timedelta(minutes=15)
        self._name_cache_lock = asyncio.Lock()
        self._service_name_cache: dict[uuid.UUID, tuple[str, datetime]] = {}
        self._bot_name_cache: dict[uuid.UUID, tuple[str, datetime]] = {}
        self._broadcaster_name_cache: dict[str, tuple[str, datetime]] = {}
        self._startup_refresh_deferred_to_session_welcome = False
        self._status_lock = asyncio.Lock()
        self._startup_state = "idle"
        self._startup_started_at: datetime | None = None
        self._startup_finished_at: datetime | None = None
        self._phase_history: list[dict] = []
        self._last_error: dict | None = None
        self._session_welcome_count = 0
        self._last_session_welcome_at: datetime | None = None
        self._run_loop_started_at: datetime | None = None
        self._connect_cycle_count = 0

    def _transport_for_event(self, event_type: str) -> Literal["websocket", "webhook"]:
        transport, _ = best_transport_for_service(
            event_type=event_type,
            webhook_available=bool(self.webhook_callback_url and self.webhook_secret),
        )
        return transport

    @staticmethod
    def _is_dead_websocket_status(status: str | None) -> bool:
        normalized = (status or "").strip().lower()
        return bool(normalized) and not normalized.startswith("enabled")

    @staticmethod
    def _is_stale_websocket_session_error(exc: TwitchApiError) -> bool:
        message = str(exc).lower()
        return "session does not exist" in message or "has already disconnected" in message

    async def _timed_phase(self, label: str, func, *args, **kwargs):
        started = time.perf_counter()
        try:
            return await func(*args, **kwargs)
        finally:
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            async with self._status_lock:
                self._phase_history.append(
                    {
                        "label": label,
                        "elapsed_ms": elapsed_ms,
                        "completed_at": datetime.now(UTC).isoformat(),
                    }
                )
                self._phase_history = self._phase_history[-40:]
            logger.info("EventSub phase %s completed in %dms", label, elapsed_ms)

    async def _acquire_subscription_key_lock(self, key: InterestKey) -> asyncio.Lock:
        async with self._subscription_key_locks_guard:
            lock = self._subscription_key_locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._subscription_key_locks[key] = lock
            return lock

    async def _release_subscription_key_lock(self, key: InterestKey, lock: asyncio.Lock) -> None:
        async with self._subscription_key_locks_guard:
            current = self._subscription_key_locks.get(key)
            if current is lock and not lock.locked():
                self._subscription_key_locks.pop(key, None)

    async def start(self) -> None:
        started = time.perf_counter()
        async with self._status_lock:
            self._startup_state = "starting"
            self._startup_started_at = datetime.now(UTC)
            self._startup_finished_at = None
            self._phase_history = []
            self._last_error = None
        await self._timed_phase("load_interests", self._load_interests)
        await self._timed_phase("reconcile_from_twitch", self._sync_from_twitch_and_reconcile)
        await self._timed_phase(
            "ensure_authorization_revoke_webhook",
            self._ensure_authorization_revoke_subscription,
        )
        await self._timed_phase("ensure_webhook_subscriptions", self._ensure_webhook_subscriptions)
        if await self._has_websocket_interest():
            self._startup_refresh_deferred_to_session_welcome = True
            logger.info(
                "Deferring startup stream-state refresh until EventSub websocket session_welcome "
                "because websocket interests are present"
            )
        else:
            self._startup_refresh_deferred_to_session_welcome = False
            await self._timed_phase(
                "refresh_stream_states_active_subscriptions",
                self._refresh_stream_states_for_active_subscriptions,
            )
            await self._timed_phase(
                "refresh_stream_states_interested_channels",
                self._refresh_stream_states_for_interested_channels,
            )
        self._task = asyncio.create_task(self._run(), name="eventsub-manager")
        self._cleanup_task = asyncio.create_task(
            self._cleanup_stale_interests_loop(), name="eventsub-interest-cleanup"
        )
        self._pending_retry_task = asyncio.create_task(
            self._retry_pending_subscriptions_loop(), name="eventsub-pending-retry"
        )
        async with self._status_lock:
            self._startup_state = "ready"
            self._startup_finished_at = datetime.now(UTC)
        logger.info(
            "EventSub manager startup finished in %dms",
            int((time.perf_counter() - started) * 1000),
        )

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
        if self._cleanup_task:
            self._cleanup_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._cleanup_task
        if self._pending_retry_task:
            self._pending_retry_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._pending_retry_task

    async def on_interest_added(self, key: InterestKey) -> None:
        await self._ensure_subscription(key)
        if self.chat_assets and key.event_type.startswith("channel.chat."):
            # Prefetch badges/emotes for faster first-message rendering downstream.
            self.chat_assets.prefetch(key.broadcaster_user_id)

    async def on_interest_removed(self, key: InterestKey, still_used: bool) -> None:
        if still_used:
            return
        async with self.session_factory() as session:
            delete_access_token: str | None = None
            if self._transport_for_event(key.event_type) == "websocket":
                bot = await session.get(BotAccount, key.bot_account_id)
                if bot and bot.enabled:
                    with suppress(Exception):
                        delete_access_token = await ensure_bot_access_token(session, self.twitch, bot)
            db_sub = await session.scalar(
                select(TwitchSubscription).where(
                    TwitchSubscription.bot_account_id == key.bot_account_id,
                    TwitchSubscription.event_type == key.event_type,
                    TwitchSubscription.broadcaster_user_id == key.broadcaster_user_id,
                    TwitchSubscription.raid_direction == (key.raid_direction or ""),
                )
            )
            if db_sub:
                with suppress(TwitchApiError):
                    await self.twitch.delete_eventsub_subscription(
                        db_sub.twitch_subscription_id, access_token=delete_access_token
                    )
                await session.delete(db_sub)
                await session.commit()
            state = await session.scalar(
                select(ChannelState).where(
                    ChannelState.bot_account_id == key.bot_account_id,
                    ChannelState.broadcaster_user_id == key.broadcaster_user_id,
                )
            )
            if state:
                await session.delete(state)
                await session.commit()

    async def prune_stale_interests(self) -> int:
        now = datetime.now(UTC)
        removed = 0
        stale: list[ServiceInterest] = []
        async with self.session_factory() as session:
            interests = list((await session.scalars(select(ServiceInterest))).all())
            stats_rows = list((await session.scalars(select(ServiceRuntimeStats))).all())
            stats_by_service = {
                row.service_account_id: row
                for row in stats_rows
            }

            for interest in interests:
                stats = stats_by_service.get(interest.service_account_id)
                active_ws = bool(stats and (stats.active_ws_connections or 0) > 0)
                disconnect_in_grace = False
                if stats and stats.last_disconnected_at:
                    disconnect_in_grace = (now - stats.last_disconnected_at) <= INTEREST_DISCONNECT_GRACE
                heartbeat_at = interest.last_heartbeat_at or interest.updated_at or interest.created_at
                heartbeat_fresh = (now - heartbeat_at) <= INTEREST_HEARTBEAT_TIMEOUT

                if active_ws or disconnect_in_grace or heartbeat_fresh:
                    if interest.stale_marked_at is not None or interest.delete_after is not None:
                        interest.stale_marked_at = None
                        interest.delete_after = None
                    continue

                if interest.stale_marked_at is None:
                    interest.stale_marked_at = now
                if interest.delete_after is None:
                    interest.delete_after = interest.stale_marked_at + INTEREST_UNSUBSCRIBE_AFTER_STALE
                if now >= interest.delete_after:
                    stale.append(interest)

            for interest in stale:
                await session.delete(interest)
            await session.commit()

        for interest in stale:
            logger.info(
                "Unsubscribing stale interest after extended inactivity: service_id=%s interest_id=%s event_type=%s broadcaster=%s",
                interest.service_account_id,
                interest.id,
                interest.event_type,
                interest.broadcaster_user_id,
            )
            key, still_used = await self.registry.remove(interest)
            await self.on_interest_removed(key, still_used)
            removed += 1
        return removed

    async def _cleanup_stale_interests_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self.prune_stale_interests()
            except Exception as exc:
                logger.warning("Failed stale interest cleanup: %s", exc)
            await asyncio.sleep(INTEREST_CLEANUP_INTERVAL_SECONDS)

    async def _retry_pending_subscriptions(self) -> None:
        pending_statuses = {
            "webhook_callback_verification_pending",
            "websocket_callback_verification_pending",
        }
        now = datetime.now(UTC)
        async with self.session_factory() as session:
            rows = list((await session.scalars(select(TwitchSubscription))).all())

        registry_keys = set(await self.registry.keys())
        status_by_key: dict[InterestKey, str] = {}
        for row in rows:
            key = InterestKey(
                row.bot_account_id,
                row.event_type,
                row.broadcaster_user_id,
                row.authorization_source or "broadcaster",
                row.raid_direction or "",
            )
            status_by_key[key] = str(row.status or "").strip().lower()

        for key in list(self._pending_retry_counts):
            if key not in registry_keys or status_by_key.get(key) not in pending_statuses:
                self._pending_retry_counts.pop(key, None)

        pending_keys: set[InterestKey] = set()
        for row in rows:
            status = str(row.status or "").strip().lower()
            if status not in pending_statuses:
                continue
            key = InterestKey(
                row.bot_account_id,
                row.event_type,
                row.broadcaster_user_id,
                row.authorization_source or "broadcaster",
                row.raid_direction or "",
            )
            if key not in registry_keys:
                continue
            created_at = row.created_at or row.last_seen_at
            if not created_at:
                continue
            if now - created_at <= self._pending_subscription_ttl:
                continue
            attempts = self._pending_retry_counts.get(key, 0)
            if attempts >= self._pending_retry_max:
                reason = (
                    f"EventSub subscription stayed in pending verification after {attempts} retries."
                )
                await self._notify_subscription_failure(
                    key=key,
                    upstream_transport=self._transport_for_event(key.event_type),
                    reason=reason,
                )
                await self.reject_interests_for_key(
                    key=key,
                    reason=reason,
                    upstream_transport=self._transport_for_event(key.event_type),
                )
                self._pending_retry_counts.pop(key, None)
                continue
            self._pending_retry_counts[key] = attempts + 1
            pending_keys.add(key)

        if pending_keys:
            logger.info("Retrying %d pending EventSub subscriptions", len(pending_keys))
        for key in pending_keys:
            await self._ensure_subscription(key)

    async def _retry_pending_subscriptions_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self._retry_pending_subscriptions()
            except Exception as exc:
                logger.warning("Failed pending subscription retry: %s", exc)
            await asyncio.sleep(self._pending_retry_interval.total_seconds())

    async def _load_interests(self) -> None:
        async with self.session_factory() as session:
            interests = list((await session.scalars(select(ServiceInterest))).all())
        await self.registry.load(interests)

    async def _run(self) -> None:
        async with self._status_lock:
            self._run_loop_started_at = datetime.now(UTC)
        while not self._stop.is_set():
            try:
                async with self._status_lock:
                    self._connect_cycle_count += 1
                if not await self._has_websocket_interest():
                    self._session_id = None
                    self._zero_listener_since = None
                    await asyncio.sleep(5)
                    continue
                remaining = await self._websocket_listener_cooldown_remaining()
                if remaining is not None and remaining.total_seconds() <= 0:
                    logger.info(
                        "No service websocket listeners for %ss; suspending EventSub websocket connection",
                        int(WS_LISTENER_COOLDOWN.total_seconds()),
                    )
                    self._session_id = None
                    await asyncio.sleep(5)
                    continue
                await self._run_single_connection(None)
            except ConnectionClosedError as exc:
                async with self._status_lock:
                    self._last_error = {
                        "kind": "connection_closed",
                        "message": str(exc),
                        "recorded_at": datetime.now(UTC).isoformat(),
                    }
                if exc.code == 4003:
                    logger.info("EventSub websocket closed as unused (4003); waiting for websocket interests")
                    self._session_id = None
                    await asyncio.sleep(5)
                    continue
                logger.exception("EventSub websocket closed unexpectedly: %s", exc)
                await asyncio.sleep(3)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                async with self._status_lock:
                    self._last_error = {
                        "kind": "run_loop_exception",
                        "message": str(exc),
                        "recorded_at": datetime.now(UTC).isoformat(),
                    }
                logger.exception("EventSub manager crashed: %s", exc)
                await asyncio.sleep(3)

    async def _has_websocket_interest(self) -> bool:
        keys = await self.registry.keys()
        return any(self._transport_for_event(key.event_type) == "websocket" for key in keys)

    async def _has_stream_state_interest(self) -> bool:
        # stream.online/offline are used to keep ChannelState accurate even when no
        # services are currently connected to this bridge.
        keys = await self.registry.keys()
        return any(key.event_type in {"stream.online", "stream.offline"} for key in keys)

    async def _run_single_connection(self, reconnect_url: str | None) -> None:
        target_url = reconnect_url or self.ws_url
        async with websockets.connect(target_url, max_size=4 * 1024 * 1024) as ws:
            while not self._stop.is_set():
                remaining = await self._websocket_listener_cooldown_remaining()
                if remaining is not None and remaining.total_seconds() <= 0:
                    logger.info(
                        "No service websocket listeners for %ss; suspending EventSub websocket connection",
                        int(WS_LISTENER_COOLDOWN.total_seconds()),
                    )
                    await ws.close()
                    self._session_id = None
                    return
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=5)
                except asyncio.TimeoutError:
                    continue
                message = json.loads(raw)
                metadata = message.get("metadata", {})
                payload = message.get("payload", {})
                msg_type = metadata.get("message_type")
                if msg_type == "session_welcome":
                    started = time.perf_counter()
                    self._session_id = payload["session"]["id"]
                    async with self._status_lock:
                        self._session_welcome_count += 1
                        self._last_session_welcome_at = datetime.now(UTC)
                    logger.info("EventSub websocket session_welcome received: session_id=%s", self._session_id)
                    await self._timed_phase("session_welcome_ensure_all_subscriptions", self._ensure_all_subscriptions)
                    if self._startup_refresh_deferred_to_session_welcome:
                        await self._timed_phase(
                            "session_welcome_refresh_active_subscriptions",
                            self._refresh_stream_states_for_active_subscriptions,
                        )
                        await self._timed_phase(
                            "session_welcome_refresh_interested_channels",
                            self._refresh_stream_states_for_interested_channels,
                        )
                        self._startup_refresh_deferred_to_session_welcome = False
                    logger.info(
                        "EventSub session_welcome bootstrap finished in %dms",
                        int((time.perf_counter() - started) * 1000),
                    )
                    continue
                if msg_type == "session_reconnect":
                    reconnect = payload.get("session", {}).get("reconnect_url")
                    if reconnect:
                        await ws.close()
                        await self._run_single_connection(reconnect)
                        return
                    continue
                if msg_type == "notification":
                    await self._handle_notification(message)
                    continue
                if msg_type == "revocation":
                    await self._handle_revocation(payload)
                    continue

    async def _websocket_listener_cooldown_remaining(self) -> timedelta | None:
        # Keep EventSub stream state subscriptions active even if no downstream services are connected.
        if await self._has_stream_state_interest():
            self._zero_listener_since = None
            return None
        active_ws, latest_disconnect = await self._service_ws_listener_activity()
        if active_ws > 0:
            self._zero_listener_since = None
            return None
        now = datetime.now(UTC)
        if latest_disconnect and (
            self._zero_listener_since is None or latest_disconnect > self._zero_listener_since
        ):
            self._zero_listener_since = latest_disconnect
        if self._zero_listener_since is None:
            self._zero_listener_since = now
        elapsed = now - self._zero_listener_since
        return WS_LISTENER_COOLDOWN - elapsed

    async def _service_ws_listener_activity(self) -> tuple[int, datetime | None]:
        async with self.session_factory() as session:
            active = await session.scalar(select(func.coalesce(func.sum(ServiceRuntimeStats.active_ws_connections), 0)))
            latest_disconnect = await session.scalar(select(func.max(ServiceRuntimeStats.last_disconnected_at)))
        return int(active or 0), latest_disconnect

    async def _disable_websocket_transport_subscriptions(self) -> None:
        async with self.session_factory() as session:
            db_subs = list((await session.scalars(select(TwitchSubscription))).all())
            for db_sub in db_subs:
                if self._transport_for_event(db_sub.event_type) != "websocket":
                    continue
                delete_access_token: str | None = None
                bot = await session.get(BotAccount, db_sub.bot_account_id)
                if bot and bot.enabled:
                    with suppress(Exception):
                        delete_access_token = await ensure_bot_access_token(session, self.twitch, bot)
                with suppress(TwitchApiError):
                    await self.twitch.delete_eventsub_subscription(
                        db_sub.twitch_subscription_id,
                        access_token=delete_access_token,
                    )
                await session.delete(db_sub)
            await session.commit()

    @staticmethod
    def _is_subscription_not_found_error(exc: TwitchApiError) -> bool:
        message = str(exc).lower()
        return "not found" in message or "does not exist" in message

    async def _list_eventsub_subscriptions_all_tokens(self) -> tuple[list[dict], dict]:
        started = time.perf_counter()
        merged: dict[str, dict] = {}
        app_total_cost = 0
        app_max_total_cost = 0
        with suppress(TwitchApiError):
            app_payload = await self.twitch.list_eventsub_subscriptions_with_meta()
            app_total_cost = int(app_payload.get("total_cost", 0) or 0)
            app_max_total_cost = int(app_payload.get("max_total_cost", 0) or 0)
            for sub in app_payload.get("data", []):
                sub_id = str(sub.get("id", ""))
                if sub_id:
                    merged[sub_id] = sub
        async with self.session_factory() as session:
            bots = list((await session.scalars(select(BotAccount).where(BotAccount.enabled.is_(True)))).all())
        semaphore = asyncio.Semaphore(4)
        max_cost_by_bot: dict[str, int] = {}
        total_cost_by_bot: dict[str, int] = {}

        async def _fetch_bot_subscriptions(bot: BotAccount) -> tuple[str, dict]:
            async with semaphore:
                try:
                    async with self.session_factory() as bot_session:
                        bot_row = await bot_session.get(BotAccount, bot.id)
                        if not bot_row or not bot_row.enabled:
                            return str(bot.id), {"data": [], "total_cost": 0, "max_total_cost": 0}
                        token = await ensure_bot_access_token(bot_session, self.twitch, bot_row)
                    return str(bot.id), await self.twitch.list_eventsub_subscriptions_with_meta(access_token=token)
                except Exception:
                    return str(bot.id), {"data": [], "total_cost": 0, "max_total_cost": 0}

        if bots:
            results = await asyncio.gather(
                *(_fetch_bot_subscriptions(bot) for bot in bots),
                return_exceptions=True,
            )
            for result in results:
                if isinstance(result, Exception):
                    continue
                bot_id, payload = result
                max_cost_by_bot[bot_id] = int(payload.get("max_total_cost", 0) or 0)
                total_cost_by_bot[bot_id] = int(payload.get("total_cost", 0) or 0)
                for sub in payload.get("data", []):
                    sub_id = str(sub.get("id", ""))
                    if sub_id:
                        merged[sub_id] = sub
        logger.info(
            "Listed EventSub subscriptions across app+%d bots: unique=%d in %dms",
            len(bots),
            len(merged),
            int((time.perf_counter() - started) * 1000),
        )
        return list(merged.values()), {
            "app_total_cost": app_total_cost,
            "app_max_total_cost": app_max_total_cost,
            "total_cost_by_bot": total_cost_by_bot,
            "max_cost_by_bot": max_cost_by_bot,
            "global_max_total_cost": max([app_max_total_cost, *max_cost_by_bot.values()], default=0),
        }

    async def get_active_subscriptions_snapshot(
        self,
        force_refresh: bool = False,
    ) -> tuple[list[dict], datetime, bool]:
        now = datetime.now(UTC)
        async with self._active_subscriptions_cache_lock:
            if (
                not force_refresh
                and self._active_subscriptions_cached_at
                and (now - self._active_subscriptions_cached_at) < self._active_subscriptions_cache_ttl
            ):
                return (
                    [dict(item) for item in self._active_subscriptions_cache],
                    self._active_subscriptions_cached_at,
                    True,
                )

            started = time.perf_counter()
            subs, cost_meta = await self._list_eventsub_subscriptions_all_tokens()
            snapshot: list[dict] = []
            async with self.session_factory() as session:
                existing_rows = list((await session.scalars(select(TwitchSubscription))).all())
                previous_sub_owner = {
                    row.twitch_subscription_id: row
                    for row in existing_rows
                }
                bots = list((await session.scalars(select(BotAccount))).all())
                bots_by_id = {bot.id: bot for bot in bots}
                bots_by_twitch_user_id = {str(bot.twitch_user_id): bot for bot in bots}
                for sub in subs:
                    condition = sub.get("condition", {})
                    event_type = str(sub.get("type", "")).strip()
                    sub_id = str(sub.get("id", "")).strip()
                    status = str(sub.get("status", "unknown"))
                    raid_direction = ""
                    broadcaster_user_id = str(condition.get("broadcaster_user_id", "")).strip()
                    if event_type == "channel.raid":
                        from_id = str(condition.get("from_broadcaster_user_id", "")).strip()
                        to_id = str(condition.get("to_broadcaster_user_id", "")).strip()
                        if from_id:
                            raid_direction = "outgoing"
                            broadcaster_user_id = from_id
                        elif to_id:
                            raid_direction = "incoming"
                            broadcaster_user_id = to_id
                    if not broadcaster_user_id and event_type == "user.update":
                        broadcaster_user_id = str(condition.get("user_id", "")).strip()
                    method = str(sub.get("transport", {}).get("method", "")).strip()
                    if method not in {"websocket", "webhook"}:
                        continue
                    if not sub_id or not event_type or not broadcaster_user_id:
                        continue
                    bot: BotAccount | None = None
                    if event_type.startswith("channel.chat."):
                        bot_user_id = str(condition.get("user_id", "")).strip()
                        if bot_user_id:
                            bot = bots_by_twitch_user_id.get(bot_user_id)
                    else:
                        moderator_user_id = str(condition.get("moderator_user_id", "")).strip()
                        if moderator_user_id:
                            bot = bots_by_twitch_user_id.get(moderator_user_id)
                        previous_row = previous_sub_owner.get(sub_id)
                        if not bot and previous_row:
                            bot = bots_by_id.get(previous_row.bot_account_id)
                        if not bot:
                            bot = bots_by_twitch_user_id.get(broadcaster_user_id)
                    if not bot:
                        continue
                    previous_row = previous_sub_owner.get(sub_id)
                    authorization_source = (
                        getattr(previous_row, "authorization_source", "broadcaster")
                        if previous_row
                        else "broadcaster"
                    )
                    moderator_user_id = str(condition.get("moderator_user_id", "")).strip()
                    if moderator_user_id:
                        if moderator_user_id == str(bot.twitch_user_id):
                            authorization_source = "bot_moderator"
                        elif moderator_user_id == broadcaster_user_id:
                            authorization_source = "broadcaster"
                    snapshot.append(
                        {
                            "twitch_subscription_id": sub_id,
                            "status": status,
                            "cost": int(sub.get("cost", 0) or 0),
                            "event_type": event_type,
                            "broadcaster_user_id": broadcaster_user_id,
                            "authorization_source": authorization_source,
                            "raid_direction": raid_direction,
                            "upstream_transport": method,
                            "session_id": sub.get("transport", {}).get("session_id"),
                            "connected_at": sub.get("transport", {}).get("connected_at"),
                            "disconnected_at": sub.get("transport", {}).get("disconnected_at"),
                            "bot_account_id": str(bot.id),
                        }
                    )
            self._active_subscriptions_cache = [dict(item) for item in snapshot]
            self._active_subscriptions_cached_at = now
            self._active_subscriptions_total_cost = sum(int(item.get("cost", 0) or 0) for item in snapshot)
            self._active_subscriptions_max_total_cost = int(cost_meta.get("global_max_total_cost", 0) or 0)
            self._active_subscriptions_total_cost_by_bot = {
                str(key): int(value or 0)
                for key, value in (cost_meta.get("total_cost_by_bot") or {}).items()
            }
            self._active_subscriptions_max_cost_by_bot = {
                str(key): int(value or 0)
                for key, value in (cost_meta.get("max_cost_by_bot") or {}).items()
            }
            logger.info(
                "Built live EventSub subscription snapshot: upstream=%d matched=%d in %dms",
                len(subs),
                len(snapshot),
                int((time.perf_counter() - started) * 1000),
            )
            return [dict(item) for item in snapshot], now, False

    async def get_db_active_subscriptions_snapshot(self) -> tuple[list[dict], datetime]:
        now = datetime.now(UTC)
        started = time.perf_counter()
        async with self.session_factory() as session:
            rows = list((await session.scalars(select(TwitchSubscription))).all())
        snapshot = [
            {
                "twitch_subscription_id": row.twitch_subscription_id,
                "status": row.status,
                "cost": 0,
                "event_type": row.event_type,
                "broadcaster_user_id": row.broadcaster_user_id,
                "authorization_source": row.authorization_source or "broadcaster",
                "raid_direction": row.raid_direction,
                "upstream_transport": self._transport_for_event(row.event_type),
                "bot_account_id": str(row.bot_account_id),
                "session_id": row.session_id,
                "connected_at": None,
                "disconnected_at": None,
            }
            for row in rows
        ]
        self._active_subscriptions_total_cost = 0
        self._active_subscriptions_max_total_cost = 0
        self._active_subscriptions_total_cost_by_bot = {}
        self._active_subscriptions_max_cost_by_bot = {}
        logger.info(
            "Built DB EventSub subscription snapshot: rows=%d in %dms",
            len(snapshot),
            int((time.perf_counter() - started) * 1000),
        )
        return snapshot, now

    async def get_status_summary(self) -> dict:
        active_ws, latest_disconnect = await self._service_ws_listener_activity()
        cooldown_remaining = await self._websocket_listener_cooldown_remaining()
        registry_keys = await self.registry.keys()
        async with self._status_lock:
            state = {
                "startup_state": self._startup_state,
                "startup_started_at": self._startup_started_at.isoformat() if self._startup_started_at else None,
                "startup_finished_at": self._startup_finished_at.isoformat() if self._startup_finished_at else None,
                "session_id_masked": (
                    f"{self._session_id[:6]}...{self._session_id[-4:]}"
                    if self._session_id and len(self._session_id) > 12
                    else self._session_id
                ),
                "session_welcome_count": self._session_welcome_count,
                "last_session_welcome_at": (
                    self._last_session_welcome_at.isoformat() if self._last_session_welcome_at else None
                ),
                "phase_history": list(self._phase_history),
                "last_error": dict(self._last_error) if self._last_error else None,
                "connect_cycle_count": self._connect_cycle_count,
                "run_loop_started_at": self._run_loop_started_at.isoformat() if self._run_loop_started_at else None,
            }
        state.update(
            {
                "registry_key_count": len(registry_keys),
                "active_service_ws_connections": active_ws,
                "last_service_disconnect_at": latest_disconnect.isoformat() if latest_disconnect else None,
                "websocket_listener_cooldown_seconds": (
                    max(0, int(cooldown_remaining.total_seconds())) if cooldown_remaining is not None else None
                ),
                "has_websocket_interest": any(
                    self._transport_for_event(key.event_type) == "websocket" for key in registry_keys
                ),
                "has_stream_state_interest": any(
                    key.event_type in {"stream.online", "stream.offline"} for key in registry_keys
                ),
                "active_snapshot_cost_total": int(self._active_subscriptions_total_cost or 0),
                "active_snapshot_max_total_cost": int(self._active_subscriptions_max_total_cost or 0),
                "active_snapshot_total_cost_by_bot": dict(self._active_subscriptions_total_cost_by_bot),
                "active_snapshot_max_cost_by_bot": dict(self._active_subscriptions_max_cost_by_bot),
            }
        )
        return state

