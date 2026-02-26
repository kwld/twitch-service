from __future__ import annotations

import asyncio
import secrets
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from prompt_toolkit import PromptSession
from prompt_toolkit.shortcuts import checkboxlist_dialog
from sqlalchemy import delete, select

from app.auth import generate_client_id, generate_client_secret, hash_secret
from app.eventsub_catalog import (
    EVENTSUB_CATALOG,
    KNOWN_EVENT_TYPES,
    recommended_bot_scopes,
    recommended_broadcaster_scopes,
    required_scope_any_of_groups,
)
from app.models import (
    BotAccount,
    BroadcasterAuthorization,
    BroadcasterAuthorizationRequest,
    ServiceAccount,
    ServiceBotAccess,
    ServiceEventTrace,
    ServiceInterest,
    ServiceRuntimeStats,
    ServiceUserAuthRequest,
)
from app.twitch import TwitchClient


async def select_service_account(session: PromptSession, session_factory):
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


def _eventsub_selector_values() -> list[tuple[str, str]]:
    by_type: dict[str, str] = {}
    for entry in EVENTSUB_CATALOG:
        by_type.setdefault(entry.event_type, entry.title)
    return [
        (event_type, f"{event_type} - {title}")
        for event_type, title in sorted(by_type.items())
    ]


async def select_eventsub_types_checkbox() -> list[str]:
    values = _eventsub_selector_values()
    if not values:
        return []

    def _run_dialog():
        return checkboxlist_dialog(
            title="EventSub Scope Generator",
            text="Select EventSub types with [space], confirm with Enter:",
            values=values,
            ok_text="Generate",
            cancel_text="Cancel",
        ).run()

    selected = await asyncio.to_thread(_run_dialog)
    return sorted(selected or [])


async def eventsub_scope_generator_menu() -> None:
    selected_event_types = await select_eventsub_types_checkbox()
    if not selected_event_types:
        print("No event types selected.")
        return

    broadcaster_scope_set = {"channel:bot"}
    bot_scope_set: set[str] = set()
    for event_type in selected_event_types:
        broadcaster_scope_set.update(recommended_broadcaster_scopes(event_type))
        bot_scope_set.update(recommended_bot_scopes(event_type))
    requested_scope_list = sorted(broadcaster_scope_set)

    print("\nSelected EventSub types:")
    for event_type in selected_event_types:
        print(f"- {event_type}")

    print("\nGenerated recommended scope set:")
    print(", ".join(requested_scope_list))
    if bot_scope_set:
        print("\nSuggested bot token scope set (separate from broadcaster grant):")
        print(", ".join(sorted(bot_scope_set)))

    print("\nPer-event required scope ANY-OF groups:")
    for event_type in selected_event_types:
        groups = required_scope_any_of_groups(event_type)
        if not groups:
            print(f"- {event_type}: no explicit OAuth scope requirement")
            continue
        formatted_groups = ["|".join(sorted(group)) for group in groups]
        print(f"- {event_type}: " + " AND ".join(formatted_groups))


async def _build_authorization_scope_menu(
    session: PromptSession,
) -> tuple[list[str], list[str], str]:
    while True:
        print(
            "\nScope Mode\n"
            "1) Minimal (channel:bot)\n"
            "2) Recommended from selected EventSub types\n"
            "3) Custom scopes (CSV)\n"
            "4) Cancel\n"
        )
        choice = (await session.prompt_async("Select option: ")).strip()
        if choice == "1":
            return ["channel:bot"], [], "minimal"
        if choice == "2":
            selected_event_types = await select_eventsub_types_checkbox()
            if not selected_event_types:
                print("No EventSub types selected.")
                continue
            scope_set = {"channel:bot"}
            for event_type in selected_event_types:
                scope_set.update(recommended_broadcaster_scopes(event_type))
            bot_scope_set: set[str] = set()
            for event_type in selected_event_types:
                bot_scope_set.update(recommended_bot_scopes(event_type))
            if bot_scope_set:
                print("\nNote: selected events also need bot token scopes:")
                print(", ".join(sorted(bot_scope_set)))
            return sorted(scope_set), selected_event_types, "recommended"
        if choice == "3":
            raw_custom = (await session.prompt_async("Custom scopes CSV: ")).strip()
            custom_scopes = sorted({x.strip() for x in raw_custom.split(",") if x.strip()})
            if not custom_scopes:
                print("No scopes provided.")
                continue
            include_base = (await session.prompt_async("Include channel:bot? [Y/n]: ")).strip().lower()
            if include_base in {"", "y", "yes"}:
                custom_scopes = sorted(set(custom_scopes) | {"channel:bot"})
            return custom_scopes, [], "custom"
        if choice == "4":
            return [], [], "canceled"
        print("Invalid option.")


async def print_service_bot_access(session_factory, service_id) -> None:
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
    service = await select_service_account(session, session_factory)
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
            await print_service_bot_access(session_factory, service.id)
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
            account = await select_service_account(session, session_factory)
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
            account = await select_service_account(session, session_factory)
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
                await db.execute(
                    delete(BroadcasterAuthorization).where(
                        BroadcasterAuthorization.service_account_id == target.id
                    )
                )
                await db.execute(
                    delete(BroadcasterAuthorizationRequest).where(
                        BroadcasterAuthorizationRequest.service_account_id == target.id
                    )
                )
                await db.execute(
                    delete(ServiceBotAccess).where(ServiceBotAccess.service_account_id == target.id)
                )
                await db.execute(
                    delete(ServiceInterest).where(ServiceInterest.service_account_id == target.id)
                )
                await db.execute(
                    delete(ServiceRuntimeStats).where(ServiceRuntimeStats.service_account_id == target.id)
                )
                await db.execute(
                    delete(ServiceUserAuthRequest).where(ServiceUserAuthRequest.service_account_id == target.id)
                )
                await db.execute(
                    delete(ServiceEventTrace).where(ServiceEventTrace.service_account_id == target.id)
                )
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


async def authorize_bot_self_channel_menu(
    session: PromptSession,
    session_factory,
    twitch: TwitchClient,
    *,
    select_bot_account_fn: Callable[[PromptSession, object], Awaitable[BotAccount | None]],
    ask_yes_no_fn: Callable[[PromptSession, str, bool], Awaitable[bool]],
    obtain_oauth_code_for_scopes_fn: Callable[..., Awaitable[str | None]],
) -> None:
    service = await select_service_account(session, session_factory)
    if not service:
        return
    bot = await select_bot_account_fn(session, session_factory)
    if not bot:
        return

    requested_scope_list, requested_event_types, scope_mode = await _build_authorization_scope_menu(session)
    if scope_mode == "canceled" or not requested_scope_list:
        print("Canceled.")
        return
    invalid = [x for x in requested_event_types if x not in KNOWN_EVENT_TYPES]
    if invalid:
        print("Unsupported event types: " + ", ".join(sorted(set(invalid))))
        return

    print(f"\nAuthorizing service '{service.name}' for bot '{bot.name}' in bot's own channel.")
    print(f"Scope mode: {scope_mode}")
    if requested_event_types:
        print("Selected EventSub types: " + ", ".join(requested_event_types))
    print("Requested scopes: " + ", ".join(requested_scope_list))
    if not await ask_yes_no_fn(session, "Continue?", default_yes=True):
        return

    state = secrets.token_urlsafe(24)
    code = await obtain_oauth_code_for_scopes_fn(
        session=session,
        session_factory=session_factory,
        twitch=twitch,
        state=state,
        scopes=requested_scope_list,
    )
    if not code:
        return

    try:
        token = await twitch.exchange_code(code)
        token_info = await twitch.validate_user_token(token.access_token)
    except Exception as exc:
        print(f"OAuth/token validation failed: {exc}")
        return

    broadcaster_user_id = str(token_info.get("user_id", "")).strip()
    broadcaster_login = str(token_info.get("login", "")).strip().lower()
    granted_scopes = sorted(set(token_info.get("scopes", [])))
    missing = sorted(set(requested_scope_list) - set(granted_scopes))
    if missing:
        print("Missing required granted scopes: " + ", ".join(missing))
        return
    if broadcaster_user_id != bot.twitch_user_id:
        print(
            "Authorized account does not match selected bot. "
            f"Expected user_id={bot.twitch_user_id}, got {broadcaster_user_id}."
        )
        return

    async with session_factory() as db:
        row = await db.scalar(
            select(BroadcasterAuthorization).where(
                BroadcasterAuthorization.service_account_id == service.id,
                BroadcasterAuthorization.bot_account_id == bot.id,
                BroadcasterAuthorization.broadcaster_user_id == broadcaster_user_id,
            )
        )
        scopes_csv = ",".join(granted_scopes)
        now = datetime.now(UTC)
        if row:
            row.broadcaster_login = broadcaster_login
            row.scopes_csv = scopes_csv
            row.authorized_at = now
        else:
            db.add(
                BroadcasterAuthorization(
                    service_account_id=service.id,
                    bot_account_id=bot.id,
                    broadcaster_user_id=broadcaster_user_id,
                    broadcaster_login=broadcaster_login,
                    scopes_csv=scopes_csv,
                    authorized_at=now,
                )
            )
        await db.commit()
    print("Broadcaster authorization saved for bot own channel.")
