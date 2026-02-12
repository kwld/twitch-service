from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import httpx

TOKEN_URL = "https://id.twitch.tv/oauth2/token"
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
        params = {
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "response_type": "code",
            "scope": self.scopes,
            "state": state,
            "force_verify": "true",
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

    async def list_eventsub_subscriptions(self) -> list[dict[str, Any]]:
        token = await self.app_access_token()
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
    ) -> dict[str, Any]:
        token = await self.app_access_token()
        headers = {"Authorization": f"Bearer {token}", "Client-Id": self.client_id}
        body = {
            "type": event_type,
            "version": version,
            "condition": condition,
            "transport": transport,
        }
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(f"{HELIX_BASE}/eventsub/subscriptions", headers=headers, json=body)
        if resp.status_code >= 300:
            raise TwitchApiError(f"Failed creating subscription: {resp.text}")
        data = resp.json().get("data", [])
        if not data:
            raise TwitchApiError("Empty create subscription response")
        return data[0]

    async def delete_eventsub_subscription(self, subscription_id: str) -> None:
        token = await self.app_access_token()
        headers = {"Authorization": f"Bearer {token}", "Client-Id": self.client_id}
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.delete(
                f"{HELIX_BASE}/eventsub/subscriptions",
                headers=headers,
                params={"id": subscription_id},
            )
        if resp.status_code >= 300:
            raise TwitchApiError(f"Failed deleting subscription: {resp.text}")
