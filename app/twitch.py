from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import httpx

TOKEN_URL = "https://id.twitch.tv/oauth2/token"
VALIDATE_URL = "https://id.twitch.tv/oauth2/validate"
HELIX_BASE = "https://api.twitch.tv/helix"


@dataclass(slots=True)
class OAuthToken:
    access_token: str
    refresh_token: str
    expires_at: datetime


class TwitchApiError(RuntimeError):
    pass


class TwitchClient:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        scopes: str,
        eventsub_ws_url: str = "wss://eventsub.wss.twitch.tv/ws",
    ):
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self.scopes = scopes
        self.eventsub_ws_url = eventsub_ws_url
        self._app_token: str | None = None
        self._app_token_expiry: datetime | None = None

    def build_authorize_url(self, state: str) -> str:
        return self.build_authorize_url_with_scopes(state=state, scopes=self.scopes)

    def build_authorize_url_with_scopes(self, state: str, scopes: str, force_verify: bool = True) -> str:
        params = {
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "response_type": "code",
            "scope": scopes,
            "state": state,
            "force_verify": "true" if force_verify else "false",
        }
        return f"https://id.twitch.tv/oauth2/authorize?{urlencode(params)}"

    async def exchange_code(self, code: str) -> OAuthToken:
        payload = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": self.redirect_uri,
        }
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(TOKEN_URL, params=payload)
        if resp.status_code >= 300:
            raise TwitchApiError(f"Failed to exchange auth code: {resp.text}")
        data = resp.json()
        return OAuthToken(
            access_token=data["access_token"],
            refresh_token=data["refresh_token"],
            expires_at=datetime.now(UTC) + timedelta(seconds=int(data["expires_in"])),
        )

    async def refresh_token(self, refresh_token: str) -> OAuthToken:
        payload = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
        }
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(TOKEN_URL, params=payload)
        if resp.status_code >= 300:
            raise TwitchApiError(f"Failed to refresh token: {resp.text}")
        data = resp.json()
        return OAuthToken(
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token", refresh_token),
            expires_at=datetime.now(UTC) + timedelta(seconds=int(data["expires_in"])),
        )

    async def get_users(self, access_token: str) -> list[dict[str, Any]]:
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Client-Id": self.client_id,
        }
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(f"{HELIX_BASE}/users", headers=headers)
        if resp.status_code >= 300:
            raise TwitchApiError(f"Failed users lookup: {resp.text}")
        return resp.json().get("data", [])

    async def get_users_by_query(
        self,
        access_token: str,
        user_ids: list[str] | None = None,
        logins: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        user_ids = user_ids or []
        logins = logins or []
        if not user_ids and not logins:
            return []
        headers = {"Authorization": f"Bearer {access_token}", "Client-Id": self.client_id}
        params: list[tuple[str, str]] = []
        for uid in user_ids:
            params.append(("id", uid))
        for login in logins:
            params.append(("login", login))
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(f"{HELIX_BASE}/users", headers=headers, params=params)
        if resp.status_code >= 300:
            raise TwitchApiError(f"Failed users lookup by query: {resp.text}")
        return resp.json().get("data", [])

    async def get_streams_by_user_ids(self, access_token: str, user_ids: list[str]) -> list[dict[str, Any]]:
        if not user_ids:
            return []
        headers = {"Authorization": f"Bearer {access_token}", "Client-Id": self.client_id}
        params = [("user_id", uid) for uid in user_ids]
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(f"{HELIX_BASE}/streams", headers=headers, params=params)
        if resp.status_code >= 300:
            raise TwitchApiError(f"Failed streams lookup: {resp.text}")
        return resp.json().get("data", [])

    async def get_user_by_login_app(self, login: str) -> dict[str, Any] | None:
        token = await self.app_access_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Client-Id": self.client_id,
        }
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(f"{HELIX_BASE}/users", headers=headers, params={"login": login})
        if resp.status_code >= 300:
            raise TwitchApiError(f"Failed users lookup by login: {resp.text}")
        users = resp.json().get("data", [])
        return users[0] if users else None

    async def get_user_by_id_app(self, user_id: str) -> dict[str, Any] | None:
        token = await self.app_access_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Client-Id": self.client_id,
        }
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(f"{HELIX_BASE}/users", headers=headers, params={"id": user_id})
        if resp.status_code >= 300:
            raise TwitchApiError(f"Failed users lookup by id: {resp.text}")
        users = resp.json().get("data", [])
        return users[0] if users else None

    async def validate_user_token(self, access_token: str) -> dict[str, Any]:
        headers = {"Authorization": f"OAuth {access_token}"}
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(VALIDATE_URL, headers=headers)
        if resp.status_code >= 300:
            raise TwitchApiError(f"Failed token validation: {resp.text}")
        return resp.json()

    async def app_access_token(self) -> str:
        if self._app_token and self._app_token_expiry and datetime.now(UTC) < self._app_token_expiry:
            return self._app_token

        payload = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "grant_type": "client_credentials",
        }
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(TOKEN_URL, params=payload)
        if resp.status_code >= 300:
            raise TwitchApiError(f"Failed to get app token: {resp.text}")
        data = resp.json()
        self._app_token = data["access_token"]
        self._app_token_expiry = datetime.now(UTC) + timedelta(seconds=int(data["expires_in"]) - 60)
        return self._app_token

    async def list_eventsub_subscriptions(self, access_token: str | None = None) -> list[dict[str, Any]]:
        token = access_token or await self.app_access_token()
        headers = {"Authorization": f"Bearer {token}", "Client-Id": self.client_id}
        cursor = None
        out: list[dict[str, Any]] = []
        async with httpx.AsyncClient(timeout=20) as client:
            while True:
                params = {"after": cursor} if cursor else None
                resp = await client.get(f"{HELIX_BASE}/eventsub/subscriptions", headers=headers, params=params)
                if resp.status_code >= 300:
                    raise TwitchApiError(f"Failed listing subscriptions: {resp.text}")
                payload = resp.json()
                out.extend(payload.get("data", []))
                cursor = payload.get("pagination", {}).get("cursor")
                if not cursor:
                    break
        return out

    async def create_eventsub_subscription(
        self,
        event_type: str,
        version: str,
        condition: dict[str, str],
        transport: dict[str, str],
        access_token: str | None = None,
    ) -> dict[str, Any]:
        token = access_token or await self.app_access_token()
        headers = {"Authorization": f"Bearer {token}", "Client-Id": self.client_id}
        body = {
            "type": event_type,
            "version": version,
            "condition": condition,
            "transport": transport,
        }
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(f"{HELIX_BASE}/eventsub/subscriptions", headers=headers, json=body)
        if resp.status_code == 409:
            # Twitch returns 409 Conflict with message "subscription already exists" when a subscription
            # with the same type/condition/transport already exists. Treat as idempotent create.
            def _cond_match(existing: dict[str, Any], desired: dict[str, str]) -> bool:
                for k, v in desired.items():
                    if str(existing.get(k, "")) != str(v):
                        return False
                return True

            def _transport_match(existing: dict[str, Any], desired: dict[str, str]) -> bool:
                if str(existing.get("method", "")) != str(desired.get("method", "")):
                    return False
                method = str(desired.get("method", ""))
                if method == "websocket":
                    # If we requested a specific session_id, require exact match.
                    desired_session = str(desired.get("session_id", ""))
                    if desired_session and str(existing.get("session_id", "")) != desired_session:
                        return False
                if method == "webhook":
                    desired_callback = str(desired.get("callback", ""))
                    if desired_callback and str(existing.get("callback", "")) != desired_callback:
                        return False
                return True

            try:
                subs = await self.list_eventsub_subscriptions(access_token=token)
                for sub in subs:
                    if str(sub.get("type", "")) != str(event_type):
                        continue
                    # version may be absent in some payloads; only enforce when present.
                    sub_version = sub.get("version")
                    if sub_version is not None and str(sub_version) != str(version):
                        continue
                    if not _cond_match(sub.get("condition", {}) or {}, condition):
                        continue
                    if not _transport_match(sub.get("transport", {}) or {}, transport):
                        continue
                    return sub
            except Exception:
                # Fall through to the normal error below.
                pass
            raise TwitchApiError(f"Failed creating subscription (409 already exists): {resp.text}")
        if resp.status_code >= 300:
            raise TwitchApiError(f"Failed creating subscription: {resp.text}")
        data = resp.json().get("data", [])
        if not data:
            raise TwitchApiError("Empty create subscription response")
        return data[0]

    async def delete_eventsub_subscription(self, subscription_id: str, access_token: str | None = None) -> None:
        token = access_token or await self.app_access_token()
        headers = {"Authorization": f"Bearer {token}", "Client-Id": self.client_id}
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.delete(
                f"{HELIX_BASE}/eventsub/subscriptions",
                headers=headers,
                params={"id": subscription_id},
            )
        if resp.status_code >= 300:
            raise TwitchApiError(f"Failed deleting subscription: {resp.text}")

    async def send_chat_message(
        self,
        access_token: str,
        broadcaster_id: str,
        sender_id: str,
        message: str,
        reply_parent_message_id: str | None = None,
    ) -> dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Client-Id": self.client_id,
            "Content-Type": "application/json",
        }
        body: dict[str, Any] = {
            "broadcaster_id": broadcaster_id,
            "sender_id": sender_id,
            "message": message,
        }
        if reply_parent_message_id:
            body["reply_parent_message_id"] = reply_parent_message_id
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(f"{HELIX_BASE}/chat/messages", headers=headers, json=body)
        if resp.status_code >= 300:
            raise TwitchApiError(f"Failed sending chat message: {resp.text}")
        data = resp.json().get("data", [])
        if not data:
            raise TwitchApiError("Empty send chat message response")
        return data[0]

    async def create_clip(
        self,
        access_token: str,
        broadcaster_id: str,
        title: str,
        duration: float,
        has_delay: bool = False,
    ) -> dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Client-Id": self.client_id,
        }
        params: dict[str, Any] = {
            "broadcaster_id": broadcaster_id,
            "title": title,
            "duration": duration,
            "has_delay": "true" if has_delay else "false",
        }
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(f"{HELIX_BASE}/clips", headers=headers, params=params)
        if resp.status_code >= 300:
            raise TwitchApiError(f"Failed creating clip: {resp.text}")
        data = resp.json().get("data", [])
        if not data:
            raise TwitchApiError("Empty create clip response")
        return data[0]

    async def get_clips(
        self,
        access_token: str,
        clip_ids: list[str],
    ) -> list[dict[str, Any]]:
        if not clip_ids:
            return []
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Client-Id": self.client_id,
        }
        params = [("id", clip_id) for clip_id in clip_ids]
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(f"{HELIX_BASE}/clips", headers=headers, params=params)
        if resp.status_code >= 300:
            raise TwitchApiError(f"Failed getting clips: {resp.text}")
        return resp.json().get("data", [])
