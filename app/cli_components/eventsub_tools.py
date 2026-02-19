from __future__ import annotations

import asyncio
import json
from contextlib import suppress

from prompt_toolkit import PromptSession
from sqlalchemy import delete, select
import websockets

from app.bot_auth import ensure_bot_access_token
from app.event_router import InterestRegistry, LocalEventHub
from app.eventsub_manager import EventSubManager
from app.models import BotAccount, TwitchSubscription
from app.twitch import TwitchClient


def render_eventsub_subscription_line(idx: int, sub: dict) -> str:
    event_type = sub.get("type", "?")
    status = sub.get("status", "?")
    transport = sub.get("transport", {}).get("method", "?")
    condition = sub.get("condition", {})
    broadcaster = condition.get("broadcaster_user_id", "-")
    user_id = condition.get("user_id")
    suffix = f" user_id={user_id}" if user_id else ""
    return (
        f"{idx}) id={sub.get('id')} type={event_type} status={status} "
        f"transport={transport} broadcaster={broadcaster}{suffix}"
    )


async def delete_eventsub_subscription_cli(
    session_factory,
    twitch: TwitchClient,
    sub: dict,
) -> None:
    sub_id = str(sub.get("id", "")).strip()
    if not sub_id:
        return
    transport = str(sub.get("transport", {}).get("method", ""))
    access_token: str | None = None
    if transport == "websocket":
        condition = sub.get("condition", {})
        bot_user_id = str(condition.get("user_id", "")).strip()
        if bot_user_id:
            async with session_factory() as db:
                bot = await db.scalar(select(BotAccount).where(BotAccount.twitch_user_id == bot_user_id))
                if bot and bot.enabled:
                    with suppress(Exception):
                        access_token = await ensure_bot_access_token(db, twitch, bot)
    await twitch.delete_eventsub_subscription(sub_id, access_token=access_token)
    async with session_factory() as db:
        db_rows = list(
            (
                await db.scalars(
                    select(TwitchSubscription).where(TwitchSubscription.twitch_subscription_id == sub_id)
                )
            ).all()
        )
        for row in db_rows:
            await db.delete(row)
        await db.commit()


async def list_active_eventsub_subscriptions_cli(session_factory, twitch: TwitchClient) -> list[dict]:
    by_id: dict[str, dict] = {}

    with suppress(Exception):
        app_subs = await twitch.list_eventsub_subscriptions()
        for sub in app_subs:
            sub_id = str(sub.get("id", "")).strip()
            if sub_id:
                by_id[sub_id] = sub

    async with session_factory() as db:
        bots = list((await db.scalars(select(BotAccount).where(BotAccount.enabled.is_(True)))).all())

    for bot in bots:
        with suppress(Exception):
            async with session_factory() as db:
                db_bot = await db.get(BotAccount, bot.id)
                if not db_bot:
                    continue
                token = await ensure_bot_access_token(db, twitch, db_bot)
            user_subs = await twitch.list_eventsub_subscriptions(access_token=token)
            for sub in user_subs:
                sub_id = str(sub.get("id", "")).strip()
                if sub_id and sub_id not in by_id:
                    by_id[sub_id] = sub

    return list(by_id.values())


async def manage_eventsub_subscriptions_menu(
    session: PromptSession,
    session_factory,
    twitch: TwitchClient,
    settings,
    websocket_listener_cooldown_remaining_cli_fn,
    format_duration_short_fn,
) -> None:
    async def _open_eventsub_welcome_session_id() -> tuple[websockets.WebSocketClientProtocol, str]:
        ws = await websockets.connect(twitch.eventsub_ws_url, max_size=4 * 1024 * 1024)
        raw = await asyncio.wait_for(ws.recv(), timeout=15)
        welcome = json.loads(raw)
        msg_type = welcome.get("metadata", {}).get("message_type")
        if msg_type != "session_welcome":
            await ws.close()
            raise RuntimeError(f"Unexpected first EventSub message: {msg_type}")
        session_id = str(welcome.get("payload", {}).get("session", {}).get("id", "")).strip()
        if not session_id:
            await ws.close()
            raise RuntimeError("EventSub welcome did not include session id")
        return ws, session_id

    async def _recreate_all_from_interests() -> None:
        print("Fetching active subscriptions...")
        subs = await list_active_eventsub_subscriptions_cli(session_factory, twitch)
        print(f"Deleting {len(subs)} active subscriptions from Twitch...")
        failures = 0
        for sub in subs:
            try:
                await delete_eventsub_subscription_cli(session_factory, twitch, sub)
            except Exception as exc:
                failures += 1
                print(f"- failed delete {sub.get('id')}: {exc}")
        if failures:
            raise RuntimeError(f"Failed deleting {failures} subscription(s); aborting recreate")

        async with session_factory() as db:
            await db.execute(delete(TwitchSubscription))
            await db.commit()

        manager = EventSubManager(
            twitch_client=twitch,
            session_factory=session_factory,
            registry=InterestRegistry(),
            event_hub=LocalEventHub(),
            chat_assets=None,
            webhook_event_types={
                x.strip()
                for x in settings.twitch_eventsub_webhook_event_types.split(",")
                if x.strip()
            },
            webhook_callback_url=settings.twitch_eventsub_webhook_callback_url,
            webhook_secret=settings.twitch_eventsub_webhook_secret,
        )

        ws = None
        try:
            await manager._load_interests()
            await manager._ensure_authorization_revoke_subscription()
            await manager._ensure_webhook_subscriptions()
            if await manager._has_websocket_interest():
                ws, session_id = await _open_eventsub_welcome_session_id()
                manager._session_id = session_id
                await manager._ensure_all_subscriptions()
            await manager._sync_from_twitch_and_reconcile()
        finally:
            if ws:
                with suppress(Exception):
                    await ws.close()
            with suppress(Exception):
                await manager.event_hub.close()

    filter_mode = "all"
    while True:
        try:
            subs = await list_active_eventsub_subscriptions_cli(session_factory, twitch)
        except Exception as exc:
            print(f"Failed listing subscriptions: {exc}")
            return

        if filter_mode == "webhook":
            view_subs = [s for s in subs if s.get("transport", {}).get("method") == "webhook"]
        elif filter_mode == "websocket":
            view_subs = [s for s in subs if s.get("transport", {}).get("method") == "websocket"]
        else:
            view_subs = subs

        status_counts: dict[str, int] = {}
        for sub in view_subs:
            status = str(sub.get("status", "unknown"))
            status_counts[status] = status_counts.get(status, 0) + 1
        status_summary = ", ".join(f"{k}={v}" for k, v in sorted(status_counts.items())) if status_counts else "none"
        cooldown_remaining = await websocket_listener_cooldown_remaining_cli_fn(session_factory)

        print(f"\nActive EventSub subscriptions (filter={filter_mode}):")
        print(f"Status counts: {status_summary}")
        if cooldown_remaining is None:
            print("Service WS listener cooldown: active listeners connected")
        else:
            print(
                "Service WS listener cooldown remaining: "
                f"{format_duration_short_fn(cooldown_remaining)}"
            )
        if not view_subs:
            print("- none -")
        else:
            for idx, sub in enumerate(view_subs, start=1):
                print(render_eventsub_subscription_line(idx, sub))

        print("\nOptions:")
        print("1) Unsubscribe by list number")
        print("2) Unsubscribe all shown")
        print("3) Unsubscribe by exact subscription id")
        print("4) Show all")
        print("5) Show webhook only")
        print("6) Show websocket only")
        print("7) Delete all and recreate from interests")
        print("8) Back")
        choice = (await session.prompt_async("Select option: ")).strip()

        if choice == "1":
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
            sub_id = str(sub.get("id", ""))
            confirm = (await session.prompt_async(f"Type '{sub_id}' to confirm unsubscribe: ")).strip()
            if confirm != sub_id:
                print("Confirmation mismatch.")
                continue
            try:
                await delete_eventsub_subscription_cli(session_factory, twitch, sub)
                print(f"Unsubscribed {sub_id}")
            except Exception as exc:
                print(f"Failed unsubscribing: {exc}")
            continue
        if choice == "2":
            if not view_subs:
                print("Nothing to unsubscribe.")
                continue
            confirm = (await session.prompt_async("Type 'unsubscribe all' to confirm: ")).strip().lower()
            if confirm != "unsubscribe all":
                print("Canceled.")
                continue
            failures = 0
            for sub in list(view_subs):
                try:
                    await delete_eventsub_subscription_cli(session_factory, twitch, sub)
                    print(f"- removed {sub.get('id')}")
                except Exception as exc:
                    failures += 1
                    print(f"- failed {sub.get('id')}: {exc}")
            if failures:
                print(f"Completed with {failures} failures.")
            else:
                print("All shown subscriptions removed.")
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
                await delete_eventsub_subscription_cli(session_factory, twitch, sub)
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
            confirm = (
                await session.prompt_async(
                    "Type 'recreate all from interests' to confirm destructive rebuild: "
                )
            ).strip().lower()
            if confirm != "recreate all from interests":
                print("Canceled.")
                continue
            try:
                await _recreate_all_from_interests()
                print("Recreated upstream subscriptions from persisted interests.")
            except Exception as exc:
                print(f"Failed rebuild: {exc}")
            continue
        if choice == "8":
            return
        print("Invalid option.")
