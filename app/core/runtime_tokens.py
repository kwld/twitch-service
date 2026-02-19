from __future__ import annotations

import asyncio
import secrets
import uuid
from datetime import UTC, datetime, timedelta


class WsTokenStore:
    def __init__(self, ttl: timedelta) -> None:
        self._ttl = ttl
        self._tokens: dict[str, tuple[uuid.UUID, datetime]] = {}
        self._lock = asyncio.Lock()

    async def issue(self, service_account_id: uuid.UUID) -> tuple[str, int]:
        token = secrets.token_urlsafe(32)
        expires_at = datetime.now(UTC) + self._ttl
        async with self._lock:
            self._prune_expired_locked(now=datetime.now(UTC))
            self._tokens[token] = (service_account_id, expires_at)
        return token, int(self._ttl.total_seconds())

    async def consume(self, token: str) -> uuid.UUID | None:
        now = datetime.now(UTC)
        async with self._lock:
            self._prune_expired_locked(now=now)
            payload = self._tokens.pop(token, None)
        if not payload:
            return None
        service_account_id, expires_at = payload
        if expires_at <= now:
            return None
        return service_account_id

    def _prune_expired_locked(self, now: datetime) -> None:
        expired = [k for k, (_, exp) in self._tokens.items() if exp <= now]
        for key in expired:
            self._tokens.pop(key, None)


class EventSubMessageDeduper:
    def __init__(self, ttl: timedelta) -> None:
        self._ttl = ttl
        self._seen: dict[str, datetime] = {}
        self._lock = asyncio.Lock()

    async def is_new(self, message_id: str) -> bool:
        if not message_id:
            return False
        now = datetime.now(UTC)
        async with self._lock:
            threshold = now - self._ttl
            expired = [k for k, seen_at in self._seen.items() if seen_at < threshold]
            for key in expired:
                self._seen.pop(key, None)
            if message_id in self._seen:
                return False
            self._seen[message_id] = now
            return True

