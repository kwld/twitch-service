from __future__ import annotations

import asyncio
import json
import logging
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
        self._task: asyncio.Task | None = None
        self._cleanup_task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._session_id: str | None = None
        self._zero_listener_since: datetime | None = None
        self._subscription_lock = asyncio.Lock()
        self._subscription_error_cooldown = timedelta(minutes=1)
        self._subscription_error_last_sent: dict[
            tuple[uuid.UUID, uuid.UUID, str, str, str], datetime
        ] = {}
        self._subscription_error_lock = asyncio.Lock()
        self._fanout_concurrency = 32
        self._fanout_semaphore = asyncio.Semaphore(self._fanout_concurrency)
        self._trace_payload_max_chars = 12000
        self._audit_payload_max_chars = 8000
        self._active_subscriptions_cache_ttl = timedelta(seconds=30)
        self._active_subscriptions_cache_lock = asyncio.Lock()
        self._active_subscriptions_cached_at: datetime | None = None
        self._active_subscriptions_cache: list[dict] = []
        self._name_cache_ttl = timedelta(minutes=15)
        self._name_cache_lock = asyncio.Lock()
        self._service_name_cache: dict[uuid.UUID, tuple[str, datetime]] = {}
        self._bot_name_cache: dict[uuid.UUID, tuple[str, datetime]] = {}
        self._broadcaster_name_cache: dict[str, tuple[str, datetime]] = {}

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

    async def start(self) -> None:
        await self._load_interests()
        await self._sync_from_twitch_and_reconcile()
        await self._ensure_authorization_revoke_subscription()
        await self._ensure_webhook_subscriptions()
        await self._refresh_stream_states_for_active_subscriptions()
        await self._refresh_stream_states_for_interested_channels()
        self._task = asyncio.create_task(self._run(), name="eventsub-manager")
        self._cleanup_task = asyncio.create_task(
            self._cleanup_stale_interests_loop(), name="eventsub-interest-cleanup"
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

    async def _load_interests(self) -> None:
        async with self.session_factory() as session:
            interests = list((await session.scalars(select(ServiceInterest))).all())
        await self.registry.load(interests)

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
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
                    self._session_id = payload["session"]["id"]
                    await self._sync_from_twitch_and_reconcile()
                    await self._ensure_all_subscriptions()
                    await self._refresh_stream_states_for_active_subscriptions()
                    await self._refresh_stream_states_for_interested_channels()
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

    async def _list_eventsub_subscriptions_all_tokens(self) -> list[dict]:
        merged: dict[str, dict] = {}
        with suppress(TwitchApiError):
            for sub in await self.twitch.list_eventsub_subscriptions():
                sub_id = str(sub.get("id", ""))
                if sub_id:
                    merged[sub_id] = sub
        async with self.session_factory() as session:
            bots = list((await session.scalars(select(BotAccount).where(BotAccount.enabled.is_(True)))).all())
            for bot in bots:
                with suppress(Exception):
                    token = await ensure_bot_access_token(session, self.twitch, bot)
                    for sub in await self.twitch.list_eventsub_subscriptions(access_token=token):
                        sub_id = str(sub.get("id", ""))
                        if sub_id:
                            merged[sub_id] = sub
        return list(merged.values())

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

            subs = await self._list_eventsub_subscriptions_all_tokens()
            snapshot: list[dict] = []
            async with self.session_factory() as session:
                previous_sub_owner = {
                    row.twitch_subscription_id: row.bot_account_id
                    for row in list((await session.scalars(select(TwitchSubscription))).all())
                }
                for sub in subs:
                    condition = sub.get("condition", {})
                    event_type = str(sub.get("type", "")).strip()
                    sub_id = str(sub.get("id", "")).strip()
                    status = str(sub.get("status", "unknown"))
                    broadcaster_user_id = str(condition.get("broadcaster_user_id", "")).strip()
                    method = str(sub.get("transport", {}).get("method", "")).strip()
                    if method not in {"websocket", "webhook"}:
                        continue
                    if not sub_id or not event_type or not broadcaster_user_id:
                        continue
                    bot: BotAccount | None = None
                    if event_type.startswith("channel.chat."):
                        bot_user_id = str(condition.get("user_id", "")).strip()
                        if bot_user_id:
                            bot = await session.scalar(
                                select(BotAccount).where(BotAccount.twitch_user_id == bot_user_id)
                            )
                    else:
                        previous_bot_id = previous_sub_owner.get(sub_id)
                        if previous_bot_id:
                            bot = await session.get(BotAccount, previous_bot_id)
                        if not bot:
                            bot = await session.scalar(
                                select(BotAccount).where(BotAccount.twitch_user_id == broadcaster_user_id)
                            )
                    if not bot:
                        continue
                    snapshot.append(
                        {
                            "twitch_subscription_id": sub_id,
                            "status": status,
                            "event_type": event_type,
                            "broadcaster_user_id": broadcaster_user_id,
                            "upstream_transport": method,
                            "session_id": sub.get("transport", {}).get("session_id"),
                            "connected_at": sub.get("transport", {}).get("connected_at"),
                            "disconnected_at": sub.get("transport", {}).get("disconnected_at"),
                            "bot_account_id": str(bot.id),
                        }
                    )
            self._active_subscriptions_cache = [dict(item) for item in snapshot]
            self._active_subscriptions_cached_at = now
            return [dict(item) for item in snapshot], now, False

