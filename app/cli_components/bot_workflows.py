from __future__ import annotations

import asyncio
import secrets
from urllib.parse import parse_qs, urlparse

from prompt_toolkit import PromptSession
from sqlalchemy import select

from app.bot_auth import ensure_bot_access_token
from app.models import BotAccount, OAuthCallback
from app.twitch import TwitchApiError, TwitchClient


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


async def obtain_oauth_code_for_scopes(
    session: PromptSession,
    session_factory,
    twitch: TwitchClient,
    state: str,
    scopes: list[str],
) -> str | None:
    async with session_factory() as db:
        existing = await db.get(OAuthCallback, state)
        if existing:
            await db.delete(existing)
            await db.commit()

    auth_url = twitch.build_authorize_url_with_scopes(
        state=state,
        scopes=" ".join(scopes),
        force_verify=True,
    )
    print("\nOpen this URL and authorize with the broadcaster account:\n")
    print(auth_url)
    print("\nCLI will auto-detect callback for up to 5 minutes.")
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
        print("State mismatch. Stop and retry flow.")
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


async def get_bot_access_token(session_factory, twitch: TwitchClient, bot_id) -> str:
    async with session_factory() as db:
        bot = await db.get(BotAccount, bot_id)
        if not bot:
            raise RuntimeError("Bot not found")
        return await ensure_bot_access_token(db, twitch, bot)

