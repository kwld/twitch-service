from __future__ import annotations

import argparse
import asyncio
import secrets
from urllib.parse import parse_qs, urlparse

from prompt_toolkit import PromptSession
from sqlalchemy import select

from app.auth import generate_client_id, generate_client_secret, hash_secret
from app.config import load_settings
from app.db import create_engine_and_session
from app.models import Base, BotAccount, ServiceAccount
from app.twitch import TwitchApiError, TwitchClient


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Twitch EventSub Service CLI")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("console", help="Start interactive async console")
    sub.add_parser("run-api", help="Run API server")
    return parser.parse_args()


async def init_db() -> tuple:
    settings = load_settings()
    engine, session_factory = create_engine_and_session(settings)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return settings, engine, session_factory


def extract_code(redirect_url: str) -> str:
    parsed = urlparse(redirect_url)
    code = parse_qs(parsed.query).get("code", [None])[0]
    if not code:
        raise ValueError("No OAuth code found in the provided URL")
    return code


async def menu_loop() -> None:
    settings, engine, session_factory = await init_db()
    twitch = TwitchClient(
        client_id=settings.twitch_client_id,
        client_secret=settings.twitch_client_secret,
        redirect_uri=settings.twitch_redirect_uri,
        scopes=settings.twitch_scopes,
        eventsub_ws_url=settings.twitch_eventsub_ws_url,
    )
    session = PromptSession()

    while True:
        print(
            "\nEventSub Console\n"
            "1) List bots\n"
            "2) Add bot (OAuth)\n"
            "3) Refresh bot token\n"
            "4) Create service account\n"
            "5) Regenerate service secret\n"
            "6) List service accounts\n"
            "7) Exit\n"
        )
        choice = (await session.prompt_async("Select option: ")).strip()

        if choice == "1":
            async with session_factory() as db:
                bots = list((await db.scalars(select(BotAccount))).all())
            if not bots:
                print("No bots configured.")
            for bot in bots:
                print(
                    f"- {bot.name} ({bot.twitch_login}/{bot.twitch_user_id}) "
                    f"expires={bot.token_expires_at.isoformat()} enabled={bot.enabled}"
                )

        elif choice == "2":
            name = (await session.prompt_async("Bot name: ")).strip()
            state = secrets.token_urlsafe(16)
            auth_url = twitch.build_authorize_url(state=state)
            print("\nOpen this URL in browser, authorize, then paste full redirect URL:\n")
            print(auth_url)
            redirect = (await session.prompt_async("\nRedirect URL: ")).strip()
            try:
                code = extract_code(redirect)
                token = await twitch.exchange_code(code)
                users = await twitch.get_users(token.access_token)
                if not users:
                    raise TwitchApiError("Twitch returned no users")
                user = users[0]
            except Exception as exc:
                print(f"Failed OAuth flow: {exc}")
                continue
            async with session_factory() as db:
                existing = await db.scalar(select(BotAccount).where(BotAccount.name == name))
                if existing:
                    print("Bot name already exists.")
                    continue
                bot = BotAccount(
                    name=name,
                    twitch_user_id=user["id"],
                    twitch_login=user["login"],
                    access_token=token.access_token,
                    refresh_token=token.refresh_token,
                    token_expires_at=token.expires_at,
                    enabled=True,
                )
                db.add(bot)
                await db.commit()
            print(f"Added bot: {name} ({user['login']})")

        elif choice == "3":
            name = (await session.prompt_async("Bot name to refresh: ")).strip()
            async with session_factory() as db:
                bot = await db.scalar(select(BotAccount).where(BotAccount.name == name))
                if not bot:
                    print("Bot not found.")
                    continue
                try:
                    refreshed = await twitch.refresh_token(bot.refresh_token)
                except Exception as exc:
                    print(f"Refresh failed: {exc}")
                    continue
                bot.access_token = refreshed.access_token
                bot.refresh_token = refreshed.refresh_token
                bot.token_expires_at = refreshed.expires_at
                await db.commit()
            print("Token refreshed.")

        elif choice == "4":
            name = (await session.prompt_async("Service name: ")).strip()
            client_id = generate_client_id()
            client_secret = generate_client_secret()
            async with session_factory() as db:
                existing = await db.scalar(select(ServiceAccount).where(ServiceAccount.name == name))
                if existing:
                    print("Service account name already exists.")
                    continue
                db.add(
                    ServiceAccount(
                        name=name,
                        client_id=client_id,
                        client_secret_hash=hash_secret(client_secret),
                    )
                )
                await db.commit()
            print("\nService account created:")
            print(f"client_id: {client_id}")
            print(f"client_secret: {client_secret}\n")

        elif choice == "5":
            client_id = (await session.prompt_async("Client ID: ")).strip()
            new_secret = generate_client_secret()
            async with session_factory() as db:
                account = await db.scalar(
                    select(ServiceAccount).where(ServiceAccount.client_id == client_id)
                )
                if not account:
                    print("Service account not found.")
                    continue
                account.client_secret_hash = hash_secret(new_secret)
                await db.commit()
            print(f"\nNew client_secret: {new_secret}\n")

        elif choice == "6":
            async with session_factory() as db:
                accounts = list((await db.scalars(select(ServiceAccount))).all())
            if not accounts:
                print("No service accounts.")
            for account in accounts:
                print(f"- {account.name}: client_id={account.client_id} enabled={account.enabled}")

        elif choice == "7":
            break

        else:
            print("Invalid option.")

    await engine.dispose()


def main() -> None:
    args = parse_args()
    if args.command == "run-api":
        from app.main import run as run_api

        run_api()
        return
    if args.command == "console":
        asyncio.run(menu_loop())
        return


if __name__ == "__main__":
    main()
