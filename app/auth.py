from __future__ import annotations

import secrets
import uuid

from fastapi import HTTPException, status
from passlib.context import CryptContext
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ServiceAccount


pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def generate_client_id() -> str:
    return uuid.uuid4().hex


def generate_client_secret() -> str:
    return secrets.token_urlsafe(48)


def hash_secret(secret: str) -> str:
    return pwd_context.hash(secret)


def verify_secret(secret: str, secret_hash: str) -> bool:
    return pwd_context.verify(secret, secret_hash)


async def authenticate_service(
    session: AsyncSession, client_id: str, client_secret: str
) -> ServiceAccount:
    account = await session.scalar(
        select(ServiceAccount).where(
            ServiceAccount.client_id == client_id, ServiceAccount.enabled.is_(True)
        )
    )
    if not account or not verify_secret(client_secret, account.client_secret_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid service credentials"
        )
    return account
