from __future__ import annotations

from urllib.parse import urlsplit


def normalize_broadcaster_id_or_login(raw: str) -> str:
    """
    Accept either a Twitch user id, a login, or a twitch.tv URL, and normalize to
    a single token (id/login) without surrounding punctuation.
    """
    value = (raw or "").strip()
    if not value:
        return ""
    if value.startswith(("http://", "https://")):
        try:
            split = urlsplit(value)
            host = (split.netloc or "").lower()
            if host.endswith("twitch.tv"):
                path = (split.path or "").strip("/")
                if path:
                    value = path.split("/", 1)[0]
        except Exception:
            pass
    value = value.strip().lstrip("@")
    if "/" in value:
        value = value.split("/", 1)[0]
    if "?" in value:
        value = value.split("?", 1)[0]
    return value.strip()

