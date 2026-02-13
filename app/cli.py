from __future__ import annotations

import argparse
import asyncio
import json
import secrets
from contextlib import suppress
from urllib.parse import parse_qs, urlparse

from prompt_toolkit import PromptSession
from sqlalchemy import delete, func, select
import websockets

from app.auth import generate_client_id, generate_client_secret, hash_secret
from app.bot_auth import ensure_bot_access_token
from app.config import load_settings
from app.db import create_engine_and_session
from app.models import (
    Base,
    BotAccount,
    BroadcasterAuthorization,
    OAuthCallback,
    ServiceAccount,
    ServiceBotAccess,
    ServiceInterest,
    ServiceRuntimeStats,
    TwitchSubscription,
)
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


async def wait_for_oauth_callback(
    session_factory,
    state: str,
    timeout_seconds: int = 300,
    poll_interval_seconds: float = 1.0,
) -> tuple[str | None, str | None]:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while asyncio.get_running_loop().time() < deadline:
        async with session_factory() as db:
            row = await db.get(OAuthCallback, state)
            if row and (row.code or row.error):
                code, error = row.code, row.error
                await db.delete(row)
                await db.commit()
                return code, error
        await asyncio.sleep(poll_interval_seconds)
    return None, None


async def obtain_oauth_code(
    session: PromptSession,
    session_factory,
    twitch: TwitchClient,
    state: str,
) -> str | None:
    async with session_factory() as db:
        existing = await db.get(OAuthCallback, state)
        if existing:
            await db.delete(existing)
            await db.commit()

    auth_url = twitch.build_authorize_url(state=state)
    print("\nStep 1: Open this URL and authorize the Twitch account to use as bot:\n")
    print(auth_url)
    print("\nStep 2: Complete the browser flow. CLI will auto-detect callback for up to 5 minutes.")
    print("Waiting for OAuth callback...")

    code, error = await wait_for_oauth_callback(session_factory, state=state)
    if error:
        print(f"OAuth failed with error: {error}")
        return None
    if code:
        print("OAuth callback confirmed.")
        return code

    print("Timed out waiting for callback.")
    print("Paste full redirect URL to continue, or leave blank to cancel.")
    callback = (await session.prompt_async("Redirect URL: ")).strip()
    if not callback:
        return None
    code, returned_state, error = parse_oauth_callback(callback)
    if error:
        print(f"OAuth failed with error: {error}")
        return None
    if not code:
        print("No OAuth code found in redirect URL.")
        return None
    if returned_state != state:
        print("State mismatch. Stop and retry guided setup (possible CSRF or wrong callback URL).")
        return None
    return code


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
    code = await obtain_oauth_code(session, session_factory, twitch, state=state)
    if not code:
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


async def list_chat_subscriptions(
    twitch: TwitchClient,
    bot_user_id: str,
    access_token: str,
) -> list[dict]:
    subs = await twitch.list_eventsub_subscriptions(access_token=access_token)
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


async def get_bot_access_token(session_factory, twitch: TwitchClient, bot_id) -> str:
    async with session_factory() as db:
        bot = await db.get(BotAccount, bot_id)
        if not bot:
            raise RuntimeError("Bot not found")
        return await ensure_bot_access_token(db, twitch, bot)


async def resolve_target_channel(twitch: TwitchClient, raw_target: str) -> tuple[str | None, str | None]:
    target = raw_target.strip()
    if not target:
        return None, None
    if target.isdigit():
        user = await twitch.get_user_by_id_app(target)
    else:
        user = await twitch.get_user_by_login_app(target.lower())
    if not user:
        return None, None
    return str(user.get("id")), user.get("login")


async def _run_live_chat_session(
    session: PromptSession,
    session_factory,
    twitch: TwitchClient,
    bot: BotAccount,
    broadcaster_user_id: str,
    channel_label: str,
) -> None:
    try:
        token = await get_bot_access_token(session_factory, twitch, bot.id)
        token_info = await twitch.validate_user_token(token)
        granted_scopes = set(token_info.get("scopes", []))
        required_scopes = {"user:read:chat", "user:write:chat", "user:bot"}
        missing_scopes = sorted(required_scopes - granted_scopes)
        if missing_scopes:
            print(
                "Bot token is missing required scopes for chat EventSub: "
                f"{', '.join(missing_scopes)}"
            )
            print(
                "Update TWITCH_DEFAULT_SCOPES and re-run Guided bot setup to refresh bot OAuth token."
            )
            return
        token_user_id = str(token_info.get("user_id", ""))
        if token_user_id != bot.twitch_user_id:
            print(
                "Bot token user_id mismatch. Re-run Guided bot setup to store OAuth tokens "
                "for the selected bot account."
            )
            return
    except Exception as exc:
        print(f"Warning: could not validate bot token scopes: {exc}")
        return

    print(f"\nConnecting EventSub websocket for chat as bot '{bot.name}'...")
    try:
        ws, session_id = await connect_eventsub_websocket(twitch)
    except Exception as exc:
        print(f"Failed to connect EventSub websocket: {exc}")
        return

    try:
        access_token = await get_bot_access_token(session_factory, twitch, bot.id)
        created = await twitch.create_eventsub_subscription(
            event_type="channel.chat.message",
            version="1",
            condition={
                "broadcaster_user_id": broadcaster_user_id,
                "user_id": bot.twitch_user_id,
            },
            transport={"method": "websocket", "session_id": session_id},
            access_token=access_token,
        )
        await upsert_chat_subscription_record(
            session_factory=session_factory,
            bot=bot,
            sub=created,
            broadcaster_user_id=broadcaster_user_id,
        )
    except Exception as exc:
        print(f"Failed subscribing to channel chat: {exc}")
        text = str(exc).lower()
        if "403" in text and "missing proper authorization" in text:
            print(
                "Twitch returned 403. Common causes: missing chat scopes or the bot is banned/timed out "
                "in the target channel."
            )
        print("Re-run Guided bot setup if scope/token permissions changed.")
        await ws.close()
        return

    stop = asyncio.Event()

    async def _receiver() -> None:
        while not stop.is_set():
            try:
                raw_msg = await ws.recv()
            except Exception as exc:
                print(f"\n[system] chat connection closed: {exc}")
                stop.set()
                return
            payload = json.loads(raw_msg)
            metadata = payload.get("metadata", {})
            msg_type = metadata.get("message_type")
            if msg_type == "session_keepalive":
                continue
            if msg_type != "notification":
                continue
            sub = payload.get("payload", {}).get("subscription", {})
            if sub.get("type") != "channel.chat.message":
                continue
            event = payload.get("payload", {}).get("event", {})
            chatter = (
                event.get("chatter_user_name")
                or event.get("chatter_user_login")
                or event.get("chatter_user_id")
                or "unknown"
            )
            text = event.get("message", {}).get("text") or ""
            print(f"\n[{chatter}] {text}")

    receiver_task = asyncio.create_task(_receiver())
    print(f"\nConnected to channel chat: {channel_label}.")
    if broadcaster_user_id == bot.twitch_user_id:
        print("[system] Bot badge will not appear in the bot's own broadcaster channel.")
    else:
        print(
            "[system] Bot badge requires app-token send plus broadcaster channel:bot authorization "
            "or moderator status."
        )
    print("Type a message and press Enter to send.")
    print("Commands: /quit to leave, /help to show commands.\n")

    try:
        while not stop.is_set():
            line = (await session.prompt_async(f"{bot.twitch_login}> ")).strip()
            if not line:
                continue
            if line == "/help":
                print("Commands: /quit, /help")
                continue
            if line == "/quit":
                stop.set()
                break
            try:
                auth_mode_used = "app"
                try:
                    app_token = await twitch.app_access_token()
                    result = await twitch.send_chat_message(
                        access_token=app_token,
                        broadcaster_id=broadcaster_user_id,
                        sender_id=bot.twitch_user_id,
                        message=line,
                    )
                except Exception:
                    auth_mode_used = "user"
                    token = await get_bot_access_token(session_factory, twitch, bot.id)
                    result = await twitch.send_chat_message(
                        access_token=token,
                        broadcaster_id=broadcaster_user_id,
                        sender_id=bot.twitch_user_id,
                        message=line,
                    )
                if not result.get("is_sent", False):
                    drop = result.get("drop_reason") or {}
                    print(f"[system] message dropped: {drop.get('code')} {drop.get('message')}")
                elif auth_mode_used != "app":
                    print("[system] sent with user token (no bot badge path).")
            except Exception as exc:
                print(f"[system] send failed: {exc}")
    finally:
        stop.set()
        receiver_task.cancel()
        with suppress(asyncio.CancelledError):
            await receiver_task
        await ws.close()
        print("Chat EventSub connection closed.")


async def chat_connect_menu(session: PromptSession, session_factory, twitch: TwitchClient) -> None:
    bot = await select_bot_account(session, session_factory)
    if not bot:
        return
    await _run_live_chat_session(
        session=session,
        session_factory=session_factory,
        twitch=twitch,
        bot=bot,
        broadcaster_user_id=bot.twitch_user_id,
        channel_label=bot.twitch_login,
    )


async def chat_connect_other_channel_menu(session: PromptSession, session_factory, twitch: TwitchClient) -> None:
    bot = await select_bot_account(session, session_factory)
    if not bot:
        return
    raw_target = (await session.prompt_async("Target channel login or user_id: ")).strip()
    if not raw_target:
        print("No target provided.")
        return
    try:
        broadcaster_user_id, broadcaster_login = await resolve_target_channel(twitch, raw_target)
    except Exception as exc:
        print(f"Failed resolving target channel: {exc}")
        return
    if not broadcaster_user_id:
        print("Target channel not found.")
        return
    await _run_live_chat_session(
        session=session,
        session_factory=session_factory,
        twitch=twitch,
        bot=bot,
        broadcaster_user_id=broadcaster_user_id,
        channel_label=broadcaster_login or broadcaster_user_id,
    )


async def remove_bot_menu(session: PromptSession, session_factory, twitch: TwitchClient) -> None:
    async with session_factory() as db:
        bots = list((await db.scalars(select(BotAccount))).all())
    if not bots:
        print("No bots configured.")
        return
    print("\nBots:")
    for idx, bot in enumerate(bots, start=1):
        print(f"{idx}) {bot.name} ({bot.twitch_login}/{bot.twitch_user_id}) enabled={bot.enabled}")
    raw = (await session.prompt_async("Select bot number to remove: ")).strip()
    try:
        selected = int(raw)
    except ValueError:
        print("Invalid selection.")
        return
    if selected < 1 or selected > len(bots):
        print("Invalid selection.")
        return
    bot = bots[selected - 1]
    confirm = (await session.prompt_async(f"Type '{bot.name}' to confirm removal: ")).strip()
    if confirm != bot.name:
        print("Confirmation mismatch; canceled.")
        return

    async with session_factory() as db:
        target = await db.get(BotAccount, bot.id)
        if not target:
            print("Bot already removed.")
            return
        subs = list(
            (
                await db.scalars(
                    select(TwitchSubscription).where(TwitchSubscription.bot_account_id == target.id)
                )
            ).all()
        )
        for sub in subs:
            with suppress(Exception):
                await twitch.delete_eventsub_subscription(sub.twitch_subscription_id)
        await db.delete(target)
        await db.commit()
    print(f"Removed bot: {bot.name}")


def _render_eventsub_subscription_line(idx: int, sub: dict) -> str:
    cond = sub.get("condition", {})
    transport = sub.get("transport", {})
    method = transport.get("method", "unknown")
    target = transport.get("callback") if method == "webhook" else transport.get("session_id")
    return (
        f"{idx}) id={sub.get('id')} type={sub.get('type')} status={sub.get('status')} "
        f"method={method} broadcaster={cond.get('broadcaster_user_id')} bot_user={cond.get('user_id')} "
        f"target={target}"
    )


async def _delete_eventsub_subscription_cli(
    session_factory,
    twitch: TwitchClient,
    sub: dict,
) -> None:
    subscription_id = str(sub.get("id", ""))
    event_type = str(sub.get("type", ""))
    condition = sub.get("condition", {})

    access_token: str | None = None
    if event_type.startswith("channel.chat."):
        bot_user_id = str(condition.get("user_id", "")).strip()
        if not bot_user_id:
            raise RuntimeError("Cannot delete chat subscription: missing condition.user_id")
        async with session_factory() as db:
            bot = await db.scalar(select(BotAccount).where(BotAccount.twitch_user_id == bot_user_id))
            if not bot:
                raise RuntimeError(
                    f"Cannot delete chat subscription {subscription_id}: no bot with twitch_user_id={bot_user_id}"
                )
            if not bot.enabled:
                raise RuntimeError(
                    f"Cannot delete chat subscription {subscription_id}: bot '{bot.name}' is disabled"
                )
            access_token = await ensure_bot_access_token(db, twitch, bot)

    await twitch.delete_eventsub_subscription(subscription_id, access_token=access_token)

    async with session_factory() as db:
        db_sub = await db.scalar(
            select(TwitchSubscription).where(TwitchSubscription.twitch_subscription_id == subscription_id)
        )
        if db_sub:
            await db.delete(db_sub)
            await db.commit()


async def manage_eventsub_subscriptions_menu(
    session: PromptSession,
    session_factory,
    twitch: TwitchClient,
) -> None:
    filter_mode = "all"
    while True:
        try:
            subs = await twitch.list_eventsub_subscriptions()
        except Exception as exc:
            print(f"Failed listing subscriptions: {exc}")
            return

        if filter_mode == "webhook":
            view_subs = [s for s in subs if s.get("transport", {}).get("method") == "webhook"]
        elif filter_mode == "websocket":
            view_subs = [s for s in subs if s.get("transport", {}).get("method") == "websocket"]
        else:
            view_subs = subs

        print(f"\nActive EventSub subscriptions (filter={filter_mode}):")
        if not view_subs:
            print("No active subscriptions for current filter.")
        else:
            for idx, sub in enumerate(view_subs, start=1):
                print(_render_eventsub_subscription_line(idx, sub))

        print("\nEventSub Subscription Menu")
        print("1) Refresh")
        print("2) Unsubscribe by number")
        print("3) Unsubscribe by id")
        print("4) Filter: all")
        print("5) Filter: webhook only")
        print("6) Filter: websocket only")
        print("7) Back")
        choice = (await session.prompt_async("Select option: ")).strip()
        if choice == "1":
            continue
        if choice == "2":
            if not view_subs:
                print("No subscriptions to unsubscribe.")
                continue
            raw = (await session.prompt_async("Subscription number: ")).strip()
            try:
                idx = int(raw)
            except ValueError:
                print("Invalid number.")
                continue
            if idx < 1 or idx > len(view_subs):
                print("Invalid number.")
                continue
            sub = view_subs[idx - 1]
            try:
                await _delete_eventsub_subscription_cli(session_factory, twitch, sub)
                print(f"Unsubscribed {sub['id']}")
            except Exception as exc:
                print(f"Failed unsubscribing: {exc}")
            continue
        if choice == "3":
            raw_id = (await session.prompt_async("Subscription id: ")).strip()
            if not raw_id:
                print("Subscription id is required.")
                continue
            sub = next((x for x in subs if str(x.get("id", "")) == raw_id), None)
            if not sub:
                print("Subscription id not found in active subscriptions.")
                continue
            try:
                await _delete_eventsub_subscription_cli(session_factory, twitch, sub)
                print(f"Unsubscribed {raw_id}")
            except Exception as exc:
                print(f"Failed unsubscribing: {exc}")
            continue
        if choice == "4":
            filter_mode = "all"
            continue
        if choice == "5":
            filter_mode = "webhook"
            continue
        if choice == "6":
            filter_mode = "websocket"
            continue
        if choice == "7":
            return
        print("Invalid option.")


async def _select_service_account(session: PromptSession, session_factory):
    async with session_factory() as db:
        accounts = list((await db.scalars(select(ServiceAccount))).all())
    if not accounts:
        print("No service accounts.")
        return None
    print("\nService accounts:")
    for idx, account in enumerate(accounts, start=1):
        print(f"{idx}) {account.name} client_id={account.client_id} enabled={account.enabled}")
    raw = (await session.prompt_async("Select service account (number/name/client_id): ")).strip()
    try:
        selected = int(raw)
        if selected < 1 or selected > len(accounts):
            print("Invalid selection.")
            return None
        return accounts[selected - 1]
    except ValueError:
        pass

    for account in accounts:
        if account.name == raw or account.client_id == raw:
            return account

    print("Invalid selection.")
    return None


async def _print_service_bot_access(session_factory, service_id) -> None:
    async with session_factory() as db:
        mappings = list(
            (
                await db.scalars(
                    select(ServiceBotAccess).where(ServiceBotAccess.service_account_id == service_id)
                )
            ).all()
        )
        bots = list((await db.scalars(select(BotAccount))).all())
    bot_by_id = {bot.id: bot for bot in bots}
    if not mappings:
        print("Access mode: all bots (no explicit restrictions).")
        for bot in bots:
            print(f"- {bot.name} ({bot.twitch_login}/{bot.twitch_user_id}) enabled={bot.enabled}")
        return
    print("Access mode: restricted")
    for mapping in mappings:
        bot = bot_by_id.get(mapping.bot_account_id)
        if not bot:
            print(f"- missing bot {mapping.bot_account_id}")
            continue
        print(f"- {bot.name} ({bot.twitch_login}/{bot.twitch_user_id}) enabled={bot.enabled}")


async def manage_service_bot_access_menu(session: PromptSession, session_factory) -> None:
    service = await _select_service_account(session, session_factory)
    if not service:
        return
    while True:
        print(f"\nManage Bot Access for service '{service.name}'")
        print("1) View current bot access")
        print("2) Grant bot access")
        print("3) Revoke bot access")
        print("4) Clear restrictions (allow all bots)")
        print("5) Back")
        choice = (await session.prompt_async("Select option: ")).strip()

        if choice == "1":
            await _print_service_bot_access(session_factory, service.id)
            continue

        if choice == "2":
            async with session_factory() as db:
                bots = list((await db.scalars(select(BotAccount))).all())
            if not bots:
                print("No bots available.")
                continue
            print("\nBots:")
            for idx, bot in enumerate(bots, start=1):
                print(f"{idx}) {bot.name} ({bot.twitch_login}/{bot.twitch_user_id}) enabled={bot.enabled}")
            raw = (await session.prompt_async("Bot number to grant: ")).strip()
            try:
                idx = int(raw)
            except ValueError:
                print("Invalid number.")
                continue
            if idx < 1 or idx > len(bots):
                print("Invalid number.")
                continue
            bot = bots[idx - 1]
            async with session_factory() as db:
                existing = await db.scalar(
                    select(ServiceBotAccess).where(
                        ServiceBotAccess.service_account_id == service.id,
                        ServiceBotAccess.bot_account_id == bot.id,
                    )
                )
                if existing:
                    print("Access already granted.")
                    continue
                db.add(ServiceBotAccess(service_account_id=service.id, bot_account_id=bot.id))
                await db.commit()
            print(f"Granted access: {service.name} -> {bot.name}")
            continue

        if choice == "3":
            async with session_factory() as db:
                mappings = list(
                    (
                        await db.scalars(
                            select(ServiceBotAccess).where(ServiceBotAccess.service_account_id == service.id)
                        )
                    ).all()
                )
                bots = list((await db.scalars(select(BotAccount))).all())
            if not mappings:
                print("No explicit bot access mappings. Service already has access to all bots.")
                continue
            bot_by_id = {bot.id: bot for bot in bots}
            print("\nGranted bot access:")
            for idx, mapping in enumerate(mappings, start=1):
                bot = bot_by_id.get(mapping.bot_account_id)
                if bot:
                    print(f"{idx}) {bot.name} ({bot.twitch_login}/{bot.twitch_user_id})")
                else:
                    print(f"{idx}) missing bot {mapping.bot_account_id}")
            raw = (await session.prompt_async("Access entry number to revoke: ")).strip()
            try:
                idx = int(raw)
            except ValueError:
                print("Invalid number.")
                continue
            if idx < 1 or idx > len(mappings):
                print("Invalid number.")
                continue
            mapping = mappings[idx - 1]
            async with session_factory() as db:
                await db.execute(delete(ServiceBotAccess).where(ServiceBotAccess.id == mapping.id))
                await db.commit()
            print("Access revoked.")
            continue

        if choice == "4":
            confirm = (await session.prompt_async("Type 'allow all' to confirm: ")).strip().lower()
            if confirm != "allow all":
                print("Canceled.")
                continue
            async with session_factory() as db:
                await db.execute(
                    delete(ServiceBotAccess).where(ServiceBotAccess.service_account_id == service.id)
                )
                await db.commit()
            print("Restrictions cleared. Service can access all bots.")
            continue

        if choice == "5":
            return
        print("Invalid option.")


async def manage_service_accounts_menu(session: PromptSession, session_factory) -> None:
    while True:
        print(
            "\nService Accounts\n"
            "1) List service accounts\n"
            "2) Create service account\n"
            "3) Regenerate service secret\n"
            "4) Delete service account\n"
            "5) Manage bot access\n"
            "6) Back\n"
        )
        choice = (await session.prompt_async("Select option: ")).strip()

        if choice == "1":
            async with session_factory() as db:
                accounts = list((await db.scalars(select(ServiceAccount))).all())
            if not accounts:
                print("No service accounts.")
                continue
            for account in accounts:
                print(f"- {account.name}: client_id={account.client_id} enabled={account.enabled}")
            continue

        if choice == "2":
            name = (await session.prompt_async("Service name: ")).strip()
            if not name:
                print("Service name is required.")
                continue
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
            continue

        if choice == "3":
            account = await _select_service_account(session, session_factory)
            if not account:
                continue
            new_secret = generate_client_secret()
            async with session_factory() as db:
                target = await db.get(ServiceAccount, account.id)
                if not target:
                    print("Service account not found.")
                    continue
                target.client_secret_hash = hash_secret(new_secret)
                await db.commit()
            print(f"\nNew client_secret: {new_secret}\n")
            continue

        if choice == "4":
            account = await _select_service_account(session, session_factory)
            if not account:
                continue
            confirm = (await session.prompt_async(f"Type '{account.name}' to confirm deletion: ")).strip()
            if confirm != account.name:
                print("Confirmation mismatch; canceled.")
                continue
            async with session_factory() as db:
                target = await db.get(ServiceAccount, account.id)
                if not target:
                    print("Service account already removed.")
                    continue
                await db.delete(target)
                await db.commit()
            print(f"Deleted service account: {account.name}")
            continue

        if choice == "5":
            await manage_service_bot_access_menu(session, session_factory)
            continue

        if choice == "6":
            return

        print("Invalid option.")


async def list_service_status_menu(session_factory) -> None:
    async with session_factory() as db:
        services = list((await db.scalars(select(ServiceAccount))).all())
        if not services:
            print("No service accounts.")
            return
        runtime_by_id = {
            s.service_account_id: s for s in list((await db.scalars(select(ServiceRuntimeStats))).all())
        }
        counts = (
            await db.execute(
                select(
                    ServiceInterest.service_account_id,
                    func.count(ServiceInterest.id),
                ).group_by(ServiceInterest.service_account_id)
            )
        ).all()
        interest_count_by_id = {sid: cnt for sid, cnt in counts}

    print("\nService Status and Usage:")
    for svc in services:
        stats = runtime_by_id.get(svc.id)
        interest_count = interest_count_by_id.get(svc.id, 0)
        if not stats:
            print(
                f"- {svc.name} client_id={svc.client_id} enabled={svc.enabled} "
                f"connected=False active_ws=0 interests={interest_count} api_requests=0 ws_events=0 webhook_events=0"
            )
            continue
        print(
            f"- {svc.name} client_id={svc.client_id} enabled={svc.enabled} "
            f"connected={stats.is_connected} active_ws={stats.active_ws_connections} "
            f"interests={interest_count} api_requests={stats.total_api_requests} "
            f"ws_events={stats.total_events_sent_ws} webhook_events={stats.total_events_sent_webhook} "
            f"last_connected={stats.last_connected_at} last_disconnected={stats.last_disconnected_at} "
            f"last_api={stats.last_api_request_at} last_event={stats.last_event_sent_at}"
        )


async def list_broadcaster_authorizations_menu(session_factory) -> None:
    async with session_factory() as db:
        auths = list((await db.scalars(select(BroadcasterAuthorization))).all())
        if not auths:
            print("No broadcaster authorizations recorded.")
            return
        services = list((await db.scalars(select(ServiceAccount))).all())
        bots = list((await db.scalars(select(BotAccount))).all())
    service_name_by_id = {s.id: s.name for s in services}
    bot_name_by_id = {b.id: b.name for b in bots}
    print("\nBroadcaster Authorizations:")
    for auth in auths:
        scopes = [x for x in auth.scopes_csv.split(",") if x]
        print(
            f"- service={service_name_by_id.get(auth.service_account_id, str(auth.service_account_id))} "
            f"bot={bot_name_by_id.get(auth.bot_account_id, str(auth.bot_account_id))} "
            f"broadcaster={auth.broadcaster_login}/{auth.broadcaster_user_id} "
            f"authorized_at={auth.authorized_at} scopes={','.join(scopes)}"
        )


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
            "12) Exit\n"
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
            await manage_eventsub_subscriptions_menu(session, session_factory, twitch)

        elif choice == "10":
            await list_service_status_menu(session_factory)

        elif choice == "11":
            await list_broadcaster_authorizations_menu(session_factory)

        elif choice == "12":
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
