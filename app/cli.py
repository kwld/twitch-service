from __future__ import annotations

import argparse
import asyncio
import os
import secrets
from pathlib import Path

from prompt_toolkit import PromptSession
from sqlalchemy import select

from app.cli_components.bot_workflows import (
    ask_yes_no,
    guided_bot_setup,
    obtain_oauth_code,
    obtain_oauth_code_for_scopes,
    select_bot_account,
)
from app.cli_components.interactive_tools import (
    chat_connect_menu,
    chat_connect_other_channel_menu,
    create_clip_menu,
    manage_eventsub_subscriptions_menu,
    remove_bot_menu,
)
from app.cli_components.monitoring import (
    format_duration_short,
    list_broadcaster_authorizations_menu,
    list_service_status_menu,
    list_tracked_channels_menu,
    live_service_event_tracking_menu,
    websocket_listener_cooldown_remaining_cli,
)
from app.cli_components.remote_console import env_bool, normalize_base_url, remote_menu_loop
from app.cli_components.service_management import (
    authorize_bot_self_channel_menu,
    manage_service_accounts_menu,
    select_service_account as _select_service_account,
)
from app.config import load_settings
from app.db import create_engine_and_session
from app.models import (
    Base,
    BotAccount,
)
from app.twitch import TwitchApiError, TwitchClient


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Twitch EventSub Service CLI")
    parser.add_argument(
        "--cli-env-file",
        default=".cli.env",
        help="Path to optional CLI env file for remote mode settings (default: .cli.env)",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    console = sub.add_parser("console", help="Start interactive async console")
    console.add_argument(
        "--remote",
        action="store_true",
        help="Use remote API mode instead of local database mode",
    )
    console.add_argument(
        "--api-base-url",
        default=None,
        help="Remote API base URL (for example https://api.example.com)",
    )
    sub.add_parser("run-api", help="Run API server")
    return parser.parse_args()


def _load_cli_env(path: str) -> None:
    env_path = Path(path)
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            raw = line.strip()
            if not raw or raw.startswith("#") or "=" not in raw:
                continue
            key, value = raw.split("=", 1)
            key = key.strip()
            value = value.strip()
            if key and key not in os.environ:
                os.environ[key] = value


async def init_db() -> tuple:
    settings = load_settings()
    engine, session_factory = create_engine_and_session(settings)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return settings, engine, session_factory



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
            "2) Guided bot setup (OAuth wizard)\n"
            "3) Add bot (quick OAuth)\n"
            "4) Refresh bot token\n"
            "5) Manage service accounts\n"
            "6) Live chat (own channel)\n"
            "7) Live chat (other channel)\n"
            "8) Remove bot account\n"
            "9) Manage active EventSub subscriptions\n"
            "10) View service status and usage\n"
            "11) View broadcaster authorizations\n"
            "12) Create clip\n"
            "13) View tracked channels (online/offline)\n"
            "14) Live service communication tracking\n"
            "15) Authorize bot in own channel (service)\n"
            "16) Exit\n"
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
            await guided_bot_setup(session, session_factory, twitch)

        elif choice == "3":
            name = (await session.prompt_async("Bot name: ")).strip()
            state = secrets.token_urlsafe(16)
            code = await obtain_oauth_code(session, session_factory, twitch, state=state)
            if not code:
                continue
            try:
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

        elif choice == "4":
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

        elif choice == "5":
            await manage_service_accounts_menu(session, session_factory)

        elif choice == "6":
            await chat_connect_menu(session, session_factory, twitch)

        elif choice == "7":
            await chat_connect_other_channel_menu(session, session_factory, twitch)

        elif choice == "8":
            await remove_bot_menu(session, session_factory, twitch)

        elif choice == "9":
            await manage_eventsub_subscriptions_menu(
                session,
                session_factory,
                twitch,
                websocket_listener_cooldown_remaining_cli_fn=websocket_listener_cooldown_remaining_cli,
                format_duration_short_fn=format_duration_short,
            )

        elif choice == "10":
            await list_service_status_menu(session_factory)

        elif choice == "11":
            await list_broadcaster_authorizations_menu(session_factory)

        elif choice == "12":
            await create_clip_menu(session, session_factory, twitch)

        elif choice == "13":
            await list_tracked_channels_menu(session, session_factory, ask_yes_no)

        elif choice == "14":
            await live_service_event_tracking_menu(session, session_factory, _select_service_account)

        elif choice == "15":
            await authorize_bot_self_channel_menu(
                session,
                session_factory,
                twitch,
                select_bot_account_fn=select_bot_account,
                ask_yes_no_fn=ask_yes_no,
                obtain_oauth_code_for_scopes_fn=obtain_oauth_code_for_scopes,
            )

        elif choice == "16":
            break

        else:
            print("Invalid option.")

    await engine.dispose()


def main() -> None:
    args = parse_args()
    _load_cli_env(args.cli_env_file)
    if args.command == "run-api":
        from app.main import run as run_api

        run_api()
        return
    if args.command == "console":
        env_api_base_url = os.getenv("CLI_API_BASE_URL", "")
        api_base_url_raw = args.api_base_url or env_api_base_url
        use_remote = bool(args.remote or api_base_url_raw.strip())
        if use_remote:
            try:
                api_base_url = normalize_base_url(api_base_url_raw)
            except ValueError as exc:
                raise SystemExit(str(exc)) from exc
            service_client_id = os.getenv("CLI_SERVICE_CLIENT_ID", "").strip()
            service_client_secret = os.getenv("CLI_SERVICE_CLIENT_SECRET", "").strip()
            admin_api_key = os.getenv("CLI_ADMIN_API_KEY", "").strip()
            verify_tls = env_bool("CLI_VERIFY_TLS", True)
            asyncio.run(
                remote_menu_loop(
                    api_base_url=api_base_url,
                    service_client_id=service_client_id,
                    service_client_secret=service_client_secret,
                    admin_api_key=admin_api_key,
                    verify_tls=verify_tls,
                )
            )
            return
        asyncio.run(menu_loop())
        return


if __name__ == "__main__":
    main()
