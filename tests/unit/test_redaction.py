from app.core.redaction import is_sensitive_key, mask_secret, redact_payload


def test_is_sensitive_key_detects_common_patterns():
    assert is_sensitive_key("Authorization")
    assert is_sensitive_key("x-client-secret")
    assert is_sensitive_key("ws_token")
    assert not is_sensitive_key("display_name")


def test_mask_secret_behavior():
    assert mask_secret("") == "***"
    assert mask_secret("abc") == "***"
    assert mask_secret("abcdefgh") == "***efgh"


def test_redact_payload_nested_structures():
    payload = {
        "token": "abcdef123456",
        "profile": {
            "name": "alice",
            "api_key": "xyz98765",
        },
        "items": [
            {"password": "secret-pass"},
            {"ok": True},
        ],
    }

    redacted = redact_payload(payload)

    assert redacted["token"] == "***3456"
    assert redacted["profile"]["name"] == "alice"
    assert redacted["profile"]["api_key"] == "***8765"
    assert redacted["items"][0]["password"] == "***pass"
    assert redacted["items"][1]["ok"] is True
