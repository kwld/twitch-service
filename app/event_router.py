from __future__ import annotations

import asyncio
import json
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
import time

import httpx
from fastapi import WebSocket

from app.models import ServiceInterest


@dataclass(frozen=True, slots=True)
class InterestKey:
    bot_account_id: uuid.UUID
    event_type: str
    broadcaster_user_id: str
    authorization_source: str = "broadcaster"
    raid_direction: str = ""


class InterestRegistry:
    def __init__(self) -> None:
        self._by_key: dict[InterestKey, set[uuid.UUID]] = defaultdict(set)
        self._interests: dict[uuid.UUID, ServiceInterest] = {}
        self._lock = asyncio.Lock()

    async def load(self, interests: list[ServiceInterest]) -> None:
        async with self._lock:
            self._by_key.clear()
            self._interests.clear()
            for interest in interests:
                self._interests[interest.id] = interest
                key = InterestKey(
                    bot_account_id=interest.bot_account_id,
                    event_type=interest.event_type,
                    broadcaster_user_id=interest.broadcaster_user_id,
                    authorization_source=interest.authorization_source or "broadcaster",
                    raid_direction=interest.raid_direction or "",
                )
                self._by_key[key].add(interest.id)

    async def add(self, interest: ServiceInterest) -> InterestKey:
        key = InterestKey(
            bot_account_id=interest.bot_account_id,
            event_type=interest.event_type,
            broadcaster_user_id=interest.broadcaster_user_id,
            authorization_source=interest.authorization_source or "broadcaster",
            raid_direction=interest.raid_direction or "",
        )
        async with self._lock:
            self._interests[interest.id] = interest
            self._by_key[key].add(interest.id)
        return key

    async def remove(self, interest: ServiceInterest) -> tuple[InterestKey, bool]:
        key = InterestKey(
            bot_account_id=interest.bot_account_id,
            event_type=interest.event_type,
            broadcaster_user_id=interest.broadcaster_user_id,
            authorization_source=interest.authorization_source or "broadcaster",
            raid_direction=interest.raid_direction or "",
        )
        async with self._lock:
            self._interests.pop(interest.id, None)
            ids = self._by_key.get(key)
            if ids:
                ids.discard(interest.id)
                if not ids:
                    self._by_key.pop(key, None)
        async with self._lock:
            still_used = key in self._by_key
        return key, still_used

    async def keys(self) -> list[InterestKey]:
        async with self._lock:
            return list(self._by_key.keys())

    async def has_key(self, key: InterestKey) -> bool:
        async with self._lock:
            return key in self._by_key

    async def interested(self, key: InterestKey) -> list[ServiceInterest]:
        async with self._lock:
            ids = self._by_key.get(key, set()).copy()
            return [self._interests[i] for i in ids if i in self._interests]


class LocalEventHub:
    def __init__(self) -> None:
        self._clients: dict[uuid.UUID, set[WebSocket]] = defaultdict(set)
        self._lock = asyncio.Lock()
        self._http_client = httpx.AsyncClient(
            timeout=10.0,
            limits=httpx.Limits(max_connections=200, max_keepalive_connections=50),
        )
        self.on_service_connect = None
        self.on_service_disconnect = None
        self.on_service_ws_event = None
        self.on_service_webhook_event = None

    async def connect(self, service_account_id: uuid.UUID, websocket: WebSocket) -> None:
        await websocket.accept()
        async with self._lock:
            self._clients[service_account_id].add(websocket)
        if self.on_service_connect:
            await self.on_service_connect(service_account_id)

    async def disconnect(self, service_account_id: uuid.UUID, websocket: WebSocket) -> None:
        async with self._lock:
            if service_account_id in self._clients:
                self._clients[service_account_id].discard(websocket)
                if not self._clients[service_account_id]:
                    self._clients.pop(service_account_id, None)
        if self.on_service_disconnect:
            await self.on_service_disconnect(service_account_id)

    async def active_connections(self, service_account_id: uuid.UUID) -> int:
        async with self._lock:
            return len(self._clients.get(service_account_id, set()))

    async def publish_to_service(self, service_account_id: uuid.UUID, payload: dict) -> dict:
        started = time.perf_counter()
        async with self._lock:
            sockets = list(self._clients.get(service_account_id, set()))
        if not sockets:
            return {
                "outcome": "no_listener",
                "duration_ms": int((time.perf_counter() - started) * 1000),
                "listener_count": 0,
                "delivered_count": 0,
                "failed_count": 0,
            }
        text = json.dumps(payload, default=str)
        send_results = await asyncio.gather(
            *(ws.send_text(text) for ws in sockets),
            return_exceptions=True,
        )
        dead = [ws for ws, result in zip(sockets, send_results, strict=False) if isinstance(result, Exception)]
        delivered_count = sum(1 for result in send_results if not isinstance(result, Exception))
        if dead:
            async with self._lock:
                for ws in dead:
                    self._clients[service_account_id].discard(ws)
        if self.on_service_ws_event and sockets:
            await self.on_service_ws_event(service_account_id)
        return {
            "outcome": "delivered" if delivered_count > 0 else "failed",
            "duration_ms": int((time.perf_counter() - started) * 1000),
            "listener_count": len(sockets),
            "delivered_count": delivered_count,
            "failed_count": len(dead),
        }

    async def publish_webhook(
        self,
        service_account_id: uuid.UUID,
        url: str,
        payload: dict,
        timeout: int = 10,
    ) -> dict:
        started = time.perf_counter()
        try:
            response = await self._http_client.post(url, json=payload, timeout=timeout)
            response.raise_for_status()
        except Exception as exc:
            return {
                "outcome": "failed",
                "duration_ms": int((time.perf_counter() - started) * 1000),
                "status_code": getattr(getattr(exc, "response", None), "status_code", None),
                "error": str(exc),
            }
        if self.on_service_webhook_event:
            await self.on_service_webhook_event(service_account_id)
        return {
            "outcome": "delivered",
            "duration_ms": int((time.perf_counter() - started) * 1000),
            "status_code": response.status_code,
        }

    async def close(self) -> None:
        await self._http_client.aclose()

    def envelope(self, message_id: str, event_type: str, event: dict) -> dict:
        return {
            "id": message_id,
            "provider": "twitch",
            "type": event_type,
            "event_timestamp": datetime.now(UTC).isoformat(),
            "event": event,
        }
