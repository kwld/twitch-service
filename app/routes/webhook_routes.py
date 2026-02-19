from __future__ import annotations

import asyncio
from typing import Callable

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse, Response


def register_webhook_routes(
    app: FastAPI,
    *,
    eventsub_manager,
    verify_twitch_signature: Callable[[Request, bytes], bool],
    is_new_eventsub_message_id: Callable[[str], object],
) -> None:
    @app.post("/webhooks/twitch/eventsub")
    async def twitch_eventsub_webhook(request: Request):
        raw_body = await request.body()
        if not verify_twitch_signature(request, raw_body):
            raise HTTPException(status_code=403, detail="Invalid Twitch signature")
        message_id = request.headers.get("Twitch-Eventsub-Message-Id", "")
        message_type = request.headers.get("Twitch-Eventsub-Message-Type", "").lower()
        payload = await request.json()
        if not await is_new_eventsub_message_id(message_id):
            if message_type == "webhook_callback_verification":
                challenge = payload.get("challenge", "")
                return PlainTextResponse(content=challenge, status_code=200)
            return Response(status_code=204)

        if message_type == "webhook_callback_verification":
            challenge = payload.get("challenge", "")
            return PlainTextResponse(content=challenge, status_code=200)

        if message_type == "notification":
            asyncio.create_task(
                eventsub_manager.handle_webhook_notification(
                    payload, message_id
                )
            )
            return Response(status_code=204)

        if message_type == "revocation":
            asyncio.create_task(eventsub_manager.handle_webhook_revocation(payload))
            return Response(status_code=204)

        return Response(status_code=204)

