from __future__ import annotations

import json
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.models import (
    BotAccount,
    ChannelState,
    ServiceAccount,
    ServiceBotAccess,
    ServiceEventTrace,
    ServiceInterest,
    ServiceRuntimeStats,
    TwitchSubscription,
)
from app.routes.status_routes import register_status_routes
from app.status_runtime import StatusRuntime


class _ScalarResult:
    def __init__(self, rows):
        self._rows = list(rows)

    def all(self):
        return list(self._rows)


class DummySession:
    def __init__(self, rows_by_table):
        self._rows_by_table = rows_by_table

    async def scalars(self, statement):
        text = str(statement)
        for table_name, rows in self._rows_by_table.items():
            if f"FROM {table_name}" in text:
                return _ScalarResult(rows)
        return _ScalarResult([])


def make_session_factory(rows_by_table):
    @asynccontextmanager
    async def _factory():
        yield DummySession(rows_by_table)

    return _factory


class DummyEventSubManager:
    async def get_db_active_subscriptions_snapshot(self):
        return (
            [],
            datetime.now(UTC),
        )

    async def get_active_subscriptions_snapshot(self, force_refresh: bool = False):
        return (
            [
                {
                    "twitch_subscription_id": "sub-12345678",
                    "status": "enabled",
                    "cost": 2,
                    "event_type": "channel.chat.message",
                    "broadcaster_user_id": "1316870220",
                    "upstream_transport": "websocket",
                    "bot_account_id": str(uuid.UUID("11111111-1111-1111-1111-111111111111")),
                    "session_id": "AgoQabcdef1234567890",
                }
            ],
            datetime.now(UTC),
            False,
        )

    async def get_status_summary(self):
        return {
            "startup_state": "ready",
            "startup_started_at": datetime.now(UTC).isoformat(),
            "startup_finished_at": datetime.now(UTC).isoformat(),
            "session_id_masked": "AgoQab...7890",
            "session_welcome_count": 2,
            "last_session_welcome_at": datetime.now(UTC).isoformat(),
            "phase_history": [
                {
                    "label": "load_interests",
                    "elapsed_ms": 120,
                    "completed_at": datetime.now(UTC).isoformat(),
                }
            ],
            "last_error": None,
            "connect_cycle_count": 3,
            "run_loop_started_at": datetime.now(UTC).isoformat(),
            "registry_key_count": 4,
            "active_service_ws_connections": 1,
            "last_service_disconnect_at": None,
            "websocket_listener_cooldown_seconds": None,
            "has_websocket_interest": True,
            "has_stream_state_interest": True,
        }


def build_app():
    app = FastAPI()
    service_id = uuid.uuid4()
    bot_id = uuid.UUID("11111111-1111-1111-1111-111111111111")

    now = datetime.now(UTC)
    rows_by_table = {
        "service_accounts": [
            ServiceAccount(
                id=service_id,
                name="main-app",
                client_id="client-abcdef123456",
                client_secret_hash="hash",
                enabled=True,
            )
        ],
        "service_runtime_stats": [
            ServiceRuntimeStats(
                service_account_id=service_id,
                is_connected=True,
                active_ws_connections=1,
                total_ws_connects=5,
                total_api_requests=20,
                total_events_sent_ws=7,
                total_events_sent_webhook=0,
                last_connected_at=now,
                last_api_request_at=now,
                last_event_sent_at=now,
            )
        ],
        "service_interests": [
            ServiceInterest(
                id=uuid.uuid4(),
                service_account_id=service_id,
                bot_account_id=bot_id,
                event_type="channel.chat.message",
                broadcaster_user_id="1316870220",
                transport="websocket",
                webhook_url=None,
                last_heartbeat_at=now,
            )
        ],
        "bot_accounts": [
            BotAccount(
                id=bot_id,
                name="szym-bot",
                twitch_user_id="1403423270",
                twitch_login="szym_bot",
                access_token="x",
                refresh_token="y",
                token_expires_at=now,
                enabled=True,
            )
        ],
        "service_bot_access": [
            ServiceBotAccess(
                id=uuid.uuid4(),
                service_account_id=service_id,
                bot_account_id=bot_id,
            )
        ],
        "channel_states": [
            ChannelState(
                id=uuid.uuid4(),
                bot_account_id=bot_id,
                broadcaster_user_id="1316870220",
                is_live=True,
                title="Very Secret Stream Title",
                game_name="Fallout",
                last_checked_at=now,
            )
        ],
        "service_event_traces": [
            ServiceEventTrace(
                id=uuid.uuid4(),
                service_account_id=service_id,
                direction="incoming",
                local_transport="twitch_eventsub",
                event_type="channel.chat.message",
                target="/eventsub/ws",
                payload_json=json.dumps(
                    {
                        "subscription": {
                            "condition": {
                                "broadcaster_user_id": "1316870220",
                            }
                        },
                        "event": {
                            "broadcaster_user_id": "1316870220",
                            "broadcaster_user_login": "bakusiowa_vibe",
                            "message": {"text": "!ax"},
                        },
                    }
                ),
                created_at=now,
            ),
            ServiceEventTrace(
                id=uuid.uuid4(),
                service_account_id=service_id,
                direction="outgoing",
                local_transport="websocket",
                event_type="channel.chat.message",
                target="/ws/events",
                payload_json=json.dumps(
                    {
                        "event": {
                            "broadcaster_user_id": "1316870220",
                            "broadcaster_user_login": "bakusiowa_vibe",
                            "message": {"text": "Hello World"},
                        }
                    }
                ),
                created_at=now,
            ),
            ServiceEventTrace(
                id=uuid.uuid4(),
                service_account_id=service_id,
                direction="incoming",
                local_transport="twitch_eventsub",
                event_type="stream.online",
                target="/eventsub/ws",
                payload_json=json.dumps(
                    {
                        "event": {
                            "broadcaster_user_id": "1316870220",
                            "broadcaster_user_login": "bakusiowa_vibe",
                        }
                    }
                ),
                created_at=now,
            )
        ],
        "twitch_subscriptions": [
            TwitchSubscription(
                id=uuid.uuid4(),
                bot_account_id=bot_id,
                event_type="channel.chat.message",
                broadcaster_user_id="1316870220",
                twitch_subscription_id="sub-12345678",
                status="enabled",
                session_id="AgoQabcdef1234567890",
                last_seen_at=now,
            )
        ],
    }

    async def _filter_working_interests(_session, interests):
        return interests

    register_status_routes(
        app,
        settings=SimpleNamespace(app_env="test", app_allowed_ips="", app_trust_x_forwarded_for=False),
        logger=SimpleNamespace(warning=lambda *_args, **_kwargs: None),
        session_factory=make_session_factory(rows_by_table),
        eventsub_manager=DummyEventSubManager(),
        status_runtime=StatusRuntime(),
        resolve_client_ip=lambda direct, xff, trust_x_forwarded_for=False: direct,
        is_ip_allowed=lambda _ip: True,
        filter_working_interests=_filter_working_interests,
    )
    return app


def test_status_get_returns_html_dashboard():
    client = TestClient(build_app())
    resp = client.get("/status")

    assert resp.status_code == 200
    assert "Runtime Status" in resp.text
    assert "/ws/status" in resp.text
    assert "Live Event Feed" in resp.text
    assert "Pause" in resp.text


def test_status_post_returns_json_snapshot_with_masking():
    client = TestClient(build_app())
    resp = client.post("/status")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["schema_version"] == "twitch-service-status.v1"
    assert payload["eventsub"]["startup_state"] == "ready"
    assert payload["eventsub"]["active_snapshot_cost_total"] == 2
    assert payload["services"]["rows"][0]["name"] == "main-app"
    assert payload["bots"][0]["eventsub_cost_total"] == 2
    assert payload["broadcasters"][0]["title_masked"] != "Very Secret Stream Title"
    assert payload["broadcasters"][0]["broadcaster_user_id_masked"] != "1316870220"
    assert payload["broadcasters"][0]["broadcaster_label"].startswith("chan:")
    assert payload["broadcasters"][0]["broadcaster_label"] != "chan:bakusiowa_vibe"
    assert payload["broadcasters"][0]["messages_received"] == 1
    assert payload["broadcasters"][0]["messages_sent"] == 1
    assert payload["broadcasters"][0]["eventsub_count"] >= 1
    assert "channel.chat.message" in payload["broadcasters"][0]["eventsub_names"]
    assert payload["recent_events"][0]["service_name"] == "main-app"
    assert payload["recent_events"][0]["broadcaster_label"].startswith("chan:")
    assert payload["recent_events"][0]["broadcaster_label"] != "chan:bakusiowa_vibe"
    assert payload["recent_events"][0]["broadcaster_user_id_masked"] != "1316870220"
    assert "broadcaster_user_login" in payload["recent_events"][0]["body_pretty"]


def test_status_websocket_emits_snapshot():
    client = TestClient(build_app())

    with client.websocket_connect("/ws/status") as websocket:
        payload = websocket.receive_json()

    assert payload["type"] == "status_snapshot"
    assert payload["payload"]["eventsub"]["startup_state"] == "ready"
