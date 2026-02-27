import asyncio
import uuid
from datetime import timedelta

import pytest

from app.core.runtime_tokens import EventSubMessageDeduper, WsTokenStore


@pytest.mark.asyncio
async def test_ws_token_issue_and_consume_once():
    store = WsTokenStore(ttl=timedelta(seconds=5))
    service_id = uuid.uuid4()

    token, ttl = await store.issue(service_id)
    assert token
    assert ttl == 5

    assert await store.consume(token) == service_id
    assert await store.consume(token) is None


@pytest.mark.asyncio
async def test_ws_token_expiration():
    store = WsTokenStore(ttl=timedelta(milliseconds=20))
    service_id = uuid.uuid4()

    token, _ = await store.issue(service_id)
    await asyncio.sleep(0.05)

    assert await store.consume(token) is None


@pytest.mark.asyncio
async def test_eventsub_message_deduper_basic_behavior():
    deduper = EventSubMessageDeduper(ttl=timedelta(seconds=10))

    assert not await deduper.is_new("")
    assert await deduper.is_new("m1")
    assert not await deduper.is_new("m1")
    assert await deduper.is_new("m2")


@pytest.mark.asyncio
async def test_eventsub_message_deduper_allows_after_ttl():
    deduper = EventSubMessageDeduper(ttl=timedelta(milliseconds=20))

    assert await deduper.is_new("m1")
    await asyncio.sleep(0.05)
    assert await deduper.is_new("m1")
