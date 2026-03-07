from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.event_router import InterestRegistry
from app.models import ChannelState, ServiceAccount, ServiceInterest
from app.routes.service_routes import register_service_routes


class _ScalarResult:
    def __init__(self, rows):
        self._rows = list(rows)

    def all(self):
        return list(self._rows)


class DummySession:
    def __init__(self, interests, channel_states):
        self._interests = list(interests)
        self._channel_states = list(channel_states)

    async def scalars(self, statement):
        text = str(statement)
        if "FROM service_interests" in text:
            return _ScalarResult(self._interests)
        if "FROM channel_states" in text:
            return _ScalarResult(self._channel_states)
        return _ScalarResult([])


def make_session_factory(interests, channel_states):
    @asynccontextmanager
    async def _factory():
        yield DummySession(interests, channel_states)

    return _factory


def build_app(*, service, interests, channel_states, working_ids=None):
    app = FastAPI()
    working_ids = set(working_ids or set())

    async def _service_auth():
        return service

    async def _service_allowed_bot_ids(_session, _service_id):
        return set()

    async def _filter_working_interests(_session, rows):
        if not working_ids:
            return rows
        return [row for row in rows if row.id in working_ids]

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
        session_factory=make_session_factory(interests, channel_states),
        twitch_client=None,
        eventsub_manager=None,
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


def _make_interest(service_id: uuid.UUID, bot_id: uuid.UUID, event_type: str, broadcaster_user_id: str, *, heartbeat_minutes_ago: int = 0) -> ServiceInterest:
    now = datetime.now(UTC) - timedelta(minutes=heartbeat_minutes_ago)
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


def _make_channel_state(bot_id: uuid.UUID, broadcaster_user_id: str, *, is_live: bool) -> ChannelState:
    return ChannelState(
        id=uuid.uuid4(),
        bot_account_id=bot_id,
        broadcaster_user_id=broadcaster_user_id,
        is_live=is_live,
        title="title",
        game_name="game",
        started_at=None,
        last_event_at=None,
        last_checked_at=datetime.now(UTC),
    )


def test_retained_interest_status_returns_working_counts_and_channel_state():
    service = _make_service()
    bot_id = uuid.uuid4()
    working_stream = _make_interest(service.id, bot_id, "stream.online", "123", heartbeat_minutes_ago=5)
    stale_chat = _make_interest(service.id, bot_id, "channel.chat.message", "123", heartbeat_minutes_ago=20)
    other_channel = _make_interest(service.id, bot_id, "stream.offline", "456", heartbeat_minutes_ago=2)

    app = build_app(
        service=service,
        interests=[working_stream, stale_chat, other_channel],
        channel_states=[_make_channel_state(bot_id, "123", is_live=True)],
        working_ids={working_stream.id, other_channel.id},
    )
    client = TestClient(app)

    resp = client.get(f"/v1/interests/retained-status?bot_account_id={bot_id}&broadcaster_user_ids=123,456")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["bot_account_id"] == str(bot_id)
    assert payload["requested_broadcaster_user_ids"] == ["123", "456"]
    assert payload["matched_broadcaster_count"] == 2

    by_broadcaster = {item["broadcaster_user_id"]: item for item in payload["items"]}
    assert by_broadcaster["123"]["total_interest_count"] == 2
    assert by_broadcaster["123"]["working_interest_count"] == 1
    assert by_broadcaster["123"]["retained_event_types"] == ["stream.online"]
    assert by_broadcaster["123"]["has_channel_state"] is True
    assert by_broadcaster["123"]["channel_is_live"] is True
    assert by_broadcaster["456"]["total_interest_count"] == 1
    assert by_broadcaster["456"]["working_interest_count"] == 1
    assert by_broadcaster["456"]["retained_event_types"] == ["stream.offline"]
    assert by_broadcaster["456"]["has_channel_state"] is False
    assert by_broadcaster["456"]["channel_is_live"] is None


def test_retained_interest_status_requires_requested_broadcasters():
    service = _make_service()
    bot_id = uuid.uuid4()
    app = build_app(service=service, interests=[], channel_states=[])
    client = TestClient(app)

    resp = client.get(f"/v1/interests/retained-status?bot_account_id={bot_id}&broadcaster_user_ids=,,")

    assert resp.status_code == 422
    assert resp.json()["detail"] == "broadcaster_user_ids is required"
