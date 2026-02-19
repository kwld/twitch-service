from __future__ import annotations

import asyncio
import json
from contextlib import suppress

from prompt_toolkit import PromptSession
from sqlalchemy import select
import websockets

from app.cli_components.bot_workflows import (
    ask_yes_no,
    get_bot_access_token,
    select_bot_account,
)
from app.models import BotAccount, TwitchSubscription
from app.twitch import TwitchClient


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


async def run_live_chat_session(
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
            print(f"\n[{channel_label}] <{chatter}> {text}")
            print("> ", end="", flush=True)

    async def _sender() -> None:
        while not stop.is_set():
            try:
                text = await session.prompt_async("> ")
            except (EOFError, KeyboardInterrupt):
                stop.set()
                return
            content = text.strip()
            if not content:
                continue
            if content.lower() in {"/quit", "/exit"}:
                stop.set()
                return
            try:
                await twitch.send_chat_message(
                    access_token=await get_bot_access_token(session_factory, twitch, bot.id),
                    broadcaster_id=broadcaster_user_id,
                    sender_id=bot.twitch_user_id,
                    message=content,
                    reply_parent_message_id=None,
                )
            except Exception as exc:
                print(f"[send failed] {exc}")

    receiver_task = asyncio.create_task(_receiver())
    sender_task = asyncio.create_task(_sender())
    await asyncio.wait({receiver_task, sender_task}, return_when=asyncio.FIRST_COMPLETED)
    stop.set()
    receiver_task.cancel()
    sender_task.cancel()
    with suppress(Exception):
        await ws.close()
    print("Live chat session ended.")


async def chat_connect_menu(session: PromptSession, session_factory, twitch: TwitchClient) -> None:
    bot = await select_bot_account(session, session_factory)
    if not bot:
        return
    broadcaster_user_id = bot.twitch_user_id
    channel_label = f"{bot.twitch_login} (own channel)"
    await run_live_chat_session(session, session_factory, twitch, bot, broadcaster_user_id, channel_label)


async def chat_connect_other_channel_menu(session: PromptSession, session_factory, twitch: TwitchClient) -> None:
    bot = await select_bot_account(session, session_factory)
    if not bot:
        return
    raw = (await session.prompt_async("Target channel login or user id: ")).strip()
    broadcaster_user_id, broadcaster_login = await resolve_target_channel(twitch, raw)
    if not broadcaster_user_id:
        print("Could not resolve target channel.")
        return
    label = broadcaster_login or broadcaster_user_id
    channel_label = f"{label} (target channel)"
    await run_live_chat_session(session, session_factory, twitch, bot, broadcaster_user_id, channel_label)


async def create_clip_menu(session: PromptSession, session_factory, twitch: TwitchClient) -> None:
    bot = await select_bot_account(session, session_factory)
    if not bot:
        return

    raw_target = (await session.prompt_async("Target channel login or user id [own]: ")).strip()
    if raw_target:
        broadcaster_user_id, broadcaster_login = await resolve_target_channel(twitch, raw_target)
        if not broadcaster_user_id:
            print("Could not resolve target channel.")
            return
    else:
        broadcaster_user_id = bot.twitch_user_id
        broadcaster_login = bot.twitch_login

    title = (await session.prompt_async("Clip title: ")).strip()
    if not title:
        print("Title is required.")
        return

    raw_duration = (await session.prompt_async("Duration seconds [30]: ")).strip()
    if raw_duration:
        try:
            duration = float(raw_duration)
        except ValueError:
            print("Invalid duration.")
            return
    else:
        duration = 30.0
    if duration < 5 or duration > 60:
        print("Duration must be between 5 and 60 seconds.")
        return

    has_delay = await ask_yes_no(
        session,
        "Use has_delay=true (recommended for clipping moments that just happened)?",
        default_yes=True,
    )

    try:
        access_token = await get_bot_access_token(session_factory, twitch, bot.id)
        clip = await twitch.create_clip(
            access_token=access_token,
            broadcaster_id=broadcaster_user_id,
            title=title,
            duration=duration,
            has_delay=has_delay,
        )
        clip_id = str(clip.get("id", "")).strip()
        if not clip_id:
            print("Clip creation returned empty id.")
            return
        edit_url = str(clip.get("edit_url", "")).strip()
        print(f"Clip created: id={clip_id}")
        if edit_url:
            print(f"Edit URL: {edit_url}")

        print("Waiting up to 15s for clip metadata...")
        ready = None
        for _ in range(15):
            await asyncio.sleep(1)
            items = await twitch.get_clips(access_token=access_token, clip_ids=[clip_id])
            if items:
                ready = items[0]
                break
        if ready:
            print("Clip ready:")
            print(f"- url: {ready.get('url')}")
            print(f"- embed_url: {ready.get('embed_url')}")
            print(f"- thumbnail_url: {ready.get('thumbnail_url')}")
            print(f"- created_at: {ready.get('created_at')}")
        else:
            print("Clip is still processing on Twitch. Check again shortly.")

        target_label = broadcaster_login or broadcaster_user_id
        print(f"Bot: {bot.name} ({bot.twitch_login}) -> channel: {target_label}")
        print(f"has_delay={has_delay}")
    except Exception as exc:
        print(f"Failed to create clip: {exc}")


async def remove_bot_menu(session: PromptSession, session_factory, twitch: TwitchClient) -> None:
    _ = twitch
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
        idx = int(raw)
    except ValueError:
        print("Invalid selection.")
        return
    if idx < 1 or idx > len(bots):
        print("Invalid selection.")
        return
    target = bots[idx - 1]
    confirm = (await session.prompt_async(f"Type '{target.name}' to confirm deletion: ")).strip()
    if confirm != target.name:
        print("Confirmation mismatch. Canceled.")
        return
    async with session_factory() as db:
        row = await db.get(BotAccount, target.id)
        if not row:
            print("Bot already removed.")
            return
        await db.delete(row)
        await db.commit()
    print(f"Removed bot: {target.name}")

