from __future__ import annotations

import importlib
import os
import uuid
from unittest.mock import AsyncMock
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.models import Base
from app.twitch import TwitchClient


def _replace_database_name(url: str, db_name: str) -> str:
    split = urlsplit(url)
    if not split.path or split.path == "/":
        raise ValueError("Database URL must include database name in path")
    return urlunsplit((split.scheme, split.netloc, f"/{db_name}", split.query, split.fragment))


@pytest.fixture(scope="session")
def admin_auth_headers() -> dict[str, str]:
    return {"X-Admin-Key": os.getenv("TEST_ADMIN_API_KEY", "test-admin-key")}


@pytest.fixture(scope="session")
def service_auth_headers() -> dict[str, str]:
    return {
        "X-Client-Id": os.getenv("TEST_SERVICE_CLIENT_ID", "test-client-id"),
        "X-Client-Secret": os.getenv("TEST_SERVICE_CLIENT_SECRET", "test-client-secret"),
    }


@pytest.fixture()
def mocked_twitch_client() -> AsyncMock:
    return AsyncMock(spec=TwitchClient)


@pytest_asyncio.fixture(scope="session")
async def temporary_test_database_url() -> str:
    admin_url = os.getenv("TEST_DATABASE_ADMIN_URL")
    template_url = os.getenv("TEST_DATABASE_TEMPLATE_URL") or os.getenv("DATABASE_URL")
    if not admin_url or not template_url:
        pytest.skip(
            "Set TEST_DATABASE_ADMIN_URL and TEST_DATABASE_TEMPLATE_URL (or DATABASE_URL) "
            "to enable DB-backed tests."
        )

    db_name = f"test_{uuid.uuid4().hex[:20]}"
    conn = await asyncpg.connect(admin_url)
    try:
        await conn.execute(f'CREATE DATABASE "{db_name}"')
    finally:
        await conn.close()

    test_url = _replace_database_name(template_url, db_name)
    try:
        yield test_url
    finally:
        conn = await asyncpg.connect(admin_url)
        try:
            await conn.execute(
                "SELECT pg_terminate_backend(pid) "
                "FROM pg_stat_activity WHERE datname = $1 AND pid <> pg_backend_pid()",
                db_name,
            )
            await conn.execute(f'DROP DATABASE IF EXISTS "{db_name}"')
        finally:
            await conn.close()


@pytest_asyncio.fixture(scope="session")
async def db_engine(temporary_test_database_url: str) -> AsyncEngine:
    engine = create_async_engine(
        temporary_test_database_url,
        future=True,
        pool_pre_ping=True,
        poolclass=NullPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield engine
    finally:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        await engine.dispose()


@pytest.fixture(scope="session")
def db_session_factory(db_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(db_engine, expire_on_commit=False, class_=AsyncSession)


@pytest_asyncio.fixture()
async def db_session(db_session_factory: async_sessionmaker[AsyncSession]) -> AsyncSession:
    async with db_session_factory() as session:
        yield session
        await session.rollback()


@pytest.fixture()
def app_factory(
    temporary_test_database_url: str,
    db_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
):
    _ = db_engine
    def _build(extra_env: dict[str, str] | None = None) -> FastAPI:
        monkeypatch.setenv("DATABASE_URL", temporary_test_database_url)
        monkeypatch.setenv("APP_ENV", "test")
        monkeypatch.setenv("APP_HOST", "127.0.0.1")
        monkeypatch.setenv("APP_PORT", "18081")
        monkeypatch.setenv("ADMIN_API_KEY", "test-admin-key")
        monkeypatch.setenv("SERVICE_SIGNING_SECRET", "test-service-signing-secret")
        monkeypatch.setenv("TWITCH_CLIENT_ID", "test-client-id")
        monkeypatch.setenv("TWITCH_CLIENT_SECRET", "test-client-secret")
        monkeypatch.setenv("TWITCH_REDIRECT_URI", "http://localhost:18081/oauth/callback")
        monkeypatch.setenv("TWITCH_EVENTSUB_WEBHOOK_SECRET", "test-webhook-secret-123")
        monkeypatch.setenv(
            "TWITCH_EVENTSUB_WEBHOOK_CALLBACK_URL", "https://example.invalid/webhooks/twitch/eventsub"
        )
        monkeypatch.setenv("TWITCH_EVENTSUB_WEBHOOK_EVENT_TYPES", "stream.online,stream.offline")
        monkeypatch.setenv("APP_ALLOWED_IPS", "")
        monkeypatch.setenv("APP_TRUST_X_FORWARDED_FOR", "false")
        if extra_env:
            for key, value in extra_env.items():
                monkeypatch.setenv(key, value)

        main_module = importlib.import_module("app.main")
        main_module = importlib.reload(main_module)
        main_module.eventsub_manager.start = AsyncMock(return_value=None)
        main_module.eventsub_manager.stop = AsyncMock(return_value=None)
        main_module.event_hub.close = AsyncMock(return_value=None)
        main_module.twitch_client.close = AsyncMock(return_value=None)
        return main_module.app

    return _build


@pytest.fixture()
def app(app_factory) -> FastAPI:
    return app_factory()
