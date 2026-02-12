from __future__ import annotations

import asyncio
import json
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx
from fastapi import WebSocket

from app.models import ServiceInterest


@dataclass(frozen=True, slots=True)
class InterestKey:
    bot_account_id: uuid.UUID
    event_type: str
    broadcaster_user_id: str


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
                )
                self._by_key[key].add(interest.id)

    async def add(self, interest: ServiceInterest) -> InterestKey:
        key = InterestKey(
            bot_account_id=interest.bot_account_id,
            event_type=interest.event_type,
            broadcaster_user_id=interest.broadcaster_user_id,
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

    async def publish_to_service(self, service_account_id: uuid.UUID, payload: dict) -> None:
        async with self._lock:
            sockets = list(self._clients.get(service_account_id, set()))
        if not sockets:
            return
        text = json.dumps(payload, default=str)
        dead: list[WebSocket] = []
        for ws in sockets:
            try:
                await ws.send_text(text)
            except Exception:
                dead.append(ws)
        if dead:
            async with self._lock:
                for ws in dead:
                    self._clients[service_account_id].discard(ws)
        if self.on_service_ws_event and sockets:
            await self.on_service_ws_event(service_account_id)

    async def publish_webhook(
        self,
        service_account_id: uuid.UUID,
        url: str,
        payload: dict,
        timeout: int = 10,
    ) -> None:
        async with httpx.AsyncClient(timeout=timeout) as client:
            await client.post(url, json=payload)
        if self.on_service_webhook_event:
            await self.on_service_webhook_event(service_account_id)

    def envelope(self, message_id: str, event_type: str, event: dict) -> dict:
        return {
            "id": message_id,
            "provider": "twitch",
            "type": event_type,
            "event_timestamp": datetime.now(UTC).isoformat(),
            "event": event,
        }
