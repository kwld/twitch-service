from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from app.twitch import TwitchClient, TwitchApiError

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class _CacheEntry:
    value: Any
    expires_at: datetime


def _now() -> datetime:
    return datetime.now(UTC)


class TwitchChatAssetCache:
    """
    Async, in-memory cache for Twitch chat badges and emotes (global + per broadcaster).

    Goals:
    - avoid per-message Helix calls
    - best-effort enrichment for EventSub chat notifications
    - keep old services compatible by making enrichment optional
    """

    def __init__(
        self,
        twitch: TwitchClient,
        ttl: timedelta = timedelta(hours=6),
        stale_if_error: timedelta = timedelta(hours=24),
    ) -> None:
        self.twitch = twitch
        self.ttl = ttl
        self.stale_if_error = stale_if_error

        self._lock = asyncio.Lock()
        self._global_badges: _CacheEntry | None = None
        self._global_emotes: _CacheEntry | None = None
        self._channel_badges: dict[str, _CacheEntry] = {}
        self._channel_emotes: dict[str, _CacheEntry] = {}

        # Avoid thundering herd per broadcaster.
        self._inflight: set[tuple[str, str]] = set()

    async def _set(self, kind: str, broadcaster_id: str | None, value: Any, ttl: timedelta | None = None) -> None:
        entry = _CacheEntry(value=value, expires_at=_now() + (ttl or self.ttl))
        async with self._lock:
            if kind == "global_badges":
                self._global_badges = entry
            elif kind == "global_emotes":
                self._global_emotes = entry
            elif kind == "channel_badges" and broadcaster_id:
                self._channel_badges[broadcaster_id] = entry
            elif kind == "channel_emotes" and broadcaster_id:
                self._channel_emotes[broadcaster_id] = entry

    async def _get(self, kind: str, broadcaster_id: str | None) -> _CacheEntry | None:
        async with self._lock:
            if kind == "global_badges":
                return self._global_badges
            if kind == "global_emotes":
                return self._global_emotes
            if kind == "channel_badges" and broadcaster_id:
                return self._channel_badges.get(broadcaster_id)
            if kind == "channel_emotes" and broadcaster_id:
                return self._channel_emotes.get(broadcaster_id)
        return None

    @staticmethod
    def _is_fresh(entry: _CacheEntry | None) -> bool:
        return bool(entry and _now() < entry.expires_at)

    async def _refresh_global_badges(self) -> dict:
        token = await self.twitch.app_access_token()
        payload = await self.twitch.get_global_chat_badges(access_token=token)
        await self._set("global_badges", None, payload)
        return payload

    async def _refresh_channel_badges(self, broadcaster_id: str) -> dict:
        token = await self.twitch.app_access_token()
        payload = await self.twitch.get_channel_chat_badges(broadcaster_id=broadcaster_id, access_token=token)
        await self._set("channel_badges", broadcaster_id, payload)
        return payload

    async def _refresh_global_emotes(self) -> dict:
        token = await self.twitch.app_access_token()
        payload = await self.twitch.get_global_emotes(access_token=token)
        await self._set("global_emotes", None, payload)
        return payload

    async def _refresh_channel_emotes(self, broadcaster_id: str) -> dict:
        token = await self.twitch.app_access_token()
        payload = await self.twitch.get_channel_emotes(broadcaster_id=broadcaster_id, access_token=token)
        await self._set("channel_emotes", broadcaster_id, payload)
        return payload

    def prefetch(self, broadcaster_id: str) -> None:
        # Fire-and-forget refresh; used on interest creation.
        for key in (
            ("global_badges", ""),
            ("global_emotes", ""),
            ("channel_badges", broadcaster_id),
            ("channel_emotes", broadcaster_id),
        ):
            asyncio.create_task(self._ensure_fresh(*key), name=f"twitch-chat-assets:{key[0]}:{key[1]}")

    async def refresh(self, broadcaster_id: str) -> None:
        # Force-refresh synchronously (used by the explicit API endpoint).
        await self._refresh_global_badges()
        await self._refresh_global_emotes()
        await self._refresh_channel_badges(broadcaster_id)
        await self._refresh_channel_emotes(broadcaster_id)

    async def snapshot(self, broadcaster_id: str) -> dict[str, Any]:
        global_badges = await self._get("global_badges", None)
        global_emotes = await self._get("global_emotes", None)
        channel_badges = await self._get("channel_badges", broadcaster_id)
        channel_emotes = await self._get("channel_emotes", broadcaster_id)
        return {
            "badges": {
                "global": global_badges.value if global_badges else {"data": []},
                "channel": channel_badges.value if channel_badges else {"data": []},
            },
            "emotes": {
                "global": global_emotes.value if global_emotes else {"data": []},
                "channel": channel_emotes.value if channel_emotes else {"data": []},
            },
        }

    async def _ensure_fresh(self, kind: str, broadcaster_id: str) -> None:
        b = broadcaster_id or None
        existing = await self._get(kind, b)
        if self._is_fresh(existing):
            return

        inflight_key = (kind, broadcaster_id)
        async with self._lock:
            if inflight_key in self._inflight:
                return
            self._inflight.add(inflight_key)

        try:
            if kind == "global_badges":
                await self._refresh_global_badges()
            elif kind == "global_emotes":
                await self._refresh_global_emotes()
            elif kind == "channel_badges" and b:
                await self._refresh_channel_badges(b)
            elif kind == "channel_emotes" and b:
                await self._refresh_channel_emotes(b)
        except Exception as exc:
            # Keep any old value around a bit longer to avoid repeated retries.
            logger.info("Failed refreshing %s for %s: %s", kind, broadcaster_id or "global", exc)
            if existing:
                await self._set(kind, b, existing.value, ttl=self.stale_if_error)
        finally:
            async with self._lock:
                self._inflight.discard(inflight_key)

    @staticmethod
    def _badge_map(payload: dict) -> dict[str, dict]:
        # Returns key "set_id/version_id" -> {set_id, id, title, images:{1x,2x,4x}}
        out: dict[str, dict] = {}
        for set_obj in (payload or {}).get("data", []) or []:
            set_id = str(set_obj.get("set_id", ""))
            for v in set_obj.get("versions", []) or []:
                vid = str(v.get("id", ""))
                if not set_id or not vid:
                    continue
                out[f"{set_id}/{vid}"] = {
                    "set_id": set_id,
                    "id": vid,
                    "title": v.get("title") or "",
                    "images": v.get("image_url") or v.get("image_url_1x") or None,
                    "image_url_1x": v.get("image_url_1x"),
                    "image_url_2x": v.get("image_url_2x"),
                    "image_url_4x": v.get("image_url_4x"),
                }
        return out

    @staticmethod
    def _emote_map(payload: dict) -> dict[str, dict]:
        # Returns emote_id -> {id,name,images:{url_1x,url_2x,url_4x}}
        out: dict[str, dict] = {}
        for e in (payload or {}).get("data", []) or []:
            eid = str(e.get("id", ""))
            if not eid:
                continue
            images = e.get("images") or {}
            out[eid] = {
                "id": eid,
                "name": e.get("name") or "",
                "images": images,
                "format": e.get("format"),
                "scale": e.get("scale"),
                "theme_mode": e.get("theme_mode"),
            }
        return out

    async def enrich_chat_event(self, broadcaster_id: str, event: dict) -> dict:
        """
        Returns enrichment payload for a channel.chat.* EventSub event.
        Never raises; returns empty dict on errors/missing data.
        """
        try:
            # Best-effort: trigger refresh if missing/stale, but don't block message delivery.
            self.prefetch(broadcaster_id)

            global_badges = (await self._get("global_badges", None)) or _CacheEntry({"data": []}, _now())
            global_emotes = (await self._get("global_emotes", None)) or _CacheEntry({"data": []}, _now())
            channel_badges = (await self._get("channel_badges", broadcaster_id)) or _CacheEntry({"data": []}, _now())
            channel_emotes = (await self._get("channel_emotes", broadcaster_id)) or _CacheEntry({"data": []}, _now())

            badge_lookup = {**self._badge_map(global_badges.value), **self._badge_map(channel_badges.value)}
            emote_lookup = {**self._emote_map(global_emotes.value), **self._emote_map(channel_emotes.value)}

            needed_badges: list[str] = []
            for b in event.get("badges", []) or []:
                set_id = str(b.get("set_id", ""))
                vid = str(b.get("id", ""))
                if set_id and vid:
                    needed_badges.append(f"{set_id}/{vid}")

            needed_emotes: list[str] = []
            fragments = ((event.get("message") or {}).get("fragments")) or []
            for frag in fragments:
                if (frag or {}).get("type") != "emote":
                    continue
                emote = (frag or {}).get("emote") or {}
                eid = str(emote.get("id", ""))
                if eid:
                    needed_emotes.append(eid)

            unique_badges = sorted(set(needed_badges))
            unique_emotes = sorted(set(needed_emotes))

            # First-message safety: if specific badges are missing in cache, try one immediate
            # refresh so clients can render Twitch-native badge images reliably.
            missing_badges = [k for k in unique_badges if k not in badge_lookup]
            if missing_badges:
                try:
                    await asyncio.gather(
                        self._refresh_global_badges(),
                        self._refresh_channel_badges(broadcaster_id),
                    )
                    global_badges = (await self._get("global_badges", None)) or global_badges
                    channel_badges = (await self._get("channel_badges", broadcaster_id)) or channel_badges
                    badge_lookup = {**self._badge_map(global_badges.value), **self._badge_map(channel_badges.value)}
                except Exception:
                    pass

            resolved_badges = [badge_lookup[k] for k in unique_badges if k in badge_lookup]
            resolved_emotes = [emote_lookup[eid] for eid in unique_emotes if eid in emote_lookup]

            missing_badges = [k for k in unique_badges if k not in badge_lookup]
            missing_emotes = [eid for eid in unique_emotes if eid not in emote_lookup]

            badge_image_map: dict[str, str] = {}
            badge_image_map_by_scale: dict[str, dict[str, str | None]] = {}
            for badge in resolved_badges:
                key = f"{badge.get('set_id', '')}/{badge.get('id', '')}"
                if not key or key == "/":
                    continue
                one_x = badge.get("image_url_1x")
                two_x = badge.get("image_url_2x")
                four_x = badge.get("image_url_4x")
                preferred = four_x or two_x or one_x
                if preferred:
                    badge_image_map[key] = str(preferred)
                badge_image_map_by_scale[key] = {
                    "1x": str(one_x) if one_x else None,
                    "2x": str(two_x) if two_x else None,
                    "4x": str(four_x) if four_x else None,
                }

            if not resolved_badges and not resolved_emotes:
                return {}

            return {
                "badges": resolved_badges,
                "emotes": resolved_emotes,
                "badge_image_map": badge_image_map,
                "badge_image_map_by_scale": badge_image_map_by_scale,
                "missing": {"badges": missing_badges, "emotes": missing_emotes},
            }
        except TwitchApiError:
            return {}
        except Exception:
            return {}
