from __future__ import annotations

import asyncio
import json
import logging
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import Literal

import websockets
from websockets.exceptions import ConnectionClosedError
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.bot_auth import ensure_bot_access_token
from app.event_router import InterestKey, InterestRegistry, LocalEventHub
from app.models import BotAccount, ChannelState, ServiceInterest, TwitchSubscription
from app.twitch import TwitchApiError, TwitchClient

logger = logging.getLogger(__name__)


class EventSubManager:
    def __init__(
        self,
        twitch_client: TwitchClient,
        session_factory: async_sessionmaker,
        registry: InterestRegistry,
        event_hub: LocalEventHub,
        webhook_event_types: set[str] | None = None,
        webhook_callback_url: str | None = None,
        webhook_secret: str | None = None,
    ) -> None:
        self.twitch = twitch_client
        self.ws_url: str = getattr(self.twitch, "eventsub_ws_url", "wss://eventsub.wss.twitch.tv/ws")
        self.session_factory = session_factory
        self.registry = registry
        self.event_hub = event_hub
        self.webhook_event_types = {event.strip() for event in (webhook_event_types or set()) if event.strip()}
        self.webhook_callback_url = webhook_callback_url
        self.webhook_secret = webhook_secret
        self._task: asyncio.Task | None = None
        self._cleanup_task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._session_id: str | None = None

    def _transport_for_event(self, event_type: str) -> Literal["websocket", "webhook"]:
        if event_type == "user.authorization.revoke":
            return "webhook"
        if event_type in self.webhook_event_types:
            return "webhook"
        return "websocket"

    async def start(self) -> None:
        await self._load_interests()
        await self._sync_from_twitch_and_reconcile()
        await self._ensure_authorization_revoke_subscription()
        await self._ensure_webhook_subscriptions()
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

    async def on_interest_removed(self, key: InterestKey, still_used: bool) -> None:
        if still_used:
            return
        async with self.session_factory() as session:
            delete_access_token: str | None = None
            if key.event_type.startswith("channel.chat."):
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

    async def prune_stale_interests(self, max_age: timedelta) -> int:
        threshold = datetime.now(UTC) - max_age
        removed = 0
        async with self.session_factory() as session:
            stale = list(
                (
                    await session.scalars(
                        select(ServiceInterest).where(ServiceInterest.updated_at < threshold)
                    )
                ).all()
            )
            for interest in stale:
                await session.delete(interest)
            await session.commit()
        for interest in stale:
            key, still_used = await self.registry.remove(interest)
            await self.on_interest_removed(key, still_used)
            removed += 1
        return removed

    async def _cleanup_stale_interests_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self.prune_stale_interests(max_age=timedelta(hours=1))
            except Exception as exc:
                logger.warning("Failed stale interest cleanup: %s", exc)
            await asyncio.sleep(300)

    async def _load_interests(self) -> None:
        async with self.session_factory() as session:
            interests = list((await session.scalars(select(ServiceInterest))).all())
        await self.registry.load(interests)

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                # Twitch closes idle EventSub websocket sessions with 4003 ("connection unused").
                # Avoid opening/keeping a websocket when there are no websocket-routed interests.
                if not await self._has_websocket_interest():
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

    async def _run_single_connection(self, reconnect_url: str | None) -> None:
        target_url = reconnect_url or self.ws_url
        async with websockets.connect(target_url, max_size=4 * 1024 * 1024) as ws:
            while not self._stop.is_set():
                raw = await ws.recv()
                message = json.loads(raw)
                metadata = message.get("metadata", {})
                payload = message.get("payload", {})
                msg_type = metadata.get("message_type")
                if msg_type == "session_welcome":
                    self._session_id = payload["session"]["id"]
                    await self._sync_from_twitch_and_reconcile()
                    await self._ensure_all_subscriptions()
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

    async def _sync_from_twitch_and_reconcile(self) -> None:
        subs = await self.twitch.list_eventsub_subscriptions()
        async with self.session_factory() as session:
            await session.execute(delete(TwitchSubscription))
            for sub in subs:
                condition = sub.get("condition", {})
                event_type = sub.get("type")
                broadcaster_user_id = condition.get("broadcaster_user_id")
                bot_user_id = condition.get("user_id")
                method = sub.get("transport", {}).get("method")
                if method not in {"websocket", "webhook"}:
                    continue
                if not event_type or not broadcaster_user_id:
                    continue
                expected_method = self._transport_for_event(event_type)
                if method != expected_method:
                    continue
                if event_type.startswith("channel.chat."):
                    if not bot_user_id:
                        continue
                    bot = await session.scalar(
                        select(BotAccount).where(BotAccount.twitch_user_id == bot_user_id)
                    )
                else:
                    bot = await session.scalar(
                        select(BotAccount).where(BotAccount.twitch_user_id == broadcaster_user_id)
                    )
                if not bot:
                    continue
                db_sub = TwitchSubscription(
                    bot_account_id=bot.id,
                    event_type=event_type,
                    broadcaster_user_id=broadcaster_user_id,
                    twitch_subscription_id=sub["id"],
                    status=sub.get("status", "unknown"),
                    session_id=sub.get("transport", {}).get("session_id"),
                    last_seen_at=datetime.now(UTC),
                )
                session.add(db_sub)
            await session.commit()

    async def _ensure_authorization_revoke_subscription(self) -> None:
        if not self.webhook_callback_url or not self.webhook_secret:
            logger.warning(
                "Skipping user.authorization.revoke subscription: webhook callback/secret not configured"
            )
            return
        with suppress(TwitchApiError):
            existing = await self.twitch.list_eventsub_subscriptions()
            for sub in existing:
                if sub.get("type") == "user.authorization.revoke":
                    transport = sub.get("transport", {})
                    if transport.get("method") == "webhook":
                        return
            await self.twitch.create_eventsub_subscription(
                event_type="user.authorization.revoke",
                version="1",
                condition={"client_id": self.twitch.client_id},
                transport={
                    "method": "webhook",
                    "callback": self.webhook_callback_url,
                    "secret": self.webhook_secret,
                },
            )

    async def _ensure_all_subscriptions(self) -> None:
        for key in await self.registry.keys():
            try:
                await self._ensure_subscription(key)
            except Exception as exc:
                logger.warning("Failed ensuring subscription for %s: %s", key, exc)

    async def _ensure_webhook_subscriptions(self) -> None:
        for key in await self.registry.keys():
            if self._transport_for_event(key.event_type) == "webhook":
                try:
                    await self._ensure_subscription(key)
                except Exception as exc:
                    logger.warning("Failed ensuring webhook subscription for %s: %s", key, exc)

    async def _ensure_subscription(self, key: InterestKey) -> None:
        upstream_transport = self._transport_for_event(key.event_type)
        if upstream_transport == "websocket" and not self._session_id:
            return
        async with self.session_factory() as session:
            db_sub = await session.scalar(
                select(TwitchSubscription).where(
                    TwitchSubscription.bot_account_id == key.bot_account_id,
                    TwitchSubscription.event_type == key.event_type,
                    TwitchSubscription.broadcaster_user_id == key.broadcaster_user_id,
                )
            )
            if db_sub and db_sub.status.startswith("enabled"):
                if upstream_transport == "webhook" and not db_sub.session_id:
                    return
                if upstream_transport == "websocket" and db_sub.session_id == self._session_id:
                    return
            if db_sub and db_sub.twitch_subscription_id:
                with suppress(TwitchApiError):
                    delete_access_token: str | None = None
                    if key.event_type.startswith("channel.chat."):
                        bot = await session.get(BotAccount, key.bot_account_id)
                        if bot and bot.enabled:
                            with suppress(Exception):
                                delete_access_token = await ensure_bot_access_token(session, self.twitch, bot)
                    await self.twitch.delete_eventsub_subscription(
                        db_sub.twitch_subscription_id, access_token=delete_access_token
                    )
                await session.delete(db_sub)
                await session.flush()
            if upstream_transport == "webhook":
                if not self.webhook_callback_url or not self.webhook_secret:
                    raise RuntimeError(
                        "TWITCH_EVENTSUB_WEBHOOK_CALLBACK_URL and TWITCH_EVENTSUB_WEBHOOK_SECRET are required for webhook events"
                    )
                transport: dict[str, str] = {
                    "method": "webhook",
                    "callback": self.webhook_callback_url,
                    "secret": self.webhook_secret,
                }
            else:
                transport = {"method": "websocket", "session_id": self._session_id or ""}
            condition: dict[str, str] = {"broadcaster_user_id": key.broadcaster_user_id}
            create_access_token: str | None = None
            if key.event_type.startswith("channel.chat."):
                bot = await session.get(BotAccount, key.bot_account_id)
                if not bot:
                    raise RuntimeError(f"Bot account missing for chat subscription: {key.bot_account_id}")
                if not bot.enabled:
                    raise RuntimeError(f"Bot account disabled for chat subscription: {key.bot_account_id}")
                create_access_token = await ensure_bot_access_token(session, self.twitch, bot)
                condition["user_id"] = bot.twitch_user_id
            created = await self.twitch.create_eventsub_subscription(
                event_type=key.event_type,
                version="1",
                condition=condition,
                transport=transport,
                access_token=create_access_token,
            )
            new_sub = TwitchSubscription(
                bot_account_id=key.bot_account_id,
                event_type=key.event_type,
                broadcaster_user_id=key.broadcaster_user_id,
                twitch_subscription_id=created["id"],
                status=created.get("status", "enabled"),
                session_id=created.get("transport", {}).get("session_id"),
                last_seen_at=datetime.now(UTC),
            )
            session.add(new_sub)
            await session.commit()

    async def handle_webhook_notification(self, payload: dict, message_id: str = "") -> None:
        await self._forward_notification_payload(payload, message_id)

    async def handle_webhook_revocation(self, payload: dict) -> None:
        await self._handle_revocation(payload)

    async def _handle_notification(self, message: dict) -> None:
        payload = message.get("payload", {})
        metadata = message.get("metadata", {})
        await self._forward_notification_payload(payload, metadata.get("message_id", ""))

    async def _forward_notification_payload(self, payload: dict, message_id: str) -> None:
        subscription = payload.get("subscription", {})
        event = payload.get("event", {})
        event_type = subscription.get("type")
        if event_type == "user.authorization.revoke":
            await self._handle_user_authorization_revoke(event)
            return

        broadcaster_user_id = event.get("broadcaster_user_id") or subscription.get("condition", {}).get(
            "broadcaster_user_id"
        )
        if not event_type or not broadcaster_user_id:
            return
        condition = subscription.get("condition", {})
        bot_lookup_user_id = (
            condition.get("user_id")
            if str(event_type).startswith("channel.chat.")
            else broadcaster_user_id
        )
        async with self.session_factory() as session:
            bot = await session.scalar(
                select(BotAccount).where(BotAccount.twitch_user_id == str(bot_lookup_user_id))
            )
            if not bot:
                return
        key = InterestKey(
            bot_account_id=bot.id,
            event_type=event_type,
            broadcaster_user_id=broadcaster_user_id,
        )
        interests = await self.registry.interested(key)
        envelope = self.event_hub.envelope(
            message_id=message_id,
            event_type=event_type,
            event=event,
        )
        await self._update_channel_state_from_event(bot.id, event_type, broadcaster_user_id, event)
        for interest in interests:
            if interest.transport == "webhook" and interest.webhook_url:
                with suppress(Exception):
                    await self.event_hub.publish_webhook(
                        interest.service_account_id,
                        interest.webhook_url,
                        envelope,
                    )
            else:
                await self.event_hub.publish_to_service(interest.service_account_id, envelope)

    async def _handle_revocation(self, payload: dict) -> None:
        sub = payload.get("subscription", {})
        twitch_id = sub.get("id")
        if not twitch_id:
            return
        async with self.session_factory() as session:
            db_sub = await session.scalar(
                select(TwitchSubscription).where(TwitchSubscription.twitch_subscription_id == twitch_id)
            )
            if db_sub:
                db_sub.status = "revoked"
                await session.commit()

    async def _handle_user_authorization_revoke(self, event: dict) -> None:
        revoked_user_id = event.get("user_id")
        if not revoked_user_id:
            return
        async with self.session_factory() as session:
            bot = await session.scalar(select(BotAccount).where(BotAccount.twitch_user_id == revoked_user_id))
            if not bot:
                return
            bot.enabled = False
            bot.access_token = ""
            bot.refresh_token = ""
            await session.commit()
        logger.warning("Disabled bot %s due to user.authorization.revoke", revoked_user_id)

    async def _update_channel_state_from_event(
        self, bot_account_id, event_type: str, broadcaster_user_id: str, event: dict
    ) -> None:
        if event_type not in {"stream.online", "stream.offline"}:
            return
        async with self.session_factory() as session:
            state = await session.scalar(
                select(ChannelState).where(
                    ChannelState.bot_account_id == bot_account_id,
                    ChannelState.broadcaster_user_id == broadcaster_user_id,
                )
            )
            if not state:
                state = ChannelState(
                    bot_account_id=bot_account_id,
                    broadcaster_user_id=broadcaster_user_id,
                    is_live=False,
                )
                session.add(state)
            if event_type == "stream.online":
                state.is_live = True
                state.started_at = self._parse_datetime(event.get("started_at"))
            else:
                state.is_live = False
                state.started_at = None
            state.last_event_at = datetime.now(UTC)
            state.last_checked_at = datetime.now(UTC)
            await session.commit()

    def _parse_datetime(self, raw: str | None):
        if not raw:
            return None
        with suppress(ValueError):
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return None

    async def _refresh_stream_states_for_interested_channels(self) -> None:
        keys = await self.registry.keys()
        per_bot: dict = {}
        for key in keys:
            if key.event_type == "user.authorization.revoke":
                continue
            per_bot.setdefault(key.bot_account_id, set()).add(key.broadcaster_user_id)
        if not per_bot:
            return
        async with self.session_factory() as session:
            for bot_id, broadcaster_ids in per_bot.items():
                bot = await session.get(BotAccount, bot_id)
                if not bot or not bot.enabled:
                    continue
                try:
                    bot_token = await ensure_bot_access_token(session, self.twitch, bot)
                    live_streams = []
                    broadcaster_list = list(broadcaster_ids)
                    for idx in range(0, len(broadcaster_list), 100):
                        chunk = broadcaster_list[idx : idx + 100]
                        live_streams.extend(await self.twitch.get_streams_by_user_ids(bot_token, chunk))
                except Exception as exc:
                    logger.warning("Failed refreshing stream states for bot %s: %s", bot_id, exc)
                    continue
                live_by_user = {s.get("user_id"): s for s in live_streams}
                for broadcaster_id in broadcaster_ids:
                    stream = live_by_user.get(broadcaster_id)
                    state = await session.scalar(
                        select(ChannelState).where(
                            ChannelState.bot_account_id == bot_id,
                            ChannelState.broadcaster_user_id == broadcaster_id,
                        )
                    )
                    if not state:
                        state = ChannelState(
                            bot_account_id=bot_id,
                            broadcaster_user_id=broadcaster_id,
                            is_live=False,
                        )
                        session.add(state)
                    if stream:
                        state.is_live = True
                        state.title = stream.get("title")
                        state.game_name = stream.get("game_name")
                        state.started_at = self._parse_datetime(stream.get("started_at"))
                    else:
                        state.is_live = False
                        state.title = None
                        state.game_name = None
                        state.started_at = None
                    state.last_checked_at = datetime.now(UTC)
            await session.commit()
