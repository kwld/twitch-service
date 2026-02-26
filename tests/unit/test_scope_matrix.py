from __future__ import annotations

import pytest

from app.eventsub_catalog import recommended_broadcaster_scopes, required_scope_any_of_groups


@pytest.mark.unit
def test_required_scope_groups_for_known_event_type() -> None:
    groups = required_scope_any_of_groups("channel.poll.begin")
    assert groups
    assert {"channel:read:polls", "channel:manage:polls"} in groups


@pytest.mark.unit
def test_recommended_scope_prefers_read_scope_when_available() -> None:
    scopes = recommended_broadcaster_scopes("channel.prediction.begin")
    assert "channel:read:predictions" in scopes
    assert "channel:manage:predictions" not in scopes


@pytest.mark.unit
def test_unknown_event_type_has_no_scope_requirements() -> None:
    assert required_scope_any_of_groups("unknown.event.type") == []
    assert recommended_broadcaster_scopes("unknown.event.type") == set()
