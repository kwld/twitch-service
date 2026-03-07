from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.event_router import InterestRegistry
from app.models import ServiceAccount, ServiceInterest
from app.routes.service_routes import register_service_routes


class _ScalarResult:
    def __init__(self, rows):
        self._rows = list(rows)

    def all(self):
        return list(self._rows)


class DummySession:
    def __init__(self, interests):
        self._interests = list(interests)

    async def scalars(self, statement):
        text = str(statement)
        if "FROM service_interests" in text:
            return _ScalarResult(self._interests)
        return _ScalarResult([])


def make_session_factory(interests):
    @asynccontextmanager
    async def _factory():
        yield DummySession(interests)

    return _factory


class DummyEventSubManager:
    def __init__(self, db_snapshot, live_snapshot):
        self.db_snapshot = db_snapshot
        self.live_snapshot = live_snapshot
        self.db_calls = 0
        self.live_calls = 0

    async def get_db_active_subscriptions_snapshot(self):
        self.db_calls += 1
        return self.db_snapshot, datetime.now(UTC)

    async def get_active_subscriptions_snapshot(self, force_refresh=False):
        self.live_calls += 1
        return self.live_snapshot, datetime.now(UTC), False


def build_app(*, service, interests, manager, allowed_ids=None):
    app = FastAPI()

    async def _service_auth():
        return service

    async def _service_allowed_bot_ids(_session, _service_id):
        return set(allowed_ids or set())

    async def _filter_working_interests(_session, rows):
        return rows

    async def _issue_ws_token(_service_id):
        return "token", 120

    async def _record_service_trace(**_kwargs):
        return None

    async def _ensure_service_can_access_bot(_session, _service_id, _bot_account_id):
        return None

    async def _ensure_default_stream_interests(**_kwargs):
        return []

    async def _validate_webhook_target_url(_url):
        return None

    register_service_routes(
        app,
        session_factory=make_session_factory(interests),
        twitch_client=None,
        eventsub_manager=manager,
        service_auth=_service_auth,
        interest_registry=InterestRegistry(),
        logger=None,
        issue_ws_token=_issue_ws_token,
        record_service_trace=_record_service_trace,
        split_csv=lambda value: [v.strip() for v in str(value or "").split(",") if v.strip()],
        filter_working_interests=_filter_working_interests,
        service_allowed_bot_ids=_service_allowed_bot_ids,
        ensure_service_can_access_bot=_ensure_service_can_access_bot,
        ensure_default_stream_interests=_ensure_default_stream_interests,
        validate_webhook_target_url=_validate_webhook_target_url,
        normalize_broadcaster_id_or_login=lambda value: str(value).strip(),
        broadcaster_auth_scopes=("channel:bot",),
        service_user_auth_scopes=("user:read:email",),
    )
    return app


def _make_service() -> ServiceAccount:
    return ServiceAccount(
        id=uuid.uuid4(),
        name="svc",
        client_id="client",
        client_secret_hash="hash",
        enabled=True,
    )


def _make_interest(service_id: uuid.UUID, bot_id: uuid.UUID, event_type: str, broadcaster_user_id: str) -> ServiceInterest:
    now = datetime.now(UTC)
    return ServiceInterest(
        id=uuid.uuid4(),
        service_account_id=service_id,
        bot_account_id=bot_id,
        event_type=event_type,
        broadcaster_user_id=broadcaster_user_id,
        transport="websocket",
        webhook_url=None,
        last_heartbeat_at=now,
    )


def test_active_subscriptions_refresh_false_uses_db_snapshot_and_broadcaster_filter():
    service = _make_service()
    bot_id = uuid.uuid4()
    interests = [
        _make_interest(service.id, bot_id, "stream.online", "123"),
        _make_interest(service.id, bot_id, "stream.online", "999"),
    ]
    manager = DummyEventSubManager(
        db_snapshot=[
            {
                "twitch_subscription_id": "sub-123",
                "status": "enabled",
                "event_type": "stream.online",
                "broadcaster_user_id": "123",
                "upstream_transport": "websocket",
                "bot_account_id": str(bot_id),
                "session_id": "sess-1",
                "connected_at": None,
                "disconnected_at": None,
            },
            {
                "twitch_subscription_id": "sub-999",
                "status": "enabled",
                "event_type": "stream.online",
                "broadcaster_user_id": "999",
                "upstream_transport": "websocket",
                "bot_account_id": str(bot_id),
                "session_id": "sess-2",
                "connected_at": None,
                "disconnected_at": None,
            },
        ],
        live_snapshot=[],
    )

    app = build_app(service=service, interests=interests, manager=manager)
    client = TestClient(app)

    resp = client.get("/v1/eventsub/subscriptions/active?refresh=false&broadcaster_user_id=123")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["source"] == "cache"
    assert payload["matched_for_service"] == 1
    assert [item["twitch_subscription_id"] for item in payload["items"]] == ["sub-123"]
    assert manager.db_calls == 1
    assert manager.live_calls == 0


def test_active_subscriptions_refresh_true_uses_live_snapshot_and_allowed_bot_filter():
    service = _make_service()
    allowed_bot_id = uuid.uuid4()
    blocked_bot_id = uuid.uuid4()
    interests = [
        _make_interest(service.id, allowed_bot_id, "stream.online", "123"),
        _make_interest(service.id, blocked_bot_id, "stream.online", "456"),
    ]
    manager = DummyEventSubManager(
        db_snapshot=[],
        live_snapshot=[
            {
                "twitch_subscription_id": "sub-allowed",
                "status": "enabled",
                "event_type": "stream.online",
                "broadcaster_user_id": "123",
                "upstream_transport": "websocket",
                "bot_account_id": str(allowed_bot_id),
                "session_id": "sess-a",
                "connected_at": None,
                "disconnected_at": None,
            },
            {
                "twitch_subscription_id": "sub-blocked",
                "status": "enabled",
                "event_type": "stream.online",
                "broadcaster_user_id": "456",
                "upstream_transport": "websocket",
                "bot_account_id": str(blocked_bot_id),
                "session_id": "sess-b",
                "connected_at": None,
                "disconnected_at": None,
            },
        ],
    )

    app = build_app(service=service, interests=interests, manager=manager, allowed_ids={allowed_bot_id})
    client = TestClient(app)

    resp = client.get("/v1/eventsub/subscriptions/active?refresh=true")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["source"] == "twitch_live"
    assert payload["matched_for_service"] == 1
    assert [item["twitch_subscription_id"] for item in payload["items"]] == ["sub-allowed"]
    assert manager.db_calls == 0
    assert manager.live_calls == 1
