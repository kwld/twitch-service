from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import suppress
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import delete, select

from app.bot_auth import ensure_bot_access_token
from app.eventsub_catalog import (
    preferred_eventsub_version,
    required_scope_any_of_groups,
    requires_condition_user_id,
)
from app.event_router import InterestKey
from app.models import (
    BotAccount,
    BroadcasterAuthorization,
    ServiceInterest,
    TwitchSubscription,
)
from app.twitch import TwitchApiError

logger = logging.getLogger(__name__)


class EventSubSubscriptionMixin:
    async def _sync_from_twitch_and_reconcile(self) -> None:
        subs = await self._list_eventsub_subscriptions_all_tokens()
        async with self.session_factory() as session:
            previous_sub_owner = {
                row.twitch_subscription_id: row.bot_account_id
                for row in list((await session.scalars(select(TwitchSubscription))).all())
            }
            await session.execute(delete(TwitchSubscription))
            deduped: dict[tuple[uuid.UUID, str, str], dict] = {}
            duplicates: list[dict] = []
            for sub in subs:
                condition = sub.get("condition", {})
                event_type = sub.get("type")
                sub_id = str(sub.get("id", ""))
                sub_status = str(sub.get("status", "unknown"))
                broadcaster_user_id = condition.get("broadcaster_user_id")
                bot_user_id = condition.get("user_id")
                method = sub.get("transport", {}).get("method")
                if method not in {"websocket", "webhook"}:
                    continue
                if not sub_id:
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
                    previous_bot_id = previous_sub_owner.get(sub_id)
                    bot = await session.get(BotAccount, previous_bot_id) if previous_bot_id else None
                    if not bot:
                        bot = await session.scalar(
                            select(BotAccount).where(BotAccount.twitch_user_id == broadcaster_user_id)
                        )
                if not bot:
                    continue
                if (
                    method == "websocket"
                    and expected_method == "websocket"
                    and self._is_dead_websocket_status(sub_status)
                ):
                    delete_access_token: str | None = None
                    if bot.enabled:
                        with suppress(Exception):
                            delete_access_token = await ensure_bot_access_token(session, self.twitch, bot)
                    with suppress(TwitchApiError):
                        await self.twitch.delete_eventsub_subscription(sub_id, access_token=delete_access_token)
                    logger.info(
                        "Removed stale websocket subscription %s type=%s status=%s for automatic recovery",
                        sub_id,
                        event_type,
                        sub_status,
                    )
                    continue
                dedupe_key = (bot.id, event_type, broadcaster_user_id)
                candidate = {
                    "bot_account_id": bot.id,
                    "event_type": event_type,
                    "broadcaster_user_id": broadcaster_user_id,
                    "twitch_subscription_id": sub_id,
                    "status": sub_status,
                    "session_id": sub.get("transport", {}).get("session_id"),
                    "connected_at": str(sub.get("transport", {}).get("connected_at", "")),
                    "method": method,
                }
                existing = deduped.get(dedupe_key)
                if not existing:
                    deduped[dedupe_key] = candidate
                    continue

                # Prefer enabled subscriptions; when tied, prefer the one with the latest
                # connected_at timestamp (ISO-8601 strings compare lexicographically here).
                candidate_rank = (
                    1 if str(candidate["status"]).startswith("enabled") else 0,
                    str(candidate["connected_at"]),
                    str(candidate["twitch_subscription_id"]),
                )
                existing_rank = (
                    1 if str(existing["status"]).startswith("enabled") else 0,
                    str(existing["connected_at"]),
                    str(existing["twitch_subscription_id"]),
                )
                if candidate_rank > existing_rank:
                    duplicates.append(existing)
                    deduped[dedupe_key] = candidate
                else:
                    duplicates.append(candidate)

            for item in deduped.values():
                session.add(
                    TwitchSubscription(
                        bot_account_id=item["bot_account_id"],
                        event_type=item["event_type"],
                        broadcaster_user_id=item["broadcaster_user_id"],
                        twitch_subscription_id=item["twitch_subscription_id"],
                        status=item["status"],
                        session_id=item["session_id"],
                        last_seen_at=datetime.now(UTC),
                    )
                )
            await session.commit()

            for duplicate in duplicates:
                duplicate_id = str(duplicate["twitch_subscription_id"])
                if not duplicate_id:
                    continue
                delete_access_token: str | None = None
                if duplicate.get("method") == "websocket":
                    duplicate_bot = await session.get(BotAccount, duplicate["bot_account_id"])
                    if duplicate_bot and duplicate_bot.enabled:
                        with suppress(Exception):
                            delete_access_token = await ensure_bot_access_token(
                                session,
                                self.twitch,
                                duplicate_bot,
                            )
                with suppress(TwitchApiError):
                    await self.twitch.delete_eventsub_subscription(
                        duplicate_id,
                        access_token=delete_access_token,
                    )
                logger.warning(
                    "Removed duplicate Twitch subscription during reconcile: id=%s type=%s broadcaster=%s",
                    duplicate_id,
                    duplicate.get("event_type"),
                    duplicate.get("broadcaster_user_id"),
                )

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
            if self._transport_for_event(key.event_type) == "websocket" and not self._session_id:
                logger.info("Skipping remaining websocket subscription ensures; EventSub session is unavailable")
                break
            try:
                await self._ensure_subscription(key)
            except Exception as exc:
                logger.warning("Failed ensuring subscription for %s: %s", key, exc)
                await self.reject_interests_for_key(
                    key=key,
                    reason=str(exc),
                    upstream_transport=self._transport_for_event(key.event_type),
                )

    async def _ensure_webhook_subscriptions(self) -> None:
        for key in await self.registry.keys():
            if self._transport_for_event(key.event_type) == "webhook":
                try:
                    await self._ensure_subscription(key)
                except Exception as exc:
                    logger.warning("Failed ensuring webhook subscription for %s: %s", key, exc)
                    await self.reject_interests_for_key(
                        key=key,
                        reason=str(exc),
                        upstream_transport=self._transport_for_event(key.event_type),
                    )

    async def _ensure_subscription(self, key: InterestKey) -> None:
        async with self._subscription_lock:
            upstream_transport = self._transport_for_event(key.event_type)
            version = preferred_eventsub_version(key.event_type)
            session_id_snapshot = self._session_id
            if upstream_transport == "websocket" and not session_id_snapshot:
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
                    if upstream_transport == "websocket" and db_sub.session_id == session_id_snapshot:
                        return
                if db_sub and db_sub.twitch_subscription_id:
                    delete_access_token: str | None = None
                    if upstream_transport == "websocket":
                        bot = await session.get(BotAccount, key.bot_account_id)
                        if bot and bot.enabled:
                            with suppress(Exception):
                                delete_access_token = await ensure_bot_access_token(session, self.twitch, bot)
                    try:
                        await self.twitch.delete_eventsub_subscription(
                            db_sub.twitch_subscription_id, access_token=delete_access_token
                        )
                    except TwitchApiError as exc:
                        if not self._is_subscription_not_found_error(exc):
                            await self._notify_subscription_failure(
                                key=key,
                                upstream_transport=upstream_transport,
                                reason=(
                                    f"Cannot rotate EventSub subscription {db_sub.twitch_subscription_id}: {exc}"
                                ),
                            )
                            logger.warning(
                                "Cannot rotate EventSub subscription %s for %s/%s: %s",
                                db_sub.twitch_subscription_id,
                                key.event_type,
                                key.broadcaster_user_id,
                                exc,
                            )
                            return
                    await session.delete(db_sub)
                    await session.flush()
                if upstream_transport == "webhook":
                    if not self.webhook_callback_url or not self.webhook_secret:
                        reason = (
                            "TWITCH_EVENTSUB_WEBHOOK_CALLBACK_URL and "
                            "TWITCH_EVENTSUB_WEBHOOK_SECRET are required for webhook events"
                        )
                        await self._notify_subscription_failure(
                            key=key,
                            upstream_transport=upstream_transport,
                            reason=reason,
                        )
                        raise RuntimeError(reason)
                    transport: dict[str, str] = {
                        "method": "webhook",
                        "callback": self.webhook_callback_url,
                        "secret": self.webhook_secret,
                    }
                else:
                    if self._session_id != session_id_snapshot or not session_id_snapshot:
                        logger.info(
                            "Skipping websocket subscription create for %s/%s due to session change",
                            key.event_type,
                            key.broadcaster_user_id,
                        )
                        return
                    transport = {"method": "websocket", "session_id": session_id_snapshot}
                condition: dict[str, str] = {"broadcaster_user_id": key.broadcaster_user_id}
                create_access_token: str | None = None
                bot = await session.get(BotAccount, key.bot_account_id)
                if not bot:
                    reason = f"Bot account missing for subscription: {key.bot_account_id}"
                    await self._notify_subscription_failure(
                        key=key,
                        upstream_transport=upstream_transport,
                        reason=reason,
                    )
                    raise RuntimeError(reason)
                if not bot.enabled:
                    reason = f"Bot account disabled for subscription: {key.bot_account_id}"
                    await self._notify_subscription_failure(
                        key=key,
                        upstream_transport=upstream_transport,
                        reason=reason,
                    )
                    raise RuntimeError(reason)
                if upstream_transport == "websocket":
                    create_access_token = await ensure_bot_access_token(session, self.twitch, bot)
                if requires_condition_user_id(key.event_type):
                    condition["user_id"] = bot.twitch_user_id

                required_scope_groups = required_scope_any_of_groups(key.event_type)
                if required_scope_groups:
                    def _has_required_scopes(scopes: set[str]) -> bool:
                        return all(any(item in scopes for item in group) for group in required_scope_groups)

                    if key.broadcaster_user_id == bot.twitch_user_id:
                        token_for_check = create_access_token or await ensure_bot_access_token(
                            session, self.twitch, bot
                        )
                        token_info = await self.twitch.validate_user_token(token_for_check)
                        scopes = set(token_info.get("scopes", []))
                        if not _has_required_scopes(scopes):
                            missing = " and ".join(["|".join(sorted(group)) for group in required_scope_groups])
                            reason = (
                                "subscription missing proper authorization: "
                                f"bot token is missing required scope(s) ({missing})"
                            )
                            await self._notify_subscription_failure(
                                key=key,
                                upstream_transport=upstream_transport,
                                reason=reason,
                            )
                            raise RuntimeError(reason)
                    else:
                        auth_rows = list(
                            (
                                await session.scalars(
                                    select(BroadcasterAuthorization).where(
                                        BroadcasterAuthorization.bot_account_id == key.bot_account_id,
                                        BroadcasterAuthorization.broadcaster_user_id == key.broadcaster_user_id,
                                    )
                                )
                            ).all()
                        )
                        any_authorized = False
                        for row in auth_rows:
                            scopes = {x.strip() for x in row.scopes_csv.split(",") if x.strip()}
                            if _has_required_scopes(scopes):
                                any_authorized = True
                                break
                        if not any_authorized:
                            missing = " and ".join(["|".join(sorted(group)) for group in required_scope_groups])
                            reason = (
                                "subscription missing proper authorization: "
                                f"broadcaster grant is missing required scope(s) ({missing})"
                            )
                            await self._notify_subscription_failure(
                                key=key,
                                upstream_transport=upstream_transport,
                                reason=reason,
                            )
                            raise RuntimeError(reason)
                try:
                    created = await self.twitch.create_eventsub_subscription(
                        event_type=key.event_type,
                        version=version,
                        condition=condition,
                        transport=transport,
                        access_token=create_access_token,
                    )
                except TwitchApiError as exc:
                    if upstream_transport == "websocket" and self._is_stale_websocket_session_error(exc):
                        logger.info(
                            "EventSub websocket session became stale during create (%s); will retry on next welcome",
                            session_id_snapshot,
                        )
                        if self._session_id == session_id_snapshot:
                            self._session_id = None
                        return
                    await self._notify_subscription_failure(
                        key=key,
                        upstream_transport=upstream_transport,
                        reason=str(exc),
                    )
                    raise
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

    @staticmethod
    def _classify_subscription_failure(reason: str) -> tuple[str, str]:
        message = reason.lower()
        if "missing proper authorization" in message or "subscription missing proper authorization" in message:
            return (
                "insufficient_permissions",
                "Broadcaster authorization for this bot is missing or no longer valid.",
            )
        if "missing required scope" in message or "scope" in message:
            return (
                "missing_scope",
                "Bot OAuth token is missing required scope for this subscription type.",
            )
        if "unauthorized" in message or "forbidden" in message:
            return (
                "unauthorized",
                "Twitch rejected subscription authorization for this bot/condition.",
            )
        return ("subscription_create_failed", "Twitch rejected subscription creation for this interest.")

    async def _should_emit_subscription_error(
        self,
        service_account_id: uuid.UUID,
        key: InterestKey,
        error_code: str,
    ) -> bool:
        now = datetime.now(UTC)
        throttle_key = (
            service_account_id,
            key.bot_account_id,
            key.event_type,
            key.broadcaster_user_id,
            error_code,
        )
        async with self._subscription_error_lock:
            threshold = now - self._subscription_error_cooldown
            expired = [k for k, sent_at in self._subscription_error_last_sent.items() if sent_at < threshold]
            for item in expired:
                self._subscription_error_last_sent.pop(item, None)
            last_sent = self._subscription_error_last_sent.get(throttle_key)
            if last_sent and now - last_sent < self._subscription_error_cooldown:
                return False
            self._subscription_error_last_sent[throttle_key] = now
            return True

    async def _notify_subscription_failure(
        self,
        key: InterestKey,
        upstream_transport: Literal["websocket", "webhook"],
        reason: str,
    ) -> None:
        interests = await self.registry.interested(key)
        if not interests:
            return
        error_code, hint = self._classify_subscription_failure(reason)
        tasks = []
        for interest in interests:
            if not await self._should_emit_subscription_error(interest.service_account_id, key, error_code):
                continue
            envelope = {
                "id": uuid.uuid4().hex,
                "provider": "twitch-service",
                "type": "subscription.error",
                "event_timestamp": datetime.now(UTC).isoformat(),
                "event": {
                    "error_code": error_code,
                    "reason": reason,
                    "hint": hint,
                    "event_type": key.event_type,
                    "broadcaster_user_id": key.broadcaster_user_id,
                    "bot_account_id": str(key.bot_account_id),
                    "upstream_transport": upstream_transport,
                },
            }
            tasks.append(
                asyncio.create_task(
                    self._deliver_envelope_to_interest(
                        interest=interest,
                        envelope=envelope,
                        event_type="subscription.error",
                        audit_level="error",
                        audit_payload={
                            "kind": "eventsub_subscription_error",
                            "service_account_id": str(interest.service_account_id),
                            "bot_account_id": str(key.bot_account_id),
                            "event_type": key.event_type,
                            "broadcaster_user_id": key.broadcaster_user_id,
                            "direction": "outgoing",
                            "transport": interest.transport,
                            "target": interest.webhook_url if interest.transport == "webhook" else "/ws/events",
                            "error_code": error_code,
                            "reason": reason,
                        },
                    )
                )
            )
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
