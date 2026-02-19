from __future__ import annotations

from datetime import UTC, datetime
from typing import Callable

from fastapi import FastAPI, HTTPException
from fastapi.responses import RedirectResponse
from sqlalchemy import select

from app.models import (
    BroadcasterAuthorization,
    BroadcasterAuthorizationRequest,
    OAuthCallback,
    ServiceUserAuthRequest,
)


def register_oauth_routes(
    app: FastAPI,
    *,
    session_factory,
    twitch_client,
    append_query: Callable[[str, dict[str, str]], str],
    broadcaster_auth_scopes: tuple[str, ...],
    service_user_auth_scopes: tuple[str, ...],
) -> None:
    @app.get("/oauth/callback")
    async def oauth_callback(code: str | None = None, state: str | None = None, error: str | None = None):
        if state:
            async with session_factory() as session:
                auth_request = await session.get(BroadcasterAuthorizationRequest, state)
                if auth_request:
                    redirect_url = auth_request.redirect_url
                    now = datetime.now(UTC)
                    if error:
                        auth_request.status = "failed"
                        auth_request.error = error
                        auth_request.completed_at = now
                        await session.commit()
                        if redirect_url:
                            return RedirectResponse(
                                url=append_query(
                                    redirect_url,
                                    {
                                        "ok": "false",
                                        "error": error,
                                        "message": "Broadcaster authorization failed.",
                                    },
                                ),
                                status_code=302,
                            )
                        return {
                            "ok": False,
                            "error": error,
                            "message": "Broadcaster authorization failed.",
                        }
                    if not code:
                        auth_request.status = "failed"
                        auth_request.error = "missing_code"
                        auth_request.completed_at = now
                        await session.commit()
                        if redirect_url:
                            return RedirectResponse(
                                url=append_query(
                                    redirect_url,
                                    {
                                        "ok": "false",
                                        "error": "missing_code",
                                        "message": "Missing OAuth code",
                                    },
                                ),
                                status_code=302,
                            )
                        raise HTTPException(status_code=400, detail="Missing OAuth code")

                    try:
                        token = await twitch_client.exchange_code(code)
                        token_info = await twitch_client.validate_user_token(token.access_token)
                    except Exception as exc:
                        auth_request.status = "failed"
                        auth_request.error = str(exc)
                        auth_request.completed_at = now
                        await session.commit()
                        if redirect_url:
                            return RedirectResponse(
                                url=append_query(
                                    redirect_url,
                                    {
                                        "ok": "false",
                                        "error": "oauth_exchange_failed",
                                        "message": f"OAuth exchange failed: {exc}",
                                    },
                                ),
                                status_code=302,
                            )
                        raise HTTPException(status_code=502, detail=f"OAuth exchange failed: {exc}") from exc

                    granted_scopes = sorted(set(token_info.get("scopes", [])))
                    required = set(broadcaster_auth_scopes)
                    if not required.issubset(set(granted_scopes)):
                        missing_required = ",".join(sorted(required - set(granted_scopes)))
                        auth_request.status = "failed"
                        auth_request.error = "missing_required_scopes:" + missing_required
                        auth_request.completed_at = now
                        await session.commit()
                        if redirect_url:
                            return RedirectResponse(
                                url=append_query(
                                    redirect_url,
                                    {
                                        "ok": "false",
                                        "error": "missing_required_scopes",
                                        "message": (
                                            "Broadcaster authorization succeeded but required scopes are missing: "
                                            + missing_required
                                        ),
                                    },
                                ),
                                status_code=302,
                            )
                        raise HTTPException(
                            status_code=400,
                            detail=(
                                "Broadcaster authorization succeeded but required scopes are missing: "
                                + missing_required
                            ),
                        )

                    broadcaster_user_id = str(token_info.get("user_id", ""))
                    broadcaster_login = str(token_info.get("login", ""))
                    if not broadcaster_user_id or not broadcaster_login:
                        auth_request.status = "failed"
                        auth_request.error = "missing_broadcaster_identity"
                        auth_request.completed_at = now
                        await session.commit()
                        if redirect_url:
                            return RedirectResponse(
                                url=append_query(
                                    redirect_url,
                                    {
                                        "ok": "false",
                                        "error": "missing_broadcaster_identity",
                                        "message": "Could not resolve broadcaster identity",
                                    },
                                ),
                                status_code=302,
                            )
                        raise HTTPException(status_code=400, detail="Could not resolve broadcaster identity")

                    existing_auth = await session.scalar(
                        select(BroadcasterAuthorization).where(
                            BroadcasterAuthorization.service_account_id == auth_request.service_account_id,
                            BroadcasterAuthorization.bot_account_id == auth_request.bot_account_id,
                            BroadcasterAuthorization.broadcaster_user_id == broadcaster_user_id,
                        )
                    )
                    scopes_csv = ",".join(granted_scopes)
                    if existing_auth:
                        existing_auth.broadcaster_login = broadcaster_login
                        existing_auth.scopes_csv = scopes_csv
                        existing_auth.authorized_at = now
                    else:
                        session.add(
                            BroadcasterAuthorization(
                                service_account_id=auth_request.service_account_id,
                                bot_account_id=auth_request.bot_account_id,
                                broadcaster_user_id=broadcaster_user_id,
                                broadcaster_login=broadcaster_login,
                                scopes_csv=scopes_csv,
                                authorized_at=now,
                            )
                        )
                    auth_request.status = "completed"
                    auth_request.broadcaster_user_id = broadcaster_user_id
                    auth_request.error = None
                    auth_request.completed_at = now
                    await session.commit()
                    if redirect_url:
                        return RedirectResponse(
                            url=append_query(
                                redirect_url,
                                {
                                    "ok": "true",
                                    "message": "Broadcaster authorization completed.",
                                    "service_connected": "true",
                                    "broadcaster_user_id": broadcaster_user_id,
                                    "broadcaster_login": broadcaster_login,
                                    "scopes": ",".join(granted_scopes),
                                },
                            ),
                            status_code=302,
                        )
                    return {
                        "ok": True,
                        "message": "Broadcaster authorization completed.",
                        "service_connected": True,
                        "broadcaster_user_id": broadcaster_user_id,
                        "broadcaster_login": broadcaster_login,
                        "scopes": granted_scopes,
                    }
                user_auth_request = await session.get(ServiceUserAuthRequest, state)
                if user_auth_request:
                    redirect_url = user_auth_request.redirect_url
                    now = datetime.now(UTC)
                    if error:
                        user_auth_request.status = "failed"
                        user_auth_request.error = error
                        user_auth_request.completed_at = now
                        await session.commit()
                        if redirect_url:
                            return RedirectResponse(
                                url=append_query(
                                    redirect_url,
                                    {
                                        "ok": "false",
                                        "auth_type": "service_user",
                                        "state": state,
                                        "error": error,
                                        "message": "Service user authorization failed.",
                                    },
                                ),
                                status_code=302,
                            )
                        return {
                            "ok": False,
                            "auth_type": "service_user",
                            "state": state,
                            "error": error,
                            "message": "Service user authorization failed.",
                        }
                    if not code:
                        user_auth_request.status = "failed"
                        user_auth_request.error = "missing_code"
                        user_auth_request.completed_at = now
                        await session.commit()
                        if redirect_url:
                            return RedirectResponse(
                                url=append_query(
                                    redirect_url,
                                    {
                                        "ok": "false",
                                        "auth_type": "service_user",
                                        "state": state,
                                        "error": "missing_code",
                                        "message": "Missing OAuth code",
                                    },
                                ),
                                status_code=302,
                            )
                        raise HTTPException(status_code=400, detail="Missing OAuth code")

                    try:
                        token = await twitch_client.exchange_code(code)
                        token_info = await twitch_client.validate_user_token(token.access_token)
                        users = await twitch_client.get_users(token.access_token)
                        user = users[0] if users else {}
                    except Exception as exc:
                        user_auth_request.status = "failed"
                        user_auth_request.error = str(exc)
                        user_auth_request.completed_at = now
                        await session.commit()
                        if redirect_url:
                            return RedirectResponse(
                                url=append_query(
                                    redirect_url,
                                    {
                                        "ok": "false",
                                        "auth_type": "service_user",
                                        "state": state,
                                        "error": "oauth_exchange_failed",
                                        "message": f"OAuth exchange failed: {exc}",
                                    },
                                ),
                                status_code=302,
                            )
                        raise HTTPException(status_code=502, detail=f"OAuth exchange failed: {exc}") from exc

                    granted_scopes = sorted(set(token_info.get("scopes", [])))
                    required = set(service_user_auth_scopes)
                    if not required.issubset(set(granted_scopes)):
                        missing_required = ",".join(sorted(required - set(granted_scopes)))
                        user_auth_request.status = "failed"
                        user_auth_request.error = "missing_required_scopes:" + missing_required
                        user_auth_request.completed_at = now
                        await session.commit()
                        if redirect_url:
                            return RedirectResponse(
                                url=append_query(
                                    redirect_url,
                                    {
                                        "ok": "false",
                                        "auth_type": "service_user",
                                        "state": state,
                                        "error": "missing_required_scopes",
                                        "message": (
                                            "Service user authorization succeeded but required scopes are missing: "
                                            + missing_required
                                        ),
                                    },
                                ),
                                status_code=302,
                            )
                        raise HTTPException(
                            status_code=400,
                            detail=(
                                "Service user authorization succeeded but required scopes are missing: "
                                + missing_required
                            ),
                        )

                    twitch_user_id = str(token_info.get("user_id", "") or user.get("id", ""))
                    twitch_login = str(token_info.get("login", "") or user.get("login", ""))
                    if not twitch_user_id or not twitch_login:
                        user_auth_request.status = "failed"
                        user_auth_request.error = "missing_user_identity"
                        user_auth_request.completed_at = now
                        await session.commit()
                        if redirect_url:
                            return RedirectResponse(
                                url=append_query(
                                    redirect_url,
                                    {
                                        "ok": "false",
                                        "auth_type": "service_user",
                                        "state": state,
                                        "error": "missing_user_identity",
                                        "message": "Could not resolve authenticated Twitch user identity",
                                    },
                                ),
                                status_code=302,
                            )
                        raise HTTPException(
                            status_code=400, detail="Could not resolve authenticated Twitch user identity"
                        )

                    user_auth_request.status = "completed"
                    user_auth_request.error = None
                    user_auth_request.twitch_user_id = twitch_user_id
                    user_auth_request.twitch_login = twitch_login
                    user_auth_request.twitch_display_name = str(user.get("display_name", twitch_login))
                    user_auth_request.twitch_email = user.get("email")
                    user_auth_request.access_token = token.access_token
                    user_auth_request.refresh_token = token.refresh_token
                    user_auth_request.token_expires_at = token.expires_at
                    user_auth_request.completed_at = now
                    await session.commit()
                    if redirect_url:
                        return RedirectResponse(
                            url=append_query(
                                redirect_url,
                                {
                                    "ok": "true",
                                    "auth_type": "service_user",
                                    "state": state,
                                    "message": "Service user authorization completed.",
                                    "twitch_user_id": twitch_user_id,
                                    "twitch_login": twitch_login,
                                    "scopes": ",".join(granted_scopes),
                                },
                            ),
                            status_code=302,
                        )
                    return {
                        "ok": True,
                        "auth_type": "service_user",
                        "state": state,
                        "message": "Service user authorization completed.",
                        "twitch_user_id": twitch_user_id,
                        "twitch_login": twitch_login,
                        "scopes": granted_scopes,
                    }

        if state:
            async with session_factory() as session:
                callback = await session.get(OAuthCallback, state)
                if callback is None:
                    callback = OAuthCallback(state=state)
                    session.add(callback)
                callback.code = code
                callback.error = error
                await session.commit()

        if error:
            return {
                "ok": False,
                "error": error,
                "message": "OAuth authorization returned an error.",
            }
        if not code:
            raise HTTPException(status_code=400, detail="Missing OAuth code")
        return {
            "ok": True,
            "message": "OAuth callback received. You can return to CLI and continue setup.",
            "code_received": True,
            "state_received": bool(state),
        }

