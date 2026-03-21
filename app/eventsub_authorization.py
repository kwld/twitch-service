from __future__ import annotations

from typing import Literal

from app.eventsub_catalog import required_scope_any_of_groups, supported_twitch_transports

InterestAuthorizationSource = Literal["auto", "broadcaster", "bot_moderator"]
PersistedAuthorizationSource = Literal["broadcaster", "bot_moderator"]

DEFAULT_AUTHORIZATION_SOURCE: PersistedAuthorizationSource = "broadcaster"


def event_supports_authorization_source_selection(event_type: str) -> bool:
    return bool(required_scope_any_of_groups(event_type)) and "websocket" in supported_twitch_transports(event_type)


def normalize_interest_authorization_source(
    event_type: str,
    source: str | None,
) -> InterestAuthorizationSource:
    normalized = str(source or "auto").strip().lower()
    if normalized not in {"auto", "broadcaster", "bot_moderator"}:
        normalized = "auto"
    if not event_supports_authorization_source_selection(event_type):
        return "broadcaster" if normalized != "auto" else "auto"
    return normalized  # type: ignore[return-value]


def normalize_persisted_authorization_source(
    event_type: str,
    source: str | None,
) -> PersistedAuthorizationSource:
    normalized = str(source or DEFAULT_AUTHORIZATION_SOURCE).strip().lower()
    if not event_supports_authorization_source_selection(event_type):
        return DEFAULT_AUTHORIZATION_SOURCE
    if normalized == "bot_moderator":
        return "bot_moderator"
    return "broadcaster"


def supported_authorization_sources(event_type: str) -> list[PersistedAuthorizationSource]:
    if event_supports_authorization_source_selection(event_type):
        return ["broadcaster", "bot_moderator"]
    return [DEFAULT_AUTHORIZATION_SOURCE]
