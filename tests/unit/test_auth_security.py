import re

from app.auth import (
    PBKDF2_PREFIX,
    generate_client_id,
    generate_client_secret,
    hash_secret,
    verify_secret,
)


def test_generate_client_id_is_hex_uuid_without_dashes():
    client_id = generate_client_id()
    assert re.fullmatch(r"[0-9a-f]{32}", client_id)


def test_generate_client_secret_has_entropy_and_length():
    s1 = generate_client_secret()
    s2 = generate_client_secret()
    assert len(s1) >= 32
    assert len(s2) >= 32
    assert s1 != s2


def test_hash_secret_and_verify_success_and_failure():
    secret = "super-secret-value"
    hashed = hash_secret(secret)

    assert hashed.startswith(f"{PBKDF2_PREFIX}$")
    assert verify_secret(secret, hashed)
    assert not verify_secret("wrong-secret", hashed)


def test_verify_secret_rejects_malformed_pbkdf2_hash():
    malformed = f"{PBKDF2_PREFIX}$not-an-int$salt$digest"
    assert not verify_secret("anything", malformed)


def test_verify_secret_rejects_unknown_hash_when_legacy_backend_fails(monkeypatch):
    def fake_import(*_args, **_kwargs):
        raise RuntimeError("backend unavailable")

    monkeypatch.setattr("builtins.__import__", fake_import)
    assert not verify_secret("secret", "bcrypt$2b$malformed")
