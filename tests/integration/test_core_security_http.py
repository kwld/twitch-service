from __future__ import annotations

import hashlib
import hmac
import json
import time
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient


def _twitch_signature(secret: str, message_id: str, timestamp: str, payload: bytes) -> str:
    digest = hmac.new(
        secret.encode("utf-8"),
        message_id.encode("utf-8") + timestamp.encode("utf-8") + payload,
        hashlib.sha256,
    ).hexdigest()
    return f"sha256={digest}"


def _webhook_headers(secret: str, message_id: str, payload: bytes) -> dict[str, str]:
    timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return {
        "Twitch-Eventsub-Message-Id": message_id,
        "Twitch-Eventsub-Message-Type": "notification",
        "Twitch-Eventsub-Message-Timestamp": timestamp,
        "Twitch-Eventsub-Message-Signature": _twitch_signature(secret, message_id, timestamp, payload),
        "Content-Type": "application/json",
    }


@pytest.mark.integration
def test_ip_allowlist_middleware_allows_with_forwarded_ip(app_factory) -> None:
    app = app_factory(
        {
            "APP_ALLOWED_IPS": "10.10.10.10/32",
            "APP_TRUST_X_FORWARDED_FOR": "true",
        }
    )
    with TestClient(app) as client:
        resp = client.get("/health", headers={"X-Forwarded-For": "10.10.10.10"})
    assert resp.status_code == 200


@pytest.mark.integration
def test_ip_allowlist_middleware_blocks_disallowed_ip(app_factory) -> None:
    app = app_factory(
        {
            "APP_ALLOWED_IPS": "10.10.10.10/32",
            "APP_TRUST_X_FORWARDED_FOR": "true",
        }
    )
    with TestClient(app) as client:
        resp = client.get("/health", headers={"X-Forwarded-For": "10.10.10.11"})
    assert resp.status_code == 403
    assert "Client IP not allowed" in resp.text


@pytest.mark.integration
def test_ip_allowlist_webhook_path_is_bypassed(app_factory) -> None:
    app = app_factory(
        {
            "APP_ALLOWED_IPS": "10.10.10.10/32",
            "APP_TRUST_X_FORWARDED_FOR": "true",
        }
    )
    payload = b"{}"
    with TestClient(app) as client:
        resp = client.post(
            "/webhooks/twitch/eventsub",
            data=payload,
            headers={"X-Forwarded-For": "10.10.10.11", "Content-Type": "application/json"},
        )
    # Reaches webhook signature validation instead of generic IP middleware block.
    assert resp.status_code == 403
    assert "Invalid Twitch signature" in resp.text


@pytest.mark.integration
def test_webhook_signature_valid_and_replay_deduped(app_factory) -> None:
    app = app_factory()
    import app.main as main_module

    handler = AsyncMock()
    main_module.eventsub_manager.handle_webhook_notification = handler
    secret = "test-webhook-secret-123"
    payload_obj = {
        "subscription": {"id": "sub-1", "type": "stream.online", "condition": {"broadcaster_user_id": "1"}},
        "event": {"broadcaster_user_id": "1"},
    }
    payload = json.dumps(payload_obj).encode("utf-8")
    headers = _webhook_headers(secret, "msg-1", payload)

    with TestClient(app) as client:
        first = client.post("/webhooks/twitch/eventsub", data=payload, headers=headers)
        second = client.post("/webhooks/twitch/eventsub", data=payload, headers=headers)
        time.sleep(0.05)

    assert first.status_code == 204
    assert second.status_code == 204
    assert handler.call_count == 1


@pytest.mark.integration
def test_webhook_signature_missing_headers_rejected(app_factory) -> None:
    app = app_factory()
    with TestClient(app) as client:
        resp = client.post("/webhooks/twitch/eventsub", json={})
    assert resp.status_code == 403
    assert "Invalid Twitch signature" in resp.text
