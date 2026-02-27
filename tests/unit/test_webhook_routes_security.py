from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routes.webhook_routes import register_webhook_routes


class DummyManager:
    async def handle_webhook_notification(self, payload, message_id):
        self.last_notification = (payload, message_id)

    async def handle_webhook_revocation(self, payload):
        self.last_revocation = payload


def build_app(verify_ok=True, is_new=True):
    app = FastAPI()
    manager = DummyManager()

    async def is_new_fn(_message_id: str):
        return is_new

    register_webhook_routes(
        app,
        eventsub_manager=manager,
        verify_twitch_signature=lambda _req, _raw: verify_ok,
        is_new_eventsub_message_id=is_new_fn,
    )
    return app


def _headers(message_type: str):
    return {
        "Twitch-Eventsub-Message-Id": "m-1",
        "Twitch-Eventsub-Message-Type": message_type,
        "Content-Type": "application/json",
    }


def test_webhook_rejects_invalid_signature():
    app = build_app(verify_ok=False, is_new=True)
    client = TestClient(app)
    resp = client.post("/webhooks/twitch/eventsub", headers=_headers("notification"), json={"event": {}})
    assert resp.status_code == 403
    assert resp.json()["detail"] == "Invalid Twitch signature"


def test_webhook_callback_verification_returns_challenge():
    app = build_app(verify_ok=True, is_new=True)
    client = TestClient(app)
    resp = client.post(
        "/webhooks/twitch/eventsub",
        headers=_headers("webhook_callback_verification"),
        json={"challenge": "abc123"},
    )
    assert resp.status_code == 200
    assert resp.text == "abc123"


def test_webhook_duplicate_callback_verification_still_returns_challenge():
    app = build_app(verify_ok=True, is_new=False)
    client = TestClient(app)
    resp = client.post(
        "/webhooks/twitch/eventsub",
        headers=_headers("webhook_callback_verification"),
        json={"challenge": "abc123"},
    )
    assert resp.status_code == 200
    assert resp.text == "abc123"


def test_webhook_notification_returns_204():
    app = build_app(verify_ok=True, is_new=True)
    client = TestClient(app)
    resp = client.post(
        "/webhooks/twitch/eventsub",
        headers=_headers("notification"),
        json={"subscription": {}, "event": {}},
    )
    assert resp.status_code == 204


def test_webhook_revocation_returns_204():
    app = build_app(verify_ok=True, is_new=True)
    client = TestClient(app)
    resp = client.post(
        "/webhooks/twitch/eventsub",
        headers=_headers("revocation"),
        json={"subscription": {}},
    )
    assert resp.status_code == 204


def test_webhook_duplicate_notification_returns_204_without_processing():
    app = build_app(verify_ok=True, is_new=False)
    client = TestClient(app)
    resp = client.post(
        "/webhooks/twitch/eventsub",
        headers=_headers("notification"),
        json={"subscription": {}, "event": {}},
    )
    assert resp.status_code == 204


def test_webhook_unknown_type_returns_204():
    app = build_app(verify_ok=True, is_new=True)
    client = TestClient(app)
    resp = client.post(
        "/webhooks/twitch/eventsub",
        headers=_headers("unknown"),
        json={},
    )
    assert resp.status_code == 204
