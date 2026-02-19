from __future__ import annotations

import json
from urllib.parse import urlparse

import httpx
from prompt_toolkit import PromptSession
import websockets


def env_bool(name: str, default: bool) -> bool:
    import os

    raw = (os.getenv(name, "") or "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "y", "on"}


def normalize_base_url(raw: str) -> str:
    value = (raw or "").strip()
    if not value:
        raise ValueError("Remote API base URL is required")
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Remote API base URL must be an absolute http(s) URL")
    return value.rstrip("/")


def build_ws_events_url(base_url: str, ws_token: str) -> str:
    parsed = urlparse(base_url)
    ws_scheme = "wss" if parsed.scheme == "https" else "ws"
    base_path = parsed.path.rstrip("/")
    ws_path = f"{base_path}/ws/events" if base_path else "/ws/events"
    return f"{ws_scheme}://{parsed.netloc}{ws_path}?ws_token={ws_token}"


async def remote_health_check(client: httpx.AsyncClient) -> None:
    resp = await client.get("/health")
    print(f"HTTP {resp.status_code}: {resp.text[:300]}")


def service_headers(client_id: str, client_secret: str) -> dict[str, str]:
    return {
        "X-Client-Id": client_id,
        "X-Client-Secret": client_secret,
    }


async def remote_list_accessible_bots(
    client: httpx.AsyncClient,
    client_id: str,
    client_secret: str,
) -> None:
    resp = await client.get(
        "/v1/bots/accessible",
        headers=service_headers(client_id, client_secret),
    )
    if resp.status_code >= 300:
        print(f"Request failed: HTTP {resp.status_code} {resp.text[:400]}")
        return
    payload = resp.json()
    print(json.dumps(payload, indent=2, ensure_ascii=False))


async def remote_list_interests(
    client: httpx.AsyncClient,
    client_id: str,
    client_secret: str,
) -> None:
    resp = await client.get(
        "/v1/interests",
        headers=service_headers(client_id, client_secret),
    )
    if resp.status_code >= 300:
        print(f"Request failed: HTTP {resp.status_code} {resp.text[:400]}")
        return
    payload = resp.json()
    print(json.dumps(payload, indent=2, ensure_ascii=False))


async def remote_list_service_subscriptions(
    client: httpx.AsyncClient,
    client_id: str,
    client_secret: str,
) -> None:
    resp = await client.get(
        "/v1/subscriptions",
        headers=service_headers(client_id, client_secret),
    )
    if resp.status_code >= 300:
        print(f"Request failed: HTTP {resp.status_code} {resp.text[:400]}")
        return
    payload = resp.json()
    print(json.dumps(payload, indent=2, ensure_ascii=False))


async def remote_list_subscription_transports(
    client: httpx.AsyncClient,
    client_id: str,
    client_secret: str,
) -> None:
    resp = await client.get(
        "/v1/subscriptions/transports",
        headers=service_headers(client_id, client_secret),
    )
    if resp.status_code >= 300:
        print(f"Request failed: HTTP {resp.status_code} {resp.text[:400]}")
        return
    payload = resp.json()
    print(json.dumps(payload, indent=2, ensure_ascii=False))


async def remote_list_active_subscriptions(
    session: PromptSession,
    client: httpx.AsyncClient,
    client_id: str,
    client_secret: str,
) -> None:
    raw = (await session.prompt_async("Force refresh from Twitch? [y/N]: ")).strip().lower()
    refresh = raw in {"y", "yes"}
    resp = await client.get(
        "/v1/eventsub/subscriptions/active",
        params={"refresh": "true" if refresh else "false"},
        headers=service_headers(client_id, client_secret),
    )
    if resp.status_code >= 300:
        print(f"Request failed: HTTP {resp.status_code} {resp.text[:400]}")
        return
    payload = resp.json()
    print(json.dumps(payload, indent=2, ensure_ascii=False))


async def remote_test_broadcaster_scope_flows(
    session: PromptSession,
    client: httpx.AsyncClient,
    client_id: str,
    client_secret: str,
) -> None:
    bots_resp = await client.get(
        "/v1/bots/accessible",
        headers=service_headers(client_id, client_secret),
    )
    if bots_resp.status_code >= 300:
        print(f"Request failed: HTTP {bots_resp.status_code} {bots_resp.text[:400]}")
        return
    payload = bots_resp.json()
    if isinstance(payload, dict):
        bots_raw = payload.get("bots", [])
    elif isinstance(payload, list):
        bots_raw = payload
    else:
        bots_raw = []
    bots = [bot for bot in bots_raw if isinstance(bot, dict)]
    if not bots:
        print("No accessible bots for this service.")
        return

    print("\nAccessible bots:")
    for idx, bot in enumerate(bots, start=1):
        print(
            f"{idx}) id={bot.get('id')} name={bot.get('name')} "
            f"login={bot.get('twitch_login')} enabled={bot.get('enabled')}"
        )

    raw_choice = (await session.prompt_async("Select bot (number or UUID): ")).strip()
    bot_id = raw_choice
    if raw_choice.isdigit():
        selected_idx = int(raw_choice) - 1
        if selected_idx < 0 or selected_idx >= len(bots):
            print("Invalid bot selection.")
            return
        bot_id = str(bots[selected_idx].get("id", "")).strip()
    if not bot_id:
        print("Bot id is required.")
        return

    redirect_url = (await session.prompt_async("Redirect URL (optional): ")).strip() or None
    raw_event_types = (
        await session.prompt_async(
            "Event types CSV for full scope test (blank = default probe set): "
        )
    ).strip()
    event_types = [x.strip() for x in raw_event_types.split(",") if x.strip()]
    if not event_types:
        event_types = [
            "channel.ad_break.begin",
            "channel.poll.begin",
            "channel.prediction.begin",
            "channel.goal.begin",
            "channel.charity_campaign.start",
            "channel.hype_train.begin",
            "channel.channel_points_custom_reward_redemption.add",
        ]

    minimal_payload: dict[str, object] = {"bot_account_id": bot_id}
    full_payload: dict[str, object] = {"bot_account_id": bot_id, "event_types": event_types}
    if redirect_url:
        minimal_payload["redirect_url"] = redirect_url
        full_payload["redirect_url"] = redirect_url

    minimal_resp = await client.post(
        "/v1/broadcaster-authorizations/start-minimal",
        json=minimal_payload,
        headers=service_headers(client_id, client_secret),
    )
    full_resp = await client.post(
        "/v1/broadcaster-authorizations/start",
        json=full_payload,
        headers=service_headers(client_id, client_secret),
    )

    print("\nMinimal broadcaster authorization scope test:")
    if minimal_resp.status_code >= 300:
        print(f"HTTP {minimal_resp.status_code}: {minimal_resp.text[:500]}")
    else:
        print(json.dumps(minimal_resp.json(), indent=2, ensure_ascii=False))

    print("\nEvent-aware broadcaster authorization scope test:")
    if full_resp.status_code >= 300:
        print(f"HTTP {full_resp.status_code}: {full_resp.text[:500]}")
    else:
        print(json.dumps(full_resp.json(), indent=2, ensure_ascii=False))


async def remote_list_bots_admin(client: httpx.AsyncClient, admin_api_key: str) -> None:
    if not admin_api_key:
        print("CLI_ADMIN_API_KEY is required for this action.")
        return
    resp = await client.get("/v1/bots", headers={"X-Admin-Key": admin_api_key})
    if resp.status_code >= 300:
        print(f"Request failed: HTTP {resp.status_code} {resp.text[:400]}")
        return
    payload = resp.json()
    print(json.dumps(payload, indent=2, ensure_ascii=False))


async def remote_ws_listen_once(
    client: httpx.AsyncClient,
    base_url: str,
    client_id: str,
    client_secret: str,
) -> None:
    resp = await client.post("/v1/ws-token", headers=service_headers(client_id, client_secret))
    if resp.status_code >= 300:
        print(f"Token request failed: HTTP {resp.status_code} {resp.text[:400]}")
        return
    data = resp.json()
    ws_token = str(data.get("ws_token", "")).strip()
    if not ws_token:
        print("Token response missing ws_token.")
        return
    ws_url = build_ws_events_url(base_url, ws_token)
    print(f"Connecting websocket: {ws_url}")
    try:
        async with websockets.connect(ws_url, max_size=4 * 1024 * 1024) as ws:
            print("Connected. Waiting for events (Ctrl+C to stop).")
            while True:
                raw = await ws.recv()
                print(raw)
    except KeyboardInterrupt:
        print("\nStopped websocket listener.")
    except Exception as exc:
        print(f"Websocket failed: {exc}")


async def remote_menu_loop(
    api_base_url: str,
    service_client_id: str,
    service_client_secret: str,
    admin_api_key: str,
    verify_tls: bool,
) -> None:
    session = PromptSession()
    timeout = httpx.Timeout(20.0)
    async with httpx.AsyncClient(base_url=api_base_url, timeout=timeout, verify=verify_tls) as client:
        while True:
            print(
                "\nRemote Console\n"
                f"target={api_base_url}\n"
                "1) Health check\n"
                "2) List accessible bots (service auth)\n"
                "3) List bots (admin auth)\n"
                "4) List interests (service auth)\n"
                "5) List subscriptions (service auth)\n"
                "6) List subscription transports (service auth)\n"
                "7) List active subscriptions (service auth)\n"
                "8) Test broadcaster scopes (minimal + event-aware)\n"
                "9) Listen on /ws/events (ws_token flow)\n"
                "10) Exit\n"
            )
            choice = (await session.prompt_async("Select option: ")).strip()
            if choice == "1":
                await remote_health_check(client)
                continue
            if choice == "2":
                if not service_client_id or not service_client_secret:
                    print("CLI_SERVICE_CLIENT_ID and CLI_SERVICE_CLIENT_SECRET are required.")
                    continue
                await remote_list_accessible_bots(client, service_client_id, service_client_secret)
                continue
            if choice == "3":
                await remote_list_bots_admin(client, admin_api_key)
                continue
            if choice == "4":
                if not service_client_id or not service_client_secret:
                    print("CLI_SERVICE_CLIENT_ID and CLI_SERVICE_CLIENT_SECRET are required.")
                    continue
                await remote_list_interests(client, service_client_id, service_client_secret)
                continue
            if choice == "5":
                if not service_client_id or not service_client_secret:
                    print("CLI_SERVICE_CLIENT_ID and CLI_SERVICE_CLIENT_SECRET are required.")
                    continue
                await remote_list_service_subscriptions(client, service_client_id, service_client_secret)
                continue
            if choice == "6":
                if not service_client_id or not service_client_secret:
                    print("CLI_SERVICE_CLIENT_ID and CLI_SERVICE_CLIENT_SECRET are required.")
                    continue
                await remote_list_subscription_transports(client, service_client_id, service_client_secret)
                continue
            if choice == "7":
                if not service_client_id or not service_client_secret:
                    print("CLI_SERVICE_CLIENT_ID and CLI_SERVICE_CLIENT_SECRET are required.")
                    continue
                await remote_list_active_subscriptions(
                    session,
                    client,
                    service_client_id,
                    service_client_secret,
                )
                continue
            if choice == "8":
                if not service_client_id or not service_client_secret:
                    print("CLI_SERVICE_CLIENT_ID and CLI_SERVICE_CLIENT_SECRET are required.")
                    continue
                await remote_test_broadcaster_scope_flows(
                    session,
                    client,
                    service_client_id,
                    service_client_secret,
                )
                continue
            if choice == "9":
                if not service_client_id or not service_client_secret:
                    print("CLI_SERVICE_CLIENT_ID and CLI_SERVICE_CLIENT_SECRET are required.")
                    continue
                await remote_ws_listen_once(client, api_base_url, service_client_id, service_client_secret)
                continue
            if choice == "10":
                return
            print("Invalid option.")

