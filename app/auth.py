from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import uuid

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ServiceAccount


PBKDF2_PREFIX = "pbkdf2_sha256"
PBKDF2_ITERATIONS = 260_000


def generate_client_id() -> str:
    return uuid.uuid4().hex


def generate_client_secret() -> str:
    return secrets.token_urlsafe(48)


def hash_secret(secret: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        secret.encode("utf-8"),
        salt,
        PBKDF2_ITERATIONS,
    )
    salt_b64 = base64.urlsafe_b64encode(salt).decode("ascii")
    digest_b64 = base64.urlsafe_b64encode(digest).decode("ascii")
    return f"{PBKDF2_PREFIX}${PBKDF2_ITERATIONS}${salt_b64}${digest_b64}"


def verify_secret(secret: str, secret_hash: str) -> bool:
    if secret_hash.startswith(f"{PBKDF2_PREFIX}$"):
        try:
            _, iter_s, salt_b64, digest_b64 = secret_hash.split("$", 3)
            iterations = int(iter_s)
            salt = base64.urlsafe_b64decode(salt_b64.encode("ascii"))
            expected = base64.urlsafe_b64decode(digest_b64.encode("ascii"))
        except Exception:
            return False

        actual = hashlib.pbkdf2_hmac(
            "sha256",
            secret.encode("utf-8"),
            salt,
            iterations,
        )
        return hmac.compare_digest(actual, expected)

    # Backward compatibility: verify legacy passlib hashes if present.
    try:
        from passlib.context import CryptContext

        legacy_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
        return legacy_context.verify(secret, secret_hash)
    except Exception:
        return False


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
