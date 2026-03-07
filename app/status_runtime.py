from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from datetime import UTC, datetime
from hashlib import sha1
from typing import Any

from fastapi import WebSocket


def utc_now() -> datetime:
    return datetime.now(UTC)


class StatusLogHandler(logging.Handler):
    def __init__(self, runtime: "StatusRuntime") -> None:
        super().__init__(level=logging.INFO)
        self.runtime = runtime

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if record.name.startswith("eventsub.audit") or record.name.startswith("httpx"):
                return
            if record.levelno < logging.INFO:
                return
            self.runtime.append_log(
                {
                    "timestamp": utc_now().isoformat(),
                    "level": record.levelname,
                    "logger": record.name,
                    "message": record.getMessage(),
                }
            )
        except Exception:
            return


class StatusRuntime:
    def __init__(self, *, max_logs: int = 200) -> None:
        self.started_at = utc_now()
        self._recent_logs: deque[dict[str, Any]] = deque(maxlen=max_logs)
        self._clients: set[WebSocket] = set()
        self._lock = asyncio.Lock()

    def append_log(self, entry: dict[str, Any]) -> None:
        self._recent_logs.append(entry)

    def get_recent_logs(self, limit: int = 80) -> list[dict[str, Any]]:
        rows = list(self._recent_logs)
        if limit > 0:
            rows = rows[-limit:]
        return rows

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        async with self._lock:
            self._clients.add(websocket)

    async def disconnect(self, websocket: WebSocket) -> None:
        async with self._lock:
            self._clients.discard(websocket)

    async def broadcast_snapshot(self, snapshot: dict[str, Any]) -> None:
        payload = json.dumps(
            {
                "type": "status_snapshot",
                "generated_at": snapshot.get("generated_at"),
                "payload": snapshot,
                "hash": sha1(json.dumps(snapshot, sort_keys=True, default=str).encode("utf-8")).hexdigest(),
            },
            default=str,
        )
        async with self._lock:
            clients = list(self._clients)
        if not clients:
            return
        results = await asyncio.gather(
            *(client.send_text(payload) for client in clients),
            return_exceptions=True,
        )
        dead = [client for client, result in zip(clients, results, strict=False) if isinstance(result, Exception)]
        if dead:
            async with self._lock:
                for client in dead:
                    self._clients.discard(client)
