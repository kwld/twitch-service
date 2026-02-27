import uuid

import pytest
from pydantic import ValidationError

from app.schemas import (
    CreateClipRequest,
    CreateInterestRequest,
    ResolveEventSubScopesRequest,
    SendChatMessageRequest,
)


def test_create_interest_request_accepts_valid_webhook_payload():
    model = CreateInterestRequest(
        bot_account_id=uuid.uuid4(),
        event_type="stream.online",
        broadcaster_user_id="12345",
        transport="webhook",
        webhook_url="https://example.com/hook",
    )
    assert model.transport == "webhook"
    assert str(model.webhook_url) == "https://example.com/hook"


def test_create_interest_request_rejects_invalid_event_type_length():
    with pytest.raises(ValidationError):
        CreateInterestRequest(
            bot_account_id=uuid.uuid4(),
            event_type="ab",
            broadcaster_user_id="12345",
        )


def test_send_chat_message_request_bounds():
    ok = SendChatMessageRequest(
        bot_account_id=uuid.uuid4(),
        broadcaster_user_id="12345",
        message="hello",
    )
    assert ok.auth_mode == "auto"

    with pytest.raises(ValidationError):
        SendChatMessageRequest(
            bot_account_id=uuid.uuid4(),
            broadcaster_user_id="12345",
            message="",
        )


def test_create_clip_request_duration_bounds():
    CreateClipRequest(
        bot_account_id=uuid.uuid4(),
        broadcaster_user_id="12345",
        title="Clip",
        duration=5.0,
    )
    CreateClipRequest(
        bot_account_id=uuid.uuid4(),
        broadcaster_user_id="12345",
        title="Clip",
        duration=60.0,
    )

    with pytest.raises(ValidationError):
        CreateClipRequest(
            bot_account_id=uuid.uuid4(),
            broadcaster_user_id="12345",
            title="Clip",
            duration=4.9,
        )

    with pytest.raises(ValidationError):
        CreateClipRequest(
            bot_account_id=uuid.uuid4(),
            broadcaster_user_id="12345",
            title="Clip",
            duration=60.1,
        )


def test_resolve_scopes_request_has_independent_default_list_instances():
    r1 = ResolveEventSubScopesRequest()
    r2 = ResolveEventSubScopesRequest()

    r1.event_types.append("stream.online")
    assert r2.event_types == []
