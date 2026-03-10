from __future__ import annotations

import asyncio
import json
import logging
import random
import time
import uuid
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import Literal

from sqlalchemy import delete, select

from app.bot_auth import ensure_bot_access_token
from app.eventsub_catalog import (
    preferred_eventsub_version,
    required_scope_any_of_groups,
    requires_condition_user_id,
    requires_client_id_condition,
    requires_extension_client_id,
    requires_moderator_user_id,
    requires_organization_id,
    requires_raid_direction,
    requires_user_id_condition,
)
from app.event_router import InterestKey
from app.models import (
    BotAccount,
    BroadcasterAuthorization,
    ServiceEventTrace,
    ServiceInterest,
    TwitchSubscription,
)
from app.twitch import TwitchApiError

logger = logging.getLogger(__name__)


class EventSubSubscriptionMixin:
    def _is_rate_limited_error(self, exc: Exception) -> bool:
        message = str(exc).lower()
        return (
            "status\":429" in message
            or "status\": 429" in message
            or "too many requests" in message
            or "rate limit" in message
        )

    def _rate_limit_backoff_delay(self, attempt: int) -> float:
        base_delay = float(getattr(self, "_subscription_rate_limit_base_delay", 1.0))
        max_delay = float(getattr(self, "_subscription_rate_limit_max_delay", 20.0))
        delay = min(max_delay, base_delay * (2**attempt))
        jitter = random.uniform(0, delay * 0.2)
        return delay + jitter

    def _build_subscription_condition(
        self,
        *,
        event_type: str,
        broadcaster_user_id: str,
        bot_user_id: str,
        raid_direction: str | None = None,
    ) -> dict[str, str | bool]:
        normalized = event_type.strip().lower()

        if requires_client_id_condition(normalized):
            return {"client_id": self.twitch.client_id}

        if requires_extension_client_id(normalized):
            if not self.extension_client_id:
                raise RuntimeError(
                    "Missing TWITCH_EXTENSION_CLIENT_ID for extension.bits_transaction.create subscriptions"
                )
            return {"extension_client_id": str(self.extension_client_id)}

        if requires_organization_id(normalized):
            if not self.drop_organization_id:
                raise RuntimeError(
                    "Missing TWITCH_DROP_ORGANIZATION_ID for drop.entitlement.grant subscriptions"
                )
            return {"organization_id": str(self.drop_organization_id), "is_batching_enabled": True}

        if requires_raid_direction(normalized):
            direction = str(raid_direction or getattr(self, "raid_direction", "incoming") or "incoming").strip().lower()
            if direction in {"outgoing", "from"}:
                return {"from_broadcaster_user_id": broadcaster_user_id}
            return {"to_broadcaster_user_id": broadcaster_user_id}

        if requires_user_id_condition(normalized):
            return {"user_id": broadcaster_user_id}

        condition: dict[str, str | bool] = {"broadcaster_user_id": broadcaster_user_id}

        if requires_moderator_user_id(normalized):
            condition["moderator_user_id"] = broadcaster_user_id

        if requires_condition_user_id(normalized):
            condition["user_id"] = bot_user_id

        return condition
    async def _record_service_actions_for_key(
        self,
        *,
        key: InterestKey,
        event_type: str,
        target: str,
        payload: dict,
    ) -> None:
        interested = await self.registry.interested(key)
        service_ids = {interest.service_account_id for interest in interested}
        if not service_ids:
            return
        try:
            payload_dict = payload if isinstance(payload, dict) else {"value": payload}
            payload_dict.setdefault("_action_status", "completed")
            payload_json = json.dumps(payload_dict, default=str)
            if len(payload_json) > getattr(self, "_trace_payload_max_chars", 12000):
                payload_json = payload_json[: getattr(self, "_trace_payload_max_chars", 12000)] + "... [truncated]"
            async with self.session_factory() as session:
                for service_id in service_ids:
                    session.add(
                        ServiceEventTrace(
                            service_account_id=service_id,
                            direction="outgoing",
                            local_transport="eventsub_action",
                            event_type=event_type,
                            target=target,
                            payload_json=payload_json,
                        )
                    )
                await session.commit()
        except Exception:
            return

    def _is_subscription_reusable_status(self, db_sub: TwitchSubscription | None) -> bool:
        if not db_sub:
            return False
        normalized = (db_sub.status or "").strip().lower()
        if not normalized:
            return False
        if normalized.startswith("enabled"):
            return True
        if normalized in {
            "webhook_callback_verification_pending",
            "websocket_callback_verification_pending",
        }:
            created_at = db_sub.created_at
            if not created_at:
                return False
            age = datetime.now(UTC) - created_at
            return age <= getattr(self, "_pending_subscription_ttl", timedelta(minutes=10))
        return False

    async def _sync_from_twitch_and_reconcile(self) -> None:
        started = time.perf_counter()
        listed = await self._list_eventsub_subscriptions_all_tokens()
        subs = listed[0] if isinstance(listed, tuple) else listed
        async with self.session_factory() as session:
            existing_rows = list((await session.scalars(select(TwitchSubscription))).all())
            previous_sub_owner = {
                row.twitch_subscription_id: row.bot_account_id
                for row in existing_rows
            }
            bots = list((await session.scalars(select(BotAccount))).all())
            bots_by_id = {bot.id: bot for bot in bots}
            bots_by_twitch_user_id = {str(bot.twitch_user_id): bot for bot in bots}
            await session.execute(delete(TwitchSubscription))
            deduped: dict[tuple[uuid.UUID, str, str, str], dict] = {}
            duplicates: list[dict] = []
            for sub in subs:
                condition = sub.get("condition", {})
                event_type = sub.get("type")
                sub_id = str(sub.get("id", ""))
                sub_status = str(sub.get("status", "unknown"))
                raid_direction = ""
                broadcaster_user_id = condition.get("broadcaster_user_id")
                if str(event_type).strip().lower() == "channel.raid":
                    from_id = condition.get("from_broadcaster_user_id")
                    to_id = condition.get("to_broadcaster_user_id")
                    if from_id:
                        raid_direction = "outgoing"
                        broadcaster_user_id = from_id
                    elif to_id:
                        raid_direction = "incoming"
                        broadcaster_user_id = to_id
                if not broadcaster_user_id and requires_user_id_condition(str(event_type)):
                    broadcaster_user_id = condition.get("user_id")
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
                    bot = bots_by_twitch_user_id.get(str(bot_user_id))
                else:
                    previous_bot_id = previous_sub_owner.get(sub_id)
                    bot = bots_by_id.get(previous_bot_id) if previous_bot_id else None
                    if not bot:
                        bot = bots_by_twitch_user_id.get(str(broadcaster_user_id))
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
                    await self._record_service_actions_for_key(
                        key=InterestKey(
                            bot.id,
                            event_type,
                            broadcaster_user_id,
                            raid_direction,
                        ),
                        event_type="eventsub.subscription.delete_stale",
                        target="helix:/eventsub/subscriptions",
                        payload={
                            "bot_account_id": str(bot.id),
                            "broadcaster_user_id": str(broadcaster_user_id),
                            "event_type": str(event_type),
                            "subscription_id": sub_id,
                            "status": sub_status,
                            "upstream_transport": method,
                        },
                    )
                    logger.info(
                        "Removed stale websocket subscription %s type=%s status=%s for automatic recovery",
                        sub_id,
                        event_type,
                        sub_status,
                    )
                    continue
                dedupe_key = (bot.id, event_type, broadcaster_user_id, raid_direction)
                candidate = {
                    "bot_account_id": bot.id,
                    "event_type": event_type,
                    "broadcaster_user_id": broadcaster_user_id,
                    "twitch_subscription_id": sub_id,
                    "status": sub_status,
                    "session_id": sub.get("transport", {}).get("session_id"),
                    "connected_at": str(sub.get("transport", {}).get("connected_at", "")),
                    "method": method,
                    "raid_direction": raid_direction,
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
                        raid_direction=item.get("raid_direction", ""),
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
                    duplicate_bot = bots_by_id.get(duplicate["bot_account_id"])
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
                await self._record_service_actions_for_key(
                    key=InterestKey(
                        duplicate["bot_account_id"],
                        duplicate["event_type"],
                        duplicate["broadcaster_user_id"],
                        duplicate.get("raid_direction", ""),
                    ),
                    event_type="eventsub.subscription.delete_duplicate",
                    target="helix:/eventsub/subscriptions",
                    payload={
                        "bot_account_id": str(duplicate["bot_account_id"]),
                        "broadcaster_user_id": str(duplicate["broadcaster_user_id"]),
                        "event_type": str(duplicate["event_type"]),
                        "subscription_id": duplicate_id,
                        "status": duplicate.get("status"),
                        "upstream_transport": duplicate.get("method"),
                    },
                )
                logger.warning(
                    "Removed duplicate Twitch subscription during reconcile: id=%s type=%s broadcaster=%s",
                    duplicate_id,
                    duplicate.get("event_type"),
                    duplicate.get("broadcaster_user_id"),
                )
        logger.info(
            "EventSub reconcile persisted %d subscriptions, skipped/merged %d duplicates in %dms",
            len(deduped),
            len(duplicates),
            int((time.perf_counter() - started) * 1000),
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
        keys = await self.registry.keys()
        concurrency = max(1, int(getattr(self, "_subscription_ensure_concurrency", 1)))
        semaphore = asyncio.Semaphore(concurrency)

        async def _ensure_key(key: InterestKey) -> None:
            upstream_transport = self._transport_for_event(key.event_type)
            if upstream_transport == "websocket" and not self._session_id:
                logger.info(
                    "Skipping websocket subscription ensure for %s/%s; EventSub session is unavailable",
                    key.event_type,
                    key.broadcaster_user_id,
                )
                return
            async with semaphore:
                try:
                    await self._ensure_subscription(key)
                except Exception as exc:
                    logger.warning("Failed ensuring subscription for %s: %s", key, exc)
                    await self.reject_interests_for_key(
                        key=key,
                        reason=str(exc),
                        upstream_transport=upstream_transport,
                    )

        await asyncio.gather(*(_ensure_key(key) for key in keys))

    async def _ensure_webhook_subscriptions(self) -> None:
        keys = [key for key in await self.registry.keys() if self._transport_for_event(key.event_type) == "webhook"]
        concurrency = max(1, int(getattr(self, "_subscription_ensure_concurrency", 1)))
        semaphore = asyncio.Semaphore(concurrency)

        async def _ensure_key(key: InterestKey) -> None:
            async with semaphore:
                try:
                    await self._ensure_subscription(key)
                except Exception as exc:
                    logger.warning("Failed ensuring webhook subscription for %s: %s", key, exc)
                    await self.reject_interests_for_key(
                        key=key,
                        reason=str(exc),
                        upstream_transport="webhook",
                    )

        await asyncio.gather(*(_ensure_key(key) for key in keys))

    async def _ensure_subscription(self, key: InterestKey) -> None:
        lock = await self._acquire_subscription_key_lock(key)
        try:
            async with lock:
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
                            TwitchSubscription.raid_direction == (key.raid_direction or ""),
                        )
                    )
                    if db_sub and self._is_subscription_reusable_status(db_sub):
                        if upstream_transport == "webhook":
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
                            await self._record_service_actions_for_key(
                                key=key,
                                event_type="eventsub.subscription.rotate_delete",
                                target="helix:/eventsub/subscriptions",
                                payload={
                                    "bot_account_id": str(key.bot_account_id),
                                    "broadcaster_user_id": str(key.broadcaster_user_id),
                                    "event_type": str(key.event_type),
                                    "subscription_id": db_sub.twitch_subscription_id,
                                    "upstream_transport": upstream_transport,
                                },
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
                    bot = await session.get(BotAccount, key.bot_account_id)
                    if not bot:
                        reason = f"Bot account missing for subscription: {key.bot_account_id}"
                        await self._notify_subscription_failure(
                            key=key,
                            upstream_transport=upstream_transport,
                            reason=reason,
                        )
                        raise RuntimeError(reason)
                    condition = self._build_subscription_condition(
                        event_type=key.event_type,
                        broadcaster_user_id=key.broadcaster_user_id,
                        bot_user_id=str(bot.twitch_user_id),
                        raid_direction=key.raid_direction or None,
                    )
                    create_access_token: str | None = None
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
                    required_scope_groups = required_scope_any_of_groups(key.event_type)
                    if required_scope_groups:
                        def _has_required_scopes(scopes: set[str]) -> bool:
                            return all(any(item in scopes for item in group) for group in required_scope_groups)

                        missing = " and ".join(["|".join(sorted(group)) for group in required_scope_groups])
                        combined_scopes: set[str] = set()
                        broadcaster_scopes: set[str] = set()
                        bot_scopes: set[str] = set()

                        service_ids = {interest.service_account_id for interest in await self.registry.interested(key)}
                        if service_ids:
                            auth_rows = list(
                                (
                                    await session.scalars(
                                        select(BroadcasterAuthorization).where(
                                            BroadcasterAuthorization.service_account_id.in_(service_ids),
                                            BroadcasterAuthorization.broadcaster_user_id == key.broadcaster_user_id,
                                        )
                                    )
                                ).all()
                            )
                        else:
                            auth_rows = list(
                                (
                                    await session.scalars(
                                        select(BroadcasterAuthorization).where(
                                            BroadcasterAuthorization.broadcaster_user_id == key.broadcaster_user_id,
                                        )
                                    )
                                ).all()
                            )
                        for row in auth_rows:
                            broadcaster_scopes.update(
                                {x.strip() for x in row.scopes_csv.split(",") if x.strip()}
                            )
                        combined_scopes.update(broadcaster_scopes)

                        needs_bot_scope_check = (
                            upstream_transport == "websocket"
                            or key.broadcaster_user_id == bot.twitch_user_id
                            or any(any(not scope.startswith("channel:") for scope in group) for group in required_scope_groups)
                        )
                        if needs_bot_scope_check:
                            token_for_check = create_access_token or await ensure_bot_access_token(
                                session, self.twitch, bot
                            )
                            token_info = await self.twitch.validate_user_token(token_for_check)
                            bot_scopes = {x.strip() for x in token_info.get("scopes", []) if str(x).strip()}
                            combined_scopes.update(bot_scopes)

                        if not _has_required_scopes(combined_scopes):
                            if key.broadcaster_user_id == bot.twitch_user_id:
                                reason = (
                                    "subscription missing proper authorization: "
                                    f"bot token is missing required scope(s) ({missing})"
                                )
                            elif not broadcaster_scopes:
                                reason = (
                                    "subscription missing proper authorization: "
                                    f"broadcaster grant is missing required scope(s) ({missing})"
                                )
                            else:
                                reason = (
                                    "subscription missing proper authorization: "
                                    f"bot token and broadcaster grant scopes do not satisfy required scope(s) ({missing})"
                                )
                            await self._notify_subscription_failure(
                                key=key,
                                upstream_transport=upstream_transport,
                                reason=reason,
                            )
                            raise RuntimeError(reason)
                    max_retries = int(getattr(self, "_subscription_rate_limit_max_retries", 3))
                    created = None
                    for attempt in range(max_retries + 1):
                        try:
                            created = await self.twitch.create_eventsub_subscription(
                                event_type=key.event_type,
                                version=version,
                                condition=condition,
                                transport=transport,
                                access_token=create_access_token,
                            )
                            break
                        except TwitchApiError as exc:
                            if upstream_transport == "websocket" and self._is_stale_websocket_session_error(exc):
                                logger.info(
                                    "EventSub websocket session became stale during create (%s); will retry on next welcome",
                                    session_id_snapshot,
                                )
                                if self._session_id == session_id_snapshot:
                                    self._session_id = None
                                return
                            if self._is_rate_limited_error(exc) and attempt < max_retries:
                                delay = self._rate_limit_backoff_delay(attempt)
                                logger.warning(
                                    "Rate limited creating %s subscription; retrying in %.1fs",
                                    key.event_type,
                                    delay,
                                )
                                await asyncio.sleep(delay)
                                continue
                            await self._notify_subscription_failure(
                                key=key,
                                upstream_transport=upstream_transport,
                                reason=str(exc),
                            )
                            raise
                    await self._record_service_actions_for_key(
                        key=key,
                        event_type="eventsub.subscription.create",
                        target="helix:/eventsub/subscriptions",
                        payload={
                            "bot_account_id": str(key.bot_account_id),
                            "broadcaster_user_id": str(key.broadcaster_user_id),
                            "event_type": str(key.event_type),
                            "subscription_id": created["id"],
                            "status": created.get("status", "enabled"),
                            "upstream_transport": upstream_transport,
                            "session_id": created.get("transport", {}).get("session_id"),
                        },
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
                    interested = await self.registry.interested(key)
                    service_ids = sorted({str(interest.service_account_id) for interest in interested})
                    downstream_transports = sorted({str(interest.transport) for interest in interested})
                    logger.info(
                        "EventSub subscription ensured: event=%s broadcaster=%s upstream=%s downstream=%s services=%d service_ids=%s subscription=%s",
                        key.event_type,
                        key.broadcaster_user_id,
                        upstream_transport,
                        ",".join(downstream_transports) or "-",
                        len(service_ids),
                        ",".join(service_ids) or "-",
                        created["id"],
                    )
        finally:
            await self._release_subscription_key_lock(key, lock)

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
