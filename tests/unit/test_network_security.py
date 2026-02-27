import ipaddress
import socket

import pytest
from fastapi import HTTPException

from app.core.network_security import (
    WebhookTargetValidator,
    host_matches_allowlist,
    is_ip_allowed,
    is_public_ip_address,
    parse_allowed_ip_networks,
    parse_webhook_target_allowlist,
    resolve_client_ip,
)


def test_parse_allowed_ip_networks_accepts_hosts_and_cidrs():
    networks = parse_allowed_ip_networks("10.0.0.1, 192.168.0.0/24,2001:db8::1")
    assert len(networks) == 3
    assert any(str(n) == "10.0.0.1/32" for n in networks)
    assert any(str(n) == "192.168.0.0/24" for n in networks)
    assert any(str(n) == "2001:db8::1/128" for n in networks)


def test_parse_allowed_ip_networks_rejects_invalid():
    with pytest.raises(RuntimeError, match="Invalid APP_ALLOWED_IPS entry"):
        parse_allowed_ip_networks("bad-ip")


def test_resolve_client_ip_prefers_xff_when_trusted():
    assert resolve_client_ip("1.1.1.1", "2.2.2.2,3.3.3.3", trust_x_forwarded_for=True) == "2.2.2.2"
    assert resolve_client_ip("1.1.1.1", "2.2.2.2", trust_x_forwarded_for=False) == "1.1.1.1"


def test_is_ip_allowed_basic_paths():
    networks = parse_allowed_ip_networks("10.0.0.0/24")
    assert is_ip_allowed("10.0.0.42", networks)
    assert not is_ip_allowed("10.0.1.1", networks)
    assert not is_ip_allowed("not-an-ip", networks)
    assert not is_ip_allowed(None, networks)
    assert is_ip_allowed(None, [])


def test_parse_webhook_target_allowlist_validation():
    assert parse_webhook_target_allowlist("example.com, .api.example.com") == ["example.com", "api.example.com"]
    with pytest.raises(RuntimeError, match="Use hostnames only"):
        parse_webhook_target_allowlist("https://example.com")


def test_host_matches_allowlist_exact_and_subdomain():
    allowlist = ["example.com"]
    assert host_matches_allowlist("example.com", allowlist)
    assert host_matches_allowlist("a.b.example.com", allowlist)
    assert not host_matches_allowlist("evil-example.com", allowlist)


def test_is_public_ip_address():
    assert is_public_ip_address(ipaddress.ip_address("8.8.8.8"))
    assert not is_public_ip_address(ipaddress.ip_address("10.0.0.1"))


@pytest.mark.asyncio
async def test_validator_rejects_bad_scheme():
    validator = WebhookTargetValidator(allowlist=[], block_private_targets=True)
    with pytest.raises(HTTPException, match="must use http or https"):
        await validator.validate("ftp://example.com")


@pytest.mark.asyncio
async def test_validator_rejects_userinfo():
    validator = WebhookTargetValidator(allowlist=[], block_private_targets=True)
    with pytest.raises(HTTPException, match="must not contain userinfo"):
        await validator.validate("https://user:pass@example.com")


@pytest.mark.asyncio
async def test_validator_rejects_missing_host():
    validator = WebhookTargetValidator(allowlist=[], block_private_targets=True)
    with pytest.raises(HTTPException, match="host is required"):
        await validator.validate("https:///only-path")


@pytest.mark.asyncio
async def test_validator_allowlist_rejection():
    validator = WebhookTargetValidator(allowlist=["allowed.example"], block_private_targets=True)
    with pytest.raises(HTTPException, match="host is not allowed"):
        await validator.validate("https://denied.example/hook")


@pytest.mark.asyncio
async def test_validator_skips_private_checks_when_disabled():
    validator = WebhookTargetValidator(allowlist=[], block_private_targets=False)
    await validator.validate("https://127.0.0.1/hook")


@pytest.mark.asyncio
async def test_validator_rejects_private_ip_literal():
    validator = WebhookTargetValidator(allowlist=[], block_private_targets=True)
    with pytest.raises(HTTPException, match="target IP must be public"):
        await validator.validate("https://127.0.0.1/hook")


@pytest.mark.asyncio
async def test_validator_rejects_local_suffix_host():
    validator = WebhookTargetValidator(allowlist=[], block_private_targets=True)
    with pytest.raises(HTTPException, match="target host is not public"):
        await validator.validate("https://api.local/hook")


@pytest.mark.asyncio
async def test_validator_dns_resolution_failure(monkeypatch):
    validator = WebhookTargetValidator(allowlist=[], block_private_targets=True)

    class DummyLoop:
        async def getaddrinfo(self, *_args, **_kwargs):
            raise socket.gaierror("boom")

    monkeypatch.setattr("app.core.network_security.asyncio.get_running_loop", lambda: DummyLoop())

    with pytest.raises(HTTPException, match="host resolution failed"):
        await validator.validate("https://example.com/hook")


@pytest.mark.asyncio
async def test_validator_rejects_private_resolved_ip(monkeypatch):
    validator = WebhookTargetValidator(allowlist=[], block_private_targets=True)

    class DummyLoop:
        async def getaddrinfo(self, *_args, **_kwargs):
            return [
                (2, 1, 6, "", ("10.1.2.3", 443)),
            ]

    monkeypatch.setattr("app.core.network_security.asyncio.get_running_loop", lambda: DummyLoop())

    with pytest.raises(HTTPException, match="resolves to non-public IP"):
        await validator.validate("https://example.com/hook")


@pytest.mark.asyncio
async def test_validator_accepts_public_resolved_ip(monkeypatch):
    validator = WebhookTargetValidator(allowlist=[], block_private_targets=True)

    class DummyLoop:
        async def getaddrinfo(self, *_args, **_kwargs):
            return [
                (2, 1, 6, "", ("8.8.8.8", 443)),
            ]

    monkeypatch.setattr("app.core.network_security.asyncio.get_running_loop", lambda: DummyLoop())

    await validator.validate("https://example.com/hook")
