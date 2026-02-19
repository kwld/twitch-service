from __future__ import annotations

from fastapi import FastAPI

from app.routes.oauth_routes import register_oauth_routes
from app.routes.webhook_routes import register_webhook_routes


def register_system_routes(
    app: FastAPI,
    *,
    session_factory,
    twitch_client,
    eventsub_manager,
    append_query,
    verify_twitch_signature,
    is_new_eventsub_message_id,
    broadcaster_auth_scopes: tuple[str, ...],
    service_user_auth_scopes: tuple[str, ...],
) -> None:
    @app.get("/health")
    async def health():
        return {"ok": True}

    register_oauth_routes(
        app,
        session_factory=session_factory,
        twitch_client=twitch_client,
        append_query=append_query,
        broadcaster_auth_scopes=broadcaster_auth_scopes,
        service_user_auth_scopes=service_user_auth_scopes,
    )
    register_webhook_routes(
        app,
        eventsub_manager=eventsub_manager,
        verify_twitch_signature=verify_twitch_signature,
        is_new_eventsub_message_id=is_new_eventsub_message_id,
    )

