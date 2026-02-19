from __future__ import annotations


def is_sensitive_key(key: str) -> bool:
    normalized = key.strip().lower().replace("-", "_")
    return any(
        token in normalized
        for token in (
            "secret",
            "token",
            "authorization",
            "api_key",
            "password",
            "client_secret",
            "x_client_secret",
            "ws_token",
        )
    )


def mask_secret(value: object) -> str:
    raw = str(value)
    if not raw or len(raw) <= 4:
        return "***"
    return "***" + raw[-4:]


def redact_payload(payload: object) -> object:
    if isinstance(payload, dict):
        out: dict[str, object] = {}
        for key, value in payload.items():
            if is_sensitive_key(str(key)):
                out[str(key)] = mask_secret(value)
            else:
                out[str(key)] = redact_payload(value)
        return out
    if isinstance(payload, list):
        return [redact_payload(item) for item in payload]
    return payload

