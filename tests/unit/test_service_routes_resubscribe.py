from __future__ import annotations

import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.event_router import InterestRegistry
from app.models import ServiceAccount
from app.routes.service_routes import register_service_routes


class _ScalarResult:
    def __init__(self, rows):
        self._rows = list(rows)

    def all(self):
        return list(self._rows)


class DummySession:
    async def scalars(self, _statement):
        return _ScalarResult([])


def make_session_factory():
    @asynccontextmanager
    async def _factory():
        yield DummySession()

    return _factory


class DummyEventSubManager:
    def __init__(self):
        self.calls = []

    async def resubscribe_broadcaster(
        self,
        *,
        broadcaster_user_id,
        service_account_id=None,
        allowed_bot_ids=None,
        bot_account_id=None,
        force=False,
    ):
        self.calls.append(
            {
                "broadcaster_user_id": broadcaster_user_id,
                "service_account_id": service_account_id,
                "allowed_bot_ids": set(allowed_bot_ids or set()),
                "bot_account_id": bot_account_id,
                "force": force,
            }
        )
        return {
            "broadcaster_user_id": broadcaster_user_id,
            "bot_account_id": str(bot_account_id) if bot_account_id else None,
            "force": force,
            "matched_interest_count": 2,
            "ensured_interest_count": 2,
            "removed_subscription_count": 1,
            "event_types": ["channel.chat.message", "stream.online"],
        }

    async def get_db_active_subscriptions_snapshot(self):
        return [], None

    async def get_active_subscriptions_snapshot(self, force_refresh=False):
        return [], None, False


def build_app(*, service, manager, allowed_ids=None):
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

    async def _ensure_service_can_access_bot(_session, _service_id, bot_account_id):
        if allowed_ids and bot_account_id not in set(allowed_ids):
            raise AssertionError("unexpected bot access")
        return None

    async def _ensure_default_stream_interests(**_kwargs):
        return []

    async def _validate_webhook_target_url(_url):
        return None

    register_service_routes(
        app,
        session_factory=make_session_factory(),
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


def test_resubscribe_broadcaster_route_passes_force_and_bot_scope():
    service = _make_service()
    manager = DummyEventSubManager()
    allowed_bot_id = uuid.uuid4()
    app = build_app(service=service, manager=manager, allowed_ids={allowed_bot_id})
    client = TestClient(app)

    resp = client.post(
        "/v1/eventsub/subscriptions/resubscribe",
        json={
            "broadcaster_user_id": "123",
            "bot_account_id": str(allowed_bot_id),
            "force": True,
        },
    )

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["broadcaster_user_id"] == "123"
    assert payload["force"] is True
    assert payload["matched_interest_count"] == 2
    assert payload["removed_subscription_count"] == 1
    assert manager.calls == [
        {
            "broadcaster_user_id": "123",
            "service_account_id": service.id,
            "allowed_bot_ids": {allowed_bot_id},
            "bot_account_id": allowed_bot_id,
            "force": True,
        }
    ]
