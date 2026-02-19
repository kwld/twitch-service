from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable

from fastapi import Depends, FastAPI, HTTPException
from sqlalchemy import select

from app.auth import generate_client_id, generate_client_secret, hash_secret
from app.models import BotAccount, ServiceAccount


def register_admin_routes(
    app: FastAPI,
    *,
    session_factory,
    require_admin,
    service_auth,
    service_allowed_bot_ids: Callable[[object, uuid.UUID], Awaitable[set[uuid.UUID]]],
) -> None:
    @app.get("/v1/bots")
    async def list_bots(_: None = Depends(require_admin)):
        async with session_factory() as session:
            bots = list((await session.scalars(select(BotAccount))).all())
        return [
            {
                "id": str(bot.id),
                "name": bot.name,
                "twitch_user_id": bot.twitch_user_id,
                "twitch_login": bot.twitch_login,
                "enabled": bot.enabled,
                "token_expires_at": bot.token_expires_at.isoformat(),
            }
            for bot in bots
        ]

    @app.get("/v1/bots/accessible")
    async def list_accessible_bots(service: ServiceAccount = Depends(service_auth)):
        async with session_factory() as session:
            allowed_ids = await service_allowed_bot_ids(session, service.id)
            if allowed_ids:
                bots = list(
                    (
                        await session.scalars(
                            select(BotAccount).where(
                                BotAccount.id.in_(allowed_ids),
                                BotAccount.enabled.is_(True),
                            )
                        )
                    ).all()
                )
                access_mode = "restricted"
            else:
                bots = list((await session.scalars(select(BotAccount).where(BotAccount.enabled.is_(True)))).all())
                access_mode = "all"
        return {
            "access_mode": access_mode,
            "bots": [
                {
                    "id": str(bot.id),
                    "name": bot.name,
                    "twitch_user_id": bot.twitch_user_id,
                    "twitch_login": bot.twitch_login,
                    "enabled": bot.enabled,
                }
                for bot in bots
            ],
        }

    @app.post("/v1/admin/service-accounts")
    async def create_service_account(
        name: str,
        _: None = Depends(require_admin),
    ):
        client_id = generate_client_id()
        client_secret = generate_client_secret()
        async with session_factory() as session:
            account = ServiceAccount(
                name=name,
                client_id=client_id,
                client_secret_hash=hash_secret(client_secret),
            )
            session.add(account)
            await session.commit()
        return {"name": name, "client_id": client_id, "client_secret": client_secret}

    @app.get("/v1/admin/service-accounts")
    async def list_service_accounts(_: None = Depends(require_admin)):
        async with session_factory() as session:
            accounts = list((await session.scalars(select(ServiceAccount))).all())
        return [
            {
                "name": acc.name,
                "client_id": acc.client_id,
                "enabled": acc.enabled,
                "created_at": acc.created_at.isoformat(),
            }
            for acc in accounts
        ]

    @app.post("/v1/admin/service-accounts/{client_id}/regenerate")
    async def regenerate_service_secret(client_id: str, _: None = Depends(require_admin)):
        new_secret = generate_client_secret()
        async with session_factory() as session:
            account = await session.scalar(select(ServiceAccount).where(ServiceAccount.client_id == client_id))
            if not account:
                raise HTTPException(status_code=404, detail="Service account not found")
            account.client_secret_hash = hash_secret(new_secret)
            await session.commit()
        return {"client_id": client_id, "client_secret": new_secret}
