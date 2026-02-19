from __future__ import annotations

import secrets
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from prompt_toolkit import PromptSession
from sqlalchemy import delete, select

from app.auth import generate_client_id, generate_client_secret, hash_secret
from app.eventsub_catalog import KNOWN_EVENT_TYPES, recommended_broadcaster_scopes
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

    raw_event_types = (await session.prompt_async("Event types CSV (optional): ")).strip()
    requested_event_types = [x.strip().lower() for x in raw_event_types.split(",") if x.strip()]
    invalid = [x for x in requested_event_types if x not in KNOWN_EVENT_TYPES]
    if invalid:
        print("Unsupported event types: " + ", ".join(sorted(set(invalid))))
        return

    requested_scopes = {"channel:bot"}
    for event_type in requested_event_types:
        requested_scopes.update(recommended_broadcaster_scopes(event_type))
    requested_scope_list = sorted(requested_scopes)

    print(f"\nAuthorizing service '{service.name}' for bot '{bot.name}' in bot's own channel.")
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

