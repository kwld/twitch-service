from __future__ import annotations

import asyncio
import json
import logging
import uuid
from contextlib import suppress
from datetime import UTC, datetime
from typing import Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy import select

from app.event_router import InterestKey
from app.models import (
    BotAccount,
    ChannelState,
    ServiceAccount,
    ServiceEventTrace,
    ServiceInterest,
    TwitchSubscription,
)

logger = logging.getLogger(__name__)
eventsub_audit_logger = logging.getLogger("eventsub.audit")


class EventSubNotificationMixin:
    async def handle_webhook_notification(self, payload: dict, message_id: str = "") -> None:
        await self._forward_notification_payload(payload, message_id, incoming_transport="twitch_webhook")

    async def handle_webhook_revocation(self, payload: dict) -> None:
        await self._handle_revocation(payload)

    async def _handle_notification(self, message: dict) -> None:
        payload = message.get("payload", {})
        metadata = message.get("metadata", {})
        await self._forward_notification_payload(
            payload,
            metadata.get("message_id", ""),
            incoming_transport="twitch_websocket",
        )

    async def _forward_notification_payload(
        self,
        payload: dict,
        message_id: str,
        incoming_transport: str,
    ) -> None:
        subscription = payload.get("subscription", {})
        event = payload.get("event", {})
        event_type = subscription.get("type")
        subscription_id = str(subscription.get("id", "")).strip()
        if event_type == "user.authorization.revoke":
            await self._handle_user_authorization_revoke(event)
            return

        broadcaster_user_id = event.get("broadcaster_user_id") or subscription.get("condition", {}).get(
            "broadcaster_user_id"
        )
        if not event_type or not broadcaster_user_id:
            return
        async with self.session_factory() as session:
            bot = None
            if subscription_id:
                db_sub = await session.scalar(
                    select(TwitchSubscription).where(
                        TwitchSubscription.twitch_subscription_id == subscription_id
                    )
                )
                if db_sub:
                    bot = await session.get(BotAccount, db_sub.bot_account_id)
            if not bot:
                condition = subscription.get("condition", {})
                bot_lookup_user_id = (
                    condition.get("user_id")
                    if str(event_type).startswith("channel.chat.")
                    else broadcaster_user_id
                )
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
        if interests:
            source_payload = {
                "message_id": message_id,
                "subscription": subscription,
                "event": event,
            }
            await self._audit_log(
                level="info",
                payload={
                    "kind": "eventsub_incoming",
                    "bot_account_id": str(bot.id),
                    "event_type": event_type,
                    "broadcaster_user_id": broadcaster_user_id,
                    "message_id": message_id,
                    "direction": "incoming",
                    "transport": incoming_transport,
                    "matched_services": len({interest.service_account_id for interest in interests}),
                    "payload": source_payload,
                },
            )
            source_target = "twitch:eventsub"
            incoming_trace_tasks = [
                asyncio.create_task(
                    self._record_service_trace(
                        service_account_id=service_id,
                        direction="incoming",
                        local_transport=incoming_transport,
                        event_type=event_type,
                        target=source_target,
                        payload=source_payload,
                    )
                )
                for service_id in {interest.service_account_id for interest in interests}
            ]
            if incoming_trace_tasks:
                await asyncio.gather(*incoming_trace_tasks, return_exceptions=True)
        envelope = self.event_hub.envelope(
            message_id=message_id,
            event_type=event_type,
            event=event,
        )
        if self.chat_assets and str(event_type).startswith("channel.chat."):
            # Optional enrichment; old clients ignore unknown keys.
            enriched = await self.chat_assets.enrich_chat_event(broadcaster_user_id, event)
            if enriched:
                envelope["twitch_chat_assets"] = enriched
        await self._update_channel_state_from_event(bot.id, event_type, broadcaster_user_id, event)
        outgoing_tasks = [
            asyncio.create_task(
                self._deliver_envelope_to_interest(
                    interest=interest,
                    envelope=envelope,
                    event_type=event_type,
                    audit_level="info",
                    audit_payload={
                        "kind": "eventsub_outgoing",
                        "service_account_id": str(interest.service_account_id),
                        "bot_account_id": str(bot.id),
                        "event_type": event_type,
                        "broadcaster_user_id": broadcaster_user_id,
                        "direction": "outgoing",
                        "transport": interest.transport,
                        "target": interest.webhook_url if interest.transport == "webhook" else "/ws/events",
                        "payload": envelope,
                    },
                )
            )
            for interest in interests
        ]
        if outgoing_tasks:
            await asyncio.gather(*outgoing_tasks, return_exceptions=True)

    async def reject_interests_for_key(
        self,
        key: InterestKey,
        reason: str,
        upstream_transport: Literal["websocket", "webhook"] | None = None,
    ) -> int:
        interests = await self.registry.interested(key)
        if not interests:
            return 0
        transport = upstream_transport or self._transport_for_event(key.event_type)
        notify_tasks = []
        for interest in interests:
            envelope = {
                "id": uuid.uuid4().hex,
                "provider": "twitch-service",
                "type": "interest.rejected",
                "event_timestamp": datetime.now(UTC).isoformat(),
                "event": {
                    "interest_id": str(interest.id),
                    "service_account_id": str(interest.service_account_id),
                    "bot_account_id": str(key.bot_account_id),
                    "event_type": key.event_type,
                    "broadcaster_user_id": key.broadcaster_user_id,
                    "upstream_transport": transport,
                    "reason": reason,
                },
            }
            notify_tasks.append(
                asyncio.create_task(
                    self._deliver_envelope_to_interest(
                        interest=interest,
                        envelope=envelope,
                        event_type="interest.rejected",
                        audit_level="warning",
                        audit_payload={
                            "kind": "interest_rejected",
                            "service_account_id": str(interest.service_account_id),
                            "bot_account_id": str(key.bot_account_id),
                            "event_type": key.event_type,
                            "broadcaster_user_id": key.broadcaster_user_id,
                            "direction": "outgoing",
                            "transport": interest.transport,
                            "target": interest.webhook_url if interest.transport == "webhook" else "/ws/events",
                            "reason": reason,
                        },
                    )
                )
            )
        if notify_tasks:
            await asyncio.gather(*notify_tasks, return_exceptions=True)

        interest_ids = [interest.id for interest in interests]
        async with self.session_factory() as session:
            rows = list(
                (
                    await session.scalars(
                        select(ServiceInterest).where(ServiceInterest.id.in_(interest_ids))
                    )
                ).all()
            )
            for row in rows:
                await session.delete(row)
            await session.commit()

        for interest in interests:
            removed_key, still_used = await self.registry.remove(interest)
            await self.on_interest_removed(removed_key, still_used)
        return len(interests)

    async def _audit_log(self, level: str, payload: dict) -> None:
        kind = str(payload.get("kind", ""))
        # High-volume event fanout logs skip expensive DB/Twitch enrichment lookups.
        if kind in {"eventsub_incoming", "eventsub_outgoing"}:
            enriched_payload = payload
        else:
            enriched_payload = await self._enrich_audit_payload(payload)
        record = self._redact_payload(enriched_payload)
        if isinstance(record, dict):
            record.setdefault("timestamp", datetime.now(UTC).isoformat())
            record.setdefault("level", level)
        text = json.dumps(record, default=str)
        if len(text) > self._audit_payload_max_chars:
            text = text[: self._audit_payload_max_chars] + "... [truncated]"
        if level == "error":
            eventsub_audit_logger.error(text)
        elif level == "warning":
            eventsub_audit_logger.warning(text)
        else:
            eventsub_audit_logger.info(text)

    async def _enrich_audit_payload(self, payload: dict) -> dict:
        enriched = dict(payload)
        service_name = await self._resolve_service_name(enriched.get("service_account_id"))
        if service_name:
            enriched.setdefault("service_name", service_name)
        bot_name = await self._resolve_bot_name(enriched.get("bot_account_id"))
        if bot_name:
            enriched.setdefault("bot_name", bot_name)
        broadcaster_name = await self._resolve_broadcaster_name(
            enriched.get("broadcaster_user_id"),
            enriched.get("broadcaster_name") or enriched.get("broadcaster_login"),
        )
        if broadcaster_name:
            enriched.setdefault("broadcaster_name", broadcaster_name)
        return enriched

    def _cached_name(self, cache: dict, key: object) -> str | None:
        if key is None:
            return None
        item = cache.get(key)
        if not item:
            return None
        value, cached_at = item
        if datetime.now(UTC) - cached_at > self._name_cache_ttl:
            cache.pop(key, None)
            return None
        return value

    async def _resolve_service_name(self, raw_service_id: object) -> str | None:
        if raw_service_id is None:
            return None
        try:
            service_id = raw_service_id if isinstance(raw_service_id, uuid.UUID) else uuid.UUID(str(raw_service_id))
        except Exception:
            return None
        async with self._name_cache_lock:
            cached = self._cached_name(self._service_name_cache, service_id)
            if cached:
                return cached
        name: str | None = None
        try:
            async with self.session_factory() as session:
                service = await session.get(ServiceAccount, service_id)
                if service and service.name:
                    name = service.name
        except Exception:
            return None
        if name:
            async with self._name_cache_lock:
                self._service_name_cache[service_id] = (name, datetime.now(UTC))
        return name

    async def _resolve_bot_name(self, raw_bot_id: object) -> str | None:
        if raw_bot_id is None:
            return None
        try:
            bot_id = raw_bot_id if isinstance(raw_bot_id, uuid.UUID) else uuid.UUID(str(raw_bot_id))
        except Exception:
            return None
        async with self._name_cache_lock:
            cached = self._cached_name(self._bot_name_cache, bot_id)
            if cached:
                return cached
        name: str | None = None
        try:
            async with self.session_factory() as session:
                bot = await session.get(BotAccount, bot_id)
                if bot and bot.name:
                    name = bot.name
        except Exception:
            return None
        if name:
            async with self._name_cache_lock:
                self._bot_name_cache[bot_id] = (name, datetime.now(UTC))
        return name

    async def _resolve_broadcaster_name(
        self,
        raw_broadcaster_user_id: object,
        provided_name: object,
    ) -> str | None:
        if raw_broadcaster_user_id is None:
            return None
        broadcaster_user_id = str(raw_broadcaster_user_id).strip()
        if not broadcaster_user_id:
            return None
        if provided_name:
            name = str(provided_name).strip()
            if name:
                async with self._name_cache_lock:
                    self._broadcaster_name_cache[broadcaster_user_id] = (name, datetime.now(UTC))
                return name
        async with self._name_cache_lock:
            cached = self._cached_name(self._broadcaster_name_cache, broadcaster_user_id)
            if cached:
                return cached
        name: str | None = None
        try:
            user = await self.twitch.get_user_by_id_app(broadcaster_user_id)
            if user:
                login = str(user.get("login", "")).strip()
                display_name = str(user.get("display_name", "")).strip()
                name = display_name or login
        except Exception:
            return None
        if name:
            async with self._name_cache_lock:
                self._broadcaster_name_cache[broadcaster_user_id] = (name, datetime.now(UTC))
        return name

    @staticmethod
    def _is_sensitive_key(key: str) -> bool:
        k = key.strip().lower().replace("-", "_")
        return any(
            token in k
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

    @staticmethod
    def _mask_secret(value: object) -> str:
        raw = str(value)
        if not raw:
            return "***"
        if len(raw) <= 4:
            return "***"
        return "***" + raw[-4:]

    def _redact_payload(self, payload: object) -> object:
        if isinstance(payload, dict):
            out: dict[str, object] = {}
            for key, value in payload.items():
                if self._is_sensitive_key(str(key)):
                    out[str(key)] = self._mask_secret(value)
                else:
                    out[str(key)] = self._redact_payload(value)
            return out
        if isinstance(payload, list):
            return [self._redact_payload(x) for x in payload]
        if isinstance(payload, str):
            return payload
        return payload

    def _redact_target(self, target: str | None) -> str | None:
        if not target:
            return target
        raw = str(target)
        try:
            split = urlsplit(raw)
            if split.scheme and split.netloc:
                query_items = parse_qsl(split.query, keep_blank_values=True)
                redacted_items: list[tuple[str, str]] = []
                for key, value in query_items:
                    if self._is_sensitive_key(key):
                        redacted_items.append((key, self._mask_secret(value)))
                    else:
                        redacted_items.append((key, value))
                redacted_query = urlencode(redacted_items, doseq=True)
                return urlunsplit((split.scheme, split.netloc, split.path, redacted_query, split.fragment))
        except Exception:
            pass
        if any(token in raw.lower() for token in ("secret", "token", "authorization", "api_key", "password")):
            return self._mask_secret(raw)
        return raw

    async def _record_service_trace(
        self,
        service_account_id: uuid.UUID,
        direction: str,
        local_transport: str,
        event_type: str,
        target: str | None,
        payload: object,
    ) -> None:
        try:
            redacted = self._redact_payload(payload)
            payload_json = json.dumps(redacted, default=str)
            if len(payload_json) > self._trace_payload_max_chars:
                payload_json = payload_json[: self._trace_payload_max_chars] + "... [truncated]"
            async with self.session_factory() as session:
                service = await session.get(ServiceAccount, service_account_id)
                if service is None:
                    return
                session.add(
                    ServiceEventTrace(
                        service_account_id=service_account_id,
                        direction=direction,
                        local_transport=local_transport,
                        event_type=event_type,
                        target=self._redact_target(target),
                        payload_json=payload_json,
                    )
                )
                await session.commit()
        except Exception as exc:
            logger.debug("Skipping service event trace write: %s", exc)

    async def _deliver_envelope_to_interest(
        self,
        *,
        interest: ServiceInterest,
        envelope: dict,
        event_type: str,
        audit_level: str,
        audit_payload: dict,
    ) -> None:
        async with self._fanout_semaphore:
            # Prioritize delivery path; traces/logging are secondary.
            if interest.transport == "webhook" and interest.webhook_url:
                with suppress(Exception):
                    await self.event_hub.publish_webhook(
                        interest.service_account_id,
                        interest.webhook_url,
                        envelope,
                    )
            else:
                await self.event_hub.publish_to_service(interest.service_account_id, envelope)
            await asyncio.gather(
                self._audit_log(level=audit_level, payload=audit_payload),
                self._record_service_trace(
                    service_account_id=interest.service_account_id,
                    direction="outgoing",
                    local_transport=interest.transport,
                    event_type=event_type,
                    target=interest.webhook_url if interest.transport == "webhook" else "/ws/events",
                    payload=envelope,
                ),
                return_exceptions=True,
            )

    async def _handle_revocation(self, payload: dict) -> None:
        sub = payload.get("subscription", {})
        twitch_id = sub.get("id")
        if not twitch_id:
            return
        await self._audit_log(
            level="warning",
            payload={
                "kind": "eventsub_revocation",
                "direction": "incoming",
                "transport": "twitch_eventsub",
                "subscription_id": twitch_id,
                "payload": payload,
            },
        )
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
        await self._audit_log(
            level="warning",
            payload={
                "kind": "eventsub_user_authorization_revoke",
                "direction": "incoming",
                "transport": "twitch_eventsub",
                "event_type": "user.authorization.revoke",
                "payload": event,
            },
        )
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

    async def _refresh_stream_states_for_bot_targets(self, per_bot: dict) -> None:
        if not per_bot:
            return
        # Stream online/offline state should reflect real Twitch state even if bot tokens are stale/disabled,
        # so use app token for Helix streams lookup.
        token = await self.twitch.app_access_token()
        async with self.session_factory() as session:
            for bot_id, broadcaster_ids in per_bot.items():
                if not broadcaster_ids:
                    continue
                try:
                    live_streams: list[dict] = []
                    broadcaster_list = list(broadcaster_ids)
                    for idx in range(0, len(broadcaster_list), 100):
                        chunk = broadcaster_list[idx : idx + 100]
                        live_streams.extend(await self.twitch.get_streams_by_user_ids(token, chunk))
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

    async def _refresh_stream_states_for_active_subscriptions(self) -> None:
        per_bot: dict = {}
        async with self.session_factory() as session:
            stream_subs = list(
                (
                    await session.scalars(
                        select(TwitchSubscription).where(
                            TwitchSubscription.event_type.in_(("stream.online", "stream.offline"))
                        )
                    )
                ).all()
            )
        for sub in stream_subs:
            per_bot.setdefault(sub.bot_account_id, set()).add(sub.broadcaster_user_id)
        if not per_bot:
            logger.info("No active stream.online/stream.offline subscriptions found; skipping Helix refresh")
            return
        total = sum(len(v) for v in per_bot.values())
        logger.info(
            "Refreshing stream state from active subscriptions: bots=%d targets=%d",
            len(per_bot),
            total,
        )
        await self._refresh_stream_states_for_bot_targets(per_bot)

    async def _refresh_stream_states_for_interested_channels(self) -> None:
        keys = await self.registry.keys()
        per_bot: dict = {}
        for key in keys:
            if key.event_type == "user.authorization.revoke":
                continue
            per_bot.setdefault(key.bot_account_id, set()).add(key.broadcaster_user_id)
        await self._refresh_stream_states_for_bot_targets(per_bot)

