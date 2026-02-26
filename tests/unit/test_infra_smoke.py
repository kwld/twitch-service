from __future__ import annotations

import pytest


@pytest.mark.unit
def test_auth_header_fixtures(service_auth_headers: dict[str, str], admin_auth_headers: dict[str, str]) -> None:
    assert "X-Client-Id" in service_auth_headers
    assert "X-Client-Secret" in service_auth_headers
    assert "X-Admin-Key" in admin_auth_headers
