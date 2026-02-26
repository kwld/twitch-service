from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta

import pytest

from app.core.runtime_tokens import WsTokenStore


@pytest.mark.unit
@pytest.mark.asyncio
async def test_ws_token_issue_and_consume_once() -> None:
    store = WsTokenStore(ttl=timedelta(minutes=2))
    service_id = uuid.uuid4()
    token, expires_in = await store.issue(service_id)
    assert token
    assert expires_in == 120

    first_consume = await store.consume(token)
    second_consume = await store.consume(token)
    assert first_consume == service_id
    assert second_consume is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_ws_token_expiry_rejected() -> None:
    store = WsTokenStore(ttl=timedelta(milliseconds=20))
    service_id = uuid.uuid4()
    token, _ = await store.issue(service_id)
    await asyncio.sleep(0.05)
    assert await store.consume(token) is None
