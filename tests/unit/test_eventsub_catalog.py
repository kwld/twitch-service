from app.eventsub_catalog import (
    best_transport_for_service,
    preferred_eventsub_version,
    recommended_bot_scopes,
    recommended_broadcaster_scopes,
    required_scope_any_of_groups,
    requires_condition_user_id,
    supported_twitch_transports,
)


def test_supported_twitch_transports_webhook_only_cases():
    assert supported_twitch_transports("user.authorization.revoke") == ["webhook"]
    assert supported_twitch_transports("drop.entitlement.grant") == ["webhook"]


def test_best_transport_prefers_chat_websocket_even_with_webhook_available():
    transport, reason = best_transport_for_service("channel.chat.message", webhook_available=True)
    assert transport == "websocket"
    assert "Chat events prefer WebSocket" in reason


def test_best_transport_uses_webhook_when_available_for_non_chat():
    transport, _ = best_transport_for_service("stream.online", webhook_available=True)
    assert transport == "webhook"


def test_best_transport_falls_back_to_websocket_when_no_webhook():
    transport, _ = best_transport_for_service("stream.online", webhook_available=False)
    assert transport == "websocket"


def test_preferred_eventsub_version_picks_latest_numeric_and_fallback():
    assert preferred_eventsub_version("automod.message.hold") == "2"
    assert preferred_eventsub_version("channel.guest_star_session.begin") == "1"


def test_requires_condition_user_id_for_chat_types_only():
    assert requires_condition_user_id("channel.chat.message")
    assert requires_condition_user_id("channel.chat_settings.update")
    assert not requires_condition_user_id("stream.online")


def test_required_scope_groups_unknown_event_returns_empty():
    assert required_scope_any_of_groups("unknown.event") == []


def test_recommended_scope_selection_for_chat_message():
    assert recommended_broadcaster_scopes("channel.chat.message") == {"channel:bot"}
    assert recommended_bot_scopes("channel.chat.message") == {"user:read:chat", "user:bot"}


def test_recommended_scope_selection_prefers_read_scope_order():
    assert recommended_broadcaster_scopes("channel.poll.begin") == {"channel:read:polls"}
    assert recommended_bot_scopes("channel.poll.begin") == set()
