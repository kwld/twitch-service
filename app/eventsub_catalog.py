from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


# Source: https://dev.twitch.tv/docs/eventsub/eventsub-subscription-types/
# Snapshot date: 2026-02-17
SOURCE_URL = "https://dev.twitch.tv/docs/eventsub/eventsub-subscription-types/"
SOURCE_SNAPSHOT_DATE = "2026-02-17"


@dataclass(frozen=True, slots=True)
class EventSubCatalogEntry:
    title: str
    event_type: str
    version: str
    description: str
    status: Literal["stable", "new", "beta"] = "stable"


EVENTSUB_CATALOG: tuple[EventSubCatalogEntry, ...] = (
    EventSubCatalogEntry("Automod Message Hold", "automod.message.hold", "1", "Message caught by AutoMod."),
    EventSubCatalogEntry(
        "Automod Message Hold V2",
        "automod.message.hold",
        "2",
        "Message caught by AutoMod (public blocked terms only).",
        "new",
    ),
    EventSubCatalogEntry(
        "Automod Message Update",
        "automod.message.update",
        "1",
        "AutoMod queue message status changed.",
    ),
    EventSubCatalogEntry(
        "Automod Message Update V2",
        "automod.message.update",
        "2",
        "AutoMod queue message status changed (public blocked terms only).",
        "new",
    ),
    EventSubCatalogEntry(
        "Automod Settings Update",
        "automod.settings.update",
        "1",
        "Broadcaster AutoMod settings updated.",
    ),
    EventSubCatalogEntry(
        "Automod Terms Update",
        "automod.terms.update",
        "1",
        "Broadcaster AutoMod terms updated.",
    ),
    EventSubCatalogEntry("Channel Bits Use", "channel.bits.use", "1", "Bits used on channel.", "new"),
    EventSubCatalogEntry("Channel Update", "channel.update", "2", "Channel metadata updated."),
    EventSubCatalogEntry("Channel Follow", "channel.follow", "2", "User followed channel."),
    EventSubCatalogEntry("Channel Ad Break Begin", "channel.ad_break.begin", "1", "Ad break started."),
    EventSubCatalogEntry("Channel Chat Clear", "channel.chat.clear", "1", "Chat room messages cleared."),
    EventSubCatalogEntry(
        "Channel Chat Clear User Messages",
        "channel.chat.clear_user_messages",
        "1",
        "Specific user chat messages cleared.",
    ),
    EventSubCatalogEntry("Channel Chat Message", "channel.chat.message", "1", "Chat message sent.", "new"),
    EventSubCatalogEntry(
        "Channel Chat Message Delete",
        "channel.chat.message_delete",
        "1",
        "Specific chat message deleted.",
    ),
    EventSubCatalogEntry(
        "Channel Chat Notification",
        "channel.chat.notification",
        "1",
        "Chat UI notification event occurred.",
    ),
    EventSubCatalogEntry(
        "Channel Chat Settings Update",
        "channel.chat_settings.update",
        "1",
        "Chat settings updated.",
        "new",
    ),
    EventSubCatalogEntry(
        "Channel Chat User Message Hold",
        "channel.chat.user_message_hold",
        "1",
        "User message held by AutoMod.",
        "new",
    ),
    EventSubCatalogEntry(
        "Channel Chat User Message Update",
        "channel.chat.user_message_update",
        "1",
        "Held user message moderation state changed.",
        "new",
    ),
    EventSubCatalogEntry(
        "Channel Shared Chat Session Begin",
        "channel.shared_chat.begin",
        "1",
        "Channel joined a shared chat session.",
        "new",
    ),
    EventSubCatalogEntry(
        "Channel Shared Chat Session Update",
        "channel.shared_chat.update",
        "1",
        "Shared chat session changed.",
        "new",
    ),
    EventSubCatalogEntry(
        "Channel Shared Chat Session End",
        "channel.shared_chat.end",
        "1",
        "Channel left shared chat session.",
    ),
    EventSubCatalogEntry("Channel Subscribe", "channel.subscribe", "1", "New subscription."),
    EventSubCatalogEntry("Channel Subscription End", "channel.subscription.end", "1", "Subscription ended."),
    EventSubCatalogEntry(
        "Channel Subscription Gift",
        "channel.subscription.gift",
        "1",
        "Gift subscription sent.",
    ),
    EventSubCatalogEntry(
        "Channel Subscription Message",
        "channel.subscription.message",
        "1",
        "Resubscription chat message.",
    ),
    EventSubCatalogEntry("Channel Cheer", "channel.cheer", "1", "Bits cheer event."),
    EventSubCatalogEntry("Channel Raid", "channel.raid", "1", "Channel raid event."),
    EventSubCatalogEntry("Channel Ban", "channel.ban", "1", "User banned."),
    EventSubCatalogEntry("Channel Unban", "channel.unban", "1", "User unbanned."),
    EventSubCatalogEntry(
        "Channel Unban Request Create",
        "channel.unban_request.create",
        "1",
        "Unban request created.",
        "new",
    ),
    EventSubCatalogEntry(
        "Channel Unban Request Resolve",
        "channel.unban_request.resolve",
        "1",
        "Unban request resolved.",
        "new",
    ),
    EventSubCatalogEntry("Channel Moderate", "channel.moderate", "1", "Moderation action."),
    EventSubCatalogEntry(
        "Channel Moderate V2",
        "channel.moderate",
        "2",
        "Moderation action (includes warnings).",
        "new",
    ),
    EventSubCatalogEntry("Channel Moderator Add", "channel.moderator.add", "1", "Moderator added."),
    EventSubCatalogEntry("Channel Moderator Remove", "channel.moderator.remove", "1", "Moderator removed."),
    EventSubCatalogEntry(
        "Channel Guest Star Session Begin",
        "channel.guest_star_session.begin",
        "beta",
        "Guest Star session started.",
        "beta",
    ),
    EventSubCatalogEntry(
        "Channel Guest Star Session End",
        "channel.guest_star_session.end",
        "beta",
        "Guest Star session ended.",
        "beta",
    ),
    EventSubCatalogEntry(
        "Channel Guest Star Guest Update",
        "channel.guest_star_guest.update",
        "beta",
        "Guest Star guest/slot updated.",
        "beta",
    ),
    EventSubCatalogEntry(
        "Channel Guest Star Settings Update",
        "channel.guest_star_settings.update",
        "beta",
        "Guest Star settings updated.",
        "beta",
    ),
    EventSubCatalogEntry(
        "Channel Points Automatic Reward Redemption Add",
        "channel.channel_points_automatic_reward_redemption.add",
        "1",
        "Automatic reward redeemed.",
    ),
    EventSubCatalogEntry(
        "Channel Points Automatic Reward Redemption Add V2",
        "channel.channel_points_automatic_reward_redemption.add",
        "2",
        "Automatic reward redeemed.",
        "new",
    ),
    EventSubCatalogEntry(
        "Channel Points Custom Reward Add",
        "channel.channel_points_custom_reward.add",
        "1",
        "Custom reward created.",
    ),
    EventSubCatalogEntry(
        "Channel Points Custom Reward Update",
        "channel.channel_points_custom_reward.update",
        "1",
        "Custom reward updated.",
    ),
    EventSubCatalogEntry(
        "Channel Points Custom Reward Remove",
        "channel.channel_points_custom_reward.remove",
        "1",
        "Custom reward removed.",
    ),
    EventSubCatalogEntry(
        "Channel Points Custom Reward Redemption Add",
        "channel.channel_points_custom_reward_redemption.add",
        "1",
        "Custom reward redeemed.",
    ),
    EventSubCatalogEntry(
        "Channel Points Custom Reward Redemption Update",
        "channel.channel_points_custom_reward_redemption.update",
        "1",
        "Custom reward redemption updated.",
    ),
    EventSubCatalogEntry("Channel Poll Begin", "channel.poll.begin", "1", "Poll started."),
    EventSubCatalogEntry("Channel Poll Progress", "channel.poll.progress", "1", "Poll vote update."),
    EventSubCatalogEntry("Channel Poll End", "channel.poll.end", "1", "Poll ended."),
    EventSubCatalogEntry("Channel Prediction Begin", "channel.prediction.begin", "1", "Prediction started."),
    EventSubCatalogEntry(
        "Channel Prediction Progress",
        "channel.prediction.progress",
        "1",
        "Prediction vote update.",
    ),
    EventSubCatalogEntry("Channel Prediction Lock", "channel.prediction.lock", "1", "Prediction locked."),
    EventSubCatalogEntry("Channel Prediction End", "channel.prediction.end", "1", "Prediction ended."),
    EventSubCatalogEntry(
        "Channel Suspicious User Message",
        "channel.suspicious_user.message",
        "1",
        "Suspicious user message sent.",
        "new",
    ),
    EventSubCatalogEntry(
        "Channel Suspicious User Update",
        "channel.suspicious_user.update",
        "1",
        "Suspicious user state updated.",
        "new",
    ),
    EventSubCatalogEntry("Channel VIP Add", "channel.vip.add", "1", "VIP added.", "new"),
    EventSubCatalogEntry("Channel VIP Remove", "channel.vip.remove", "1", "VIP removed.", "new"),
    EventSubCatalogEntry(
        "Channel Warning Acknowledge",
        "channel.warning.acknowledge",
        "1",
        "Warning acknowledged.",
        "new",
    ),
    EventSubCatalogEntry("Channel Warning Send", "channel.warning.send", "1", "Warning sent.", "new"),
    EventSubCatalogEntry(
        "Charity Donation",
        "channel.charity_campaign.donate",
        "1",
        "Charity donation made.",
    ),
    EventSubCatalogEntry(
        "Charity Campaign Start",
        "channel.charity_campaign.start",
        "1",
        "Charity campaign started.",
    ),
    EventSubCatalogEntry(
        "Charity Campaign Progress",
        "channel.charity_campaign.progress",
        "1",
        "Charity campaign progress update.",
    ),
    EventSubCatalogEntry(
        "Charity Campaign Stop",
        "channel.charity_campaign.stop",
        "1",
        "Charity campaign stopped.",
    ),
    EventSubCatalogEntry(
        "Conduit Shard Disabled",
        "conduit.shard.disabled",
        "1",
        "Conduit shard disabled.",
        "new",
    ),
    EventSubCatalogEntry("Drop Entitlement Grant", "drop.entitlement.grant", "1", "Drop entitlement granted."),
    EventSubCatalogEntry(
        "Extension Bits Transaction Create",
        "extension.bits_transaction.create",
        "1",
        "Extension Bits transaction.",
    ),
    EventSubCatalogEntry("Goal Begin", "channel.goal.begin", "1", "Goal started."),
    EventSubCatalogEntry("Goal Progress", "channel.goal.progress", "1", "Goal progress update."),
    EventSubCatalogEntry("Goal End", "channel.goal.end", "1", "Goal ended."),
    EventSubCatalogEntry("Hype Train Begin", "channel.hype_train.begin", "2", "Hype Train started."),
    EventSubCatalogEntry("Hype Train Progress", "channel.hype_train.progress", "2", "Hype Train progress."),
    EventSubCatalogEntry("Hype Train End", "channel.hype_train.end", "2", "Hype Train ended."),
    EventSubCatalogEntry("Shield Mode Begin", "channel.shield_mode.begin", "1", "Shield Mode enabled."),
    EventSubCatalogEntry("Shield Mode End", "channel.shield_mode.end", "1", "Shield Mode disabled."),
    EventSubCatalogEntry("Shoutout Create", "channel.shoutout.create", "1", "Shoutout sent."),
    EventSubCatalogEntry("Shoutout Receive", "channel.shoutout.receive", "1", "Shoutout received."),
    EventSubCatalogEntry("Stream Online", "stream.online", "1", "Stream started."),
    EventSubCatalogEntry("Stream Offline", "stream.offline", "1", "Stream stopped."),
    EventSubCatalogEntry(
        "User Authorization Grant",
        "user.authorization.grant",
        "1",
        "User authorized client ID.",
    ),
    EventSubCatalogEntry(
        "User Authorization Revoke",
        "user.authorization.revoke",
        "1",
        "User revoked client ID authorization.",
    ),
    EventSubCatalogEntry("User Update", "user.update", "1", "User account updated."),
    EventSubCatalogEntry("Whisper Received", "user.whisper.message", "1", "User received whisper.", "new"),
)


KNOWN_EVENT_TYPES: frozenset[str] = frozenset(entry.event_type for entry in EVENTSUB_CATALOG)
_VERSIONS_BY_EVENT_TYPE: dict[str, list[str]] = {}
for _entry in EVENTSUB_CATALOG:
    _VERSIONS_BY_EVENT_TYPE.setdefault(_entry.event_type, []).append(_entry.version)


# Per Twitch docs (EventSub Subscription Types), these are webhook-only and cannot use WebSockets.
WEBSOCKET_UNSUPPORTED_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "drop.entitlement.grant",
        "extension.bits_transaction.create",
        "user.authorization.grant",
        "user.authorization.revoke",
    }
)


def supported_twitch_transports(event_type: str) -> list[Literal["webhook", "websocket"]]:
    normalized = event_type.strip().lower()
    if normalized in WEBSOCKET_UNSUPPORTED_EVENT_TYPES:
        return ["webhook"]
    return ["webhook", "websocket"]


def best_transport_for_service(
    event_type: str,
    webhook_available: bool,
) -> tuple[Literal["webhook", "websocket"], str]:
    transports = supported_twitch_transports(event_type)
    normalized = event_type.strip().lower()
    if normalized == "user.authorization.revoke":
        return "webhook", "Webhook-only by Twitch; required for authorization revoke handling."
    if webhook_available and "webhook" in transports:
        return (
            "webhook",
            "Webhook preferred for hosted services; app-token EventSub flow and durable delivery.",
        )
    if "websocket" in transports:
        return "websocket", "Webhook callback not configured; using websocket fallback."
    return "webhook", "Webhook-only by Twitch."


def preferred_eventsub_version(event_type: str) -> str:
    versions = _VERSIONS_BY_EVENT_TYPE.get(event_type.strip().lower(), [])
    numeric = sorted((int(v) for v in versions if str(v).isdigit()), reverse=True)
    if numeric:
        return str(numeric[0])
    return "1"


def requires_condition_user_id(event_type: str) -> bool:
    normalized = event_type.strip().lower()
    return normalized.startswith("channel.chat.") or normalized == "channel.chat_settings.update"


def required_scope_any_of_groups(event_type: str) -> list[set[str]]:
    normalized = event_type.strip().lower()
    if normalized.startswith("channel.channel_points_custom_reward"):
        return [{"channel:read:redemptions", "channel:manage:redemptions"}]
    if normalized.startswith("channel.channel_points_custom_reward_redemption"):
        return [{"channel:read:redemptions", "channel:manage:redemptions"}]
    if normalized.startswith("channel.poll."):
        return [{"channel:read:polls", "channel:manage:polls"}]
    if normalized.startswith("channel.prediction."):
        return [{"channel:read:predictions", "channel:manage:predictions"}]
    if normalized.startswith("channel.goal."):
        return [{"channel:read:goals"}]
    if normalized.startswith("channel.charity_campaign."):
        return [{"channel:read:charity"}]
    if normalized == "channel.ad_break.begin":
        return [{"channel:read:ads"}]
    if normalized.startswith("channel.hype_train."):
        return [{"channel:read:hype_train"}]
    return []


def recommended_broadcaster_scopes(event_type: str) -> set[str]:
    normalized = event_type.strip().lower()
    if normalized.startswith("channel.channel_points_custom_reward"):
        return {"channel:read:redemptions"}
    if normalized.startswith("channel.channel_points_custom_reward_redemption"):
        return {"channel:read:redemptions"}
    if normalized.startswith("channel.poll."):
        return {"channel:read:polls"}
    if normalized.startswith("channel.prediction."):
        return {"channel:read:predictions"}
    if normalized.startswith("channel.goal."):
        return {"channel:read:goals"}
    if normalized.startswith("channel.charity_campaign."):
        return {"channel:read:charity"}
    if normalized == "channel.ad_break.begin":
        return {"channel:read:ads"}
    if normalized.startswith("channel.hype_train."):
        return {"channel:read:hype_train"}
    return set()

