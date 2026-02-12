from __future__ import annotations

import argparse
import asyncio
import json
import secrets
from urllib.parse import parse_qs, urlparse

from prompt_toolkit import PromptSession
from sqlalchemy import select
import websockets

from app.auth import generate_client_id, generate_client_secret, hash_secret
from app.config import load_settings
from app.db import create_engine_and_session
from app.models import Base, BotAccount, ServiceAccount, TwitchSubscription
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


def parse_oauth_callback(redirect_url: str) -> tuple[str | None, str | None, str | None]:
    parsed = urlparse(redirect_url)
    query = parse_qs(parsed.query)
    code = query.get("code", [None])[0]
    state = query.get("state", [None])[0]
    error = query.get("error", [None])[0]
    return code, state, error


async def ask_yes_no(session: PromptSession, prompt: str, default_yes: bool = True) -> bool:
    suffix = " [Y/n]: " if default_yes else " [y/N]: "
    raw = (await session.prompt_async(prompt + suffix)).strip().lower()
    if not raw:
        return default_yes
    return raw in {"y", "yes"}


async def guided_bot_setup(session: PromptSession, session_factory, twitch: TwitchClient) -> None:
    print("\nGuided Twitch Bot Setup")
    print("This will guide you through OAuth and save/update a bot account.\n")
    print(f"Configured redirect URI: {twitch.redirect_uri}")
    print(f"Requested scopes: {twitch.scopes}\n")

    if not await ask_yes_no(session, "Continue with guided setup?", default_yes=True):
        return

    state = secrets.token_urlsafe(24)
    auth_url = twitch.build_authorize_url(state=state)
    print("\nStep 1: Open this URL and authorize the Twitch account to use as bot:\n")
    print(auth_url)
    print("\nStep 2: Paste full redirect URL after authorization.")
    callback = (await session.prompt_async("Redirect URL: ")).strip()

    code, returned_state, error = parse_oauth_callback(callback)
    if error:
        print(f"OAuth failed with error: {error}")
        return
    if not code:
        print("No OAuth code found in redirect URL.")
        return
    if returned_state != state:
        print("State mismatch. Stop and retry guided setup (possible CSRF or wrong callback URL).")
        return

    try:
        token = await twitch.exchange_code(code)
        token_info = await twitch.validate_user_token(token.access_token)
        users = await twitch.get_users(token.access_token)
        if not users:
            raise TwitchApiError("Twitch returned no users for token")
        user = users[0]
    except Exception as exc:
        print(f"OAuth/token validation failed: {exc}")
        return

    user_id = user["id"]
    user_login = user["login"]
    user_name = user.get("display_name", user_login)
    granted_scopes = set(token_info.get("scopes", []))
    requested_scopes = {s for s in twitch.scopes.split() if s}
    missing = sorted(requested_scopes - granted_scopes)

    print("\nAuthorized account details:")
    print(f"- user_id: {user_id}")
    print(f"- login: {user_login}")
    print(f"- display_name: {user_name}")
    print(f"- token expires in: {token_info.get('expires_in', 'unknown')} seconds")
    if missing:
        print(f"- missing requested scopes: {', '.join(missing)}")
        if not await ask_yes_no(
            session,
            "Continue anyway? (recommended: re-authorize and grant all requested scopes)",
            default_yes=False,
        ):
            return

    suggested_name = user_login
    bot_name = (await session.prompt_async(f"Local bot name [{suggested_name}]: ")).strip() or suggested_name

    async with session_factory() as db:
        existing_by_name = await db.scalar(select(BotAccount).where(BotAccount.name == bot_name))
        existing_by_user = await db.scalar(select(BotAccount).where(BotAccount.twitch_user_id == user_id))

        if existing_by_name and existing_by_name.twitch_user_id != user_id:
            print(
                f"Name conflict: '{bot_name}' already belongs to another twitch user "
                f"({existing_by_name.twitch_login}/{existing_by_name.twitch_user_id})."
            )
            return

        target = existing_by_user or existing_by_name
        if target:
            print(f"Existing bot record found: {target.name} ({target.twitch_login}/{target.twitch_user_id})")
            if not await ask_yes_no(session, "Update this bot with new OAuth tokens?", default_yes=True):
                return
            target.name = bot_name
            target.twitch_login = user_login
            target.access_token = token.access_token
            target.refresh_token = token.refresh_token
            target.token_expires_at = token.expires_at
            target.enabled = True
            await db.commit()
            print(f"Bot updated: {target.name} ({target.twitch_login})")
        else:
            bot = BotAccount(
                name=bot_name,
                twitch_user_id=user_id,
                twitch_login=user_login,
                access_token=token.access_token,
                refresh_token=token.refresh_token,
                token_expires_at=token.expires_at,
                enabled=True,
            )
            db.add(bot)
            await db.commit()
            print(f"Bot created: {bot.name} ({bot.twitch_login})")

    if await ask_yes_no(session, "Run a refresh-token test now?", default_yes=False):
        try:
            refreshed = await twitch.refresh_token(token.refresh_token)
            print("Refresh-token test successful.")
            async with session_factory() as db:
                saved = await db.scalar(select(BotAccount).where(BotAccount.twitch_user_id == user_id))
                if saved:
                    saved.access_token = refreshed.access_token
                    saved.refresh_token = refreshed.refresh_token
                    saved.token_expires_at = refreshed.expires_at
                    await db.commit()
        except Exception as exc:
            print(f"Refresh-token test failed: {exc}")


async def select_bot_account(session: PromptSession, session_factory):
    async with session_factory() as db:
        bots = list((await db.scalars(select(BotAccount).where(BotAccount.enabled.is_(True)))).all())
    if not bots:
        print("No enabled bots configured.")
        return None
    print("\nEnabled bots:")
    for idx, bot in enumerate(bots, start=1):
        print(f"{idx}) {bot.name} ({bot.twitch_login}/{bot.twitch_user_id})")
    raw = (await session.prompt_async("Select bot number: ")).strip()
    try:
        selected = int(raw)
    except ValueError:
        print("Invalid selection.")
        return None
    if selected < 1 or selected > len(bots):
        print("Invalid selection.")
        return None
    return bots[selected - 1]


async def connect_eventsub_websocket(twitch: TwitchClient):
    ws = await websockets.connect(twitch.eventsub_ws_url, max_size=4 * 1024 * 1024)
    raw = await asyncio.wait_for(ws.recv(), timeout=15)
    welcome = json.loads(raw)
    msg_type = welcome.get("metadata", {}).get("message_type")
    if msg_type != "session_welcome":
        await ws.close()
        raise RuntimeError(f"Unexpected first EventSub message: {msg_type}")
    session_id = welcome.get("payload", {}).get("session", {}).get("id")
    if not session_id:
        await ws.close()
        raise RuntimeError("EventSub welcome did not include session id")
    return ws, session_id


async def list_chat_subscriptions(twitch: TwitchClient, bot_user_id: str) -> list[dict]:
    subs = await twitch.list_eventsub_subscriptions()
    out = []
    for sub in subs:
        event_type = sub.get("type", "")
        cond = sub.get("condition", {})
        if not event_type.startswith("channel.chat."):
            continue
        if cond.get("user_id") != bot_user_id:
            continue
        out.append(sub)
    return out


async def upsert_chat_subscription_record(session_factory, bot: BotAccount, sub: dict, broadcaster_user_id: str):
    async with session_factory() as db:
        existing = await db.scalar(
            select(TwitchSubscription).where(
                TwitchSubscription.bot_account_id == bot.id,
                TwitchSubscription.event_type == sub.get("type", "channel.chat.message"),
                TwitchSubscription.broadcaster_user_id == broadcaster_user_id,
            )
        )
        if existing:
            existing.twitch_subscription_id = sub["id"]
            existing.status = sub.get("status", "enabled")
            existing.session_id = sub.get("transport", {}).get("session_id")
        else:
            db.add(
                TwitchSubscription(
                    bot_account_id=bot.id,
                    event_type=sub.get("type", "channel.chat.message"),
                    broadcaster_user_id=broadcaster_user_id,
                    twitch_subscription_id=sub["id"],
                    status=sub.get("status", "enabled"),
                    session_id=sub.get("transport", {}).get("session_id"),
                )
            )
        await db.commit()


async def resolve_broadcaster_user_id(twitch: TwitchClient, raw_target: str) -> tuple[str | None, str | None]:
    target = raw_target.strip()
    if not target:
        return None, None
    if target.isdigit():
        user = await twitch.get_user_by_id_app(target)
        if not user:
            return None, None
        return user["id"], user["login"]
    user = await twitch.get_user_by_login_app(target.lower())
    if not user:
        return None, None
    return user["id"], user["login"]


async def chat_connect_menu(session: PromptSession, session_factory, twitch: TwitchClient) -> None:
    bot = await select_bot_account(session, session_factory)
    if not bot:
        return

    print(f"\nConnecting EventSub websocket for chat as bot '{bot.name}'...")
    try:
        ws, session_id = await connect_eventsub_websocket(twitch)
    except Exception as exc:
        print(f"Failed to connect EventSub websocket: {exc}")
        return

    print(f"Connected. EventSub session_id={session_id}")
    print("Chat event type used: channel.chat.message")
    print("Tip: for self-test you can subscribe bot to its own channel chat.\n")

    try:
        while True:
            print(
                "\nChat Connect\n"
                "1) List chat subscriptions\n"
                "2) Subscribe bot to channel chat\n"
                "3) Subscribe bot to its own chat (self-test)\n"
                "4) Listen for chat events\n"
                "5) Disconnect and return\n"
            )
            choice = (await session.prompt_async("Select option: ")).strip()

            if choice == "1":
                try:
                    subs = await list_chat_subscriptions(twitch, bot.twitch_user_id)
                except Exception as exc:
                    print(f"Failed listing chat subscriptions: {exc}")
                    continue
                if not subs:
                    print("No chat subscriptions found for this bot.")
                    continue
                print("\nCurrent chat subscriptions:")
                for sub in subs:
                    cond = sub.get("condition", {})
                    print(
                        f"- id={sub.get('id')} status={sub.get('status')} type={sub.get('type')} "
                        f"broadcaster_user_id={cond.get('broadcaster_user_id')} user_id={cond.get('user_id')}"
                    )

            elif choice == "2":
                target = (await session.prompt_async("Target channel login or user_id: ")).strip()
                try:
                    broadcaster_user_id, broadcaster_login = await resolve_broadcaster_user_id(twitch, target)
                except Exception as exc:
                    print(f"Failed resolving target channel: {exc}")
                    continue
                if not broadcaster_user_id:
                    print("Target channel not found.")
                    continue

                try:
                    created = await twitch.create_eventsub_subscription(
                        event_type="channel.chat.message",
                        version="1",
                        condition={
                            "broadcaster_user_id": broadcaster_user_id,
                            "user_id": bot.twitch_user_id,
                        },
                        transport={"method": "websocket", "session_id": session_id},
                    )
                    confirmed = await twitch.list_eventsub_subscriptions()
                    success = any(sub.get("id") == created["id"] for sub in confirmed)
                    if not success:
                        print("Subscription call returned, but could not confirm from Twitch list.")
                        continue
                    await upsert_chat_subscription_record(
                        session_factory=session_factory,
                        bot=bot,
                        sub=created,
                        broadcaster_user_id=broadcaster_user_id,
                    )
                    print(
                        f"Subscribed successfully: bot={bot.twitch_login} -> channel={broadcaster_login or broadcaster_user_id}"
                    )
                except Exception as exc:
                    print(f"Failed subscribing to channel chat: {exc}")

            elif choice == "3":
                try:
                    created = await twitch.create_eventsub_subscription(
                        event_type="channel.chat.message",
                        version="1",
                        condition={
                            "broadcaster_user_id": bot.twitch_user_id,
                            "user_id": bot.twitch_user_id,
                        },
                        transport={"method": "websocket", "session_id": session_id},
                    )
                    confirmed = await twitch.list_eventsub_subscriptions()
                    success = any(sub.get("id") == created["id"] for sub in confirmed)
                    if not success:
                        print("Subscription call returned, but could not confirm from Twitch list.")
                        continue
                    await upsert_chat_subscription_record(
                        session_factory=session_factory,
                        bot=bot,
                        sub=created,
                        broadcaster_user_id=bot.twitch_user_id,
                    )
                    print("Self-test subscription successful.")
                except Exception as exc:
                    print(f"Self-test subscription failed: {exc}")

            elif choice == "4":
                raw_seconds = (await session.prompt_async("Listen seconds [30]: ")).strip()
                listen_seconds = 30
                if raw_seconds:
                    try:
                        listen_seconds = max(1, int(raw_seconds))
                    except ValueError:
                        print("Invalid seconds value.")
                        continue

                print(f"Listening for chat notifications for {listen_seconds}s...")
                deadline = asyncio.get_running_loop().time() + listen_seconds
                while True:
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        break
                    try:
                        raw_msg = await asyncio.wait_for(ws.recv(), timeout=remaining)
                    except asyncio.TimeoutError:
                        break
                    except Exception as exc:
                        print(f"Listen stopped: {exc}")
                        break
                    payload = json.loads(raw_msg)
                    metadata = payload.get("metadata", {})
                    msg_type = metadata.get("message_type")
                    if msg_type == "notification":
                        sub = payload.get("payload", {}).get("subscription", {})
                        event = payload.get("payload", {}).get("event", {})
                        print(
                            f"[chat] type={sub.get('type')} broadcaster={event.get('broadcaster_user_login')} "
                            f"user={event.get('chatter_user_login')} text={event.get('message', {}).get('text')}"
                        )
                    elif msg_type == "session_keepalive":
                        continue
                    else:
                        print(f"[eventsub] message_type={msg_type}")
                print("Listen window finished.")

            elif choice == "5":
                break
            else:
                print("Invalid option.")
    finally:
        await ws.close()
        print("Chat EventSub connection closed.")


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
            "5) Create service account\n"
            "6) Regenerate service secret\n"
            "7) List service accounts\n"
            "8) Chat connect (EventSub)\n"
            "9) Exit\n"
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

        elif choice == "6":
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

        elif choice == "7":
            async with session_factory() as db:
                accounts = list((await db.scalars(select(ServiceAccount))).all())
            if not accounts:
                print("No service accounts.")
            for account in accounts:
                print(f"- {account.name}: client_id={account.client_id} enabled={account.enabled}")

        elif choice == "8":
            await chat_connect_menu(session, session_factory, twitch)

        elif choice == "9":
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
