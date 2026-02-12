from __future__ import annotations

import asyncio
import json
import logging
from contextlib import suppress
from datetime import UTC, datetime
from typing import Literal

import websockets
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.event_router import InterestKey, InterestRegistry, LocalEventHub
from app.models import BotAccount, ServiceInterest, TwitchSubscription
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
        self._stop = asyncio.Event()
        self._session_id: str | None = None

    async def start(self) -> None:
        await self._load_interests()
        await self._sync_from_twitch_and_reconcile()
        await self._ensure_webhook_subscriptions()
        self._task = asyncio.create_task(self._run(), name="eventsub-manager")
        
    def _transport_for_event(self, event_type: str) -> Literal["websocket", "webhook"]:
        if event_type in self.webhook_event_types:
            return "webhook"
        return "websocket"
        await self._ensure_all_subscriptions()

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task

    async def on_interest_added(self, key: InterestKey) -> None:
        await self._ensure_subscription(key)

    async def on_interest_removed(self, key: InterestKey, still_used: bool) -> None:
        if still_used:
            return
        async with self.session_factory() as session:
            db_sub = await session.scalar(
                select(TwitchSubscription).where(
                    TwitchSubscription.bot_account_id == key.bot_account_id,
                    TwitchSubscription.event_type == key.event_type,
                    TwitchSubscription.broadcaster_user_id == key.broadcaster_user_id,
                )
            )
            if db_sub:
                with suppress(TwitchApiError):
                    await self.twitch.delete_eventsub_subscription(db_sub.twitch_subscription_id)
                await session.delete(db_sub)
                await session.commit()

    async def _load_interests(self) -> None:
        async with self.session_factory() as session:
            interests = list((await session.scalars(select(ServiceInterest))).all())
        await self.registry.load(interests)

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await self._run_single_connection(None)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("EventSub manager crashed: %s", exc)
                await asyncio.sleep(3)

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
                method = sub.get("transport", {}).get("method")
                if method not in {"websocket", "webhook"}:
                    continue
                if not event_type or not broadcaster_user_id:
                    continue
                expected_method = self._transport_for_event(event_type)
                if method != expected_method:
                    continue
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

    async def _ensure_all_subscriptions(self) -> None:
        for key in await self.registry.keys():
            await self._ensure_subscription(key)

    async def _ensure_webhook_subscriptions(self) -> None:
        for key in await self.registry.keys():
            if self._transport_for_event(key.event_type) == "webhook":
                await self._ensure_subscription(key)

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
                    await self.twitch.delete_eventsub_subscription(db_sub.twitch_subscription_id)
                await session.delete(db_sub)
                await session.flush()
            transport: dict[str, str]
            if upstream_transport == "webhook":
                if not self.webhook_callback_url or not self.webhook_secret:
                    raise RuntimeError(
                        "TWITCH_EVENTSUB_WEBHOOK_CALLBACK_URL and TWITCH_EVENTSUB_WEBHOOK_SECRET are required for webhook events"
                    )
                transport = {
                    "method": "webhook",
                    "callback": self.webhook_callback_url,
                    "secret": self.webhook_secret,
                }
            else:
                transport = {"method": "websocket", "session_id": self._session_id or ""}
            created = await self.twitch.create_eventsub_subscription(
                event_type=key.event_type,
                version="1",
                condition={"broadcaster_user_id": key.broadcaster_user_id},
                transport=transport,
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
        broadcaster_user_id = event.get("broadcaster_user_id") or subscription.get("condition", {}).get(
            "broadcaster_user_id"
        )
        if not event_type or not broadcaster_user_id:
            return
        async with self.session_factory() as session:
            bot = await session.scalar(
                select(BotAccount).where(BotAccount.twitch_user_id == broadcaster_user_id)
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
        for interest in interests:
            if interest.transport == "webhook" and interest.webhook_url:
                with suppress(Exception):
                    await self.event_hub.publish_webhook(interest.webhook_url, envelope)
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
