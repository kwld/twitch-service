from __future__ import annotations

from contextlib import suppress

from prompt_toolkit import PromptSession
from sqlalchemy import select

from app.bot_auth import ensure_bot_access_token
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
    websocket_listener_cooldown_remaining_cli_fn,
    format_duration_short_fn,
) -> None:
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
        print("7) Back")
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
            return
        print("Invalid option.")

