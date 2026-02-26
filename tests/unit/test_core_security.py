from __future__ import annotations

import asyncio
import socket
from datetime import timedelta

import pytest
from fastapi import HTTPException

from app.core.network_security import WebhookTargetValidator
from app.core.runtime_tokens import EventSubMessageDeduper


@pytest.mark.unit
@pytest.mark.asyncio
async def test_eventsub_message_deduper_accepts_first_rejects_replay() -> None:
    deduper = EventSubMessageDeduper(ttl=timedelta(minutes=10))
    message_id = "msg-123"
    assert await deduper.is_new(message_id) is True
    assert await deduper.is_new(message_id) is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_webhook_target_validator_rejects_non_http_scheme() -> None:
    validator = WebhookTargetValidator(allowlist=[], block_private_targets=True)
    with pytest.raises(HTTPException) as exc:
        await validator.validate("ftp://example.com/hook")
    assert exc.value.status_code == 422


@pytest.mark.unit
@pytest.mark.asyncio
async def test_webhook_target_validator_rejects_userinfo() -> None:
    validator = WebhookTargetValidator(allowlist=[], block_private_targets=True)
    with pytest.raises(HTTPException) as exc:
        await validator.validate("https://user:pass@example.com/hook")
    assert exc.value.status_code == 422


@pytest.mark.unit
@pytest.mark.asyncio
async def test_webhook_target_validator_rejects_private_ip_target() -> None:
    validator = WebhookTargetValidator(allowlist=[], block_private_targets=True)
    with pytest.raises(HTTPException) as exc:
        await validator.validate("http://127.0.0.1/hook")
    assert exc.value.status_code == 422


@pytest.mark.unit
@pytest.mark.asyncio
async def test_webhook_target_validator_enforces_allowlist() -> None:
    validator = WebhookTargetValidator(allowlist=["allowed.example"], block_private_targets=False)
    with pytest.raises(HTTPException) as exc:
        await validator.validate("https://denied.example/hook")
    assert exc.value.status_code == 422


@pytest.mark.unit
@pytest.mark.asyncio
async def test_webhook_target_validator_rejects_hostname_resolving_private_ip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    validator = WebhookTargetValidator(allowlist=[], block_private_targets=True)

    async def fake_getaddrinfo(*args, **kwargs):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443)),
        ]

    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "getaddrinfo", fake_getaddrinfo)

    with pytest.raises(HTTPException) as exc:
        await validator.validate("https://public-name.example/hook")
    assert exc.value.status_code == 422
