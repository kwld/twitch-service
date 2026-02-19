from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit

from fastapi import HTTPException


def parse_allowed_ip_networks(raw: str) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    values = [v.strip() for v in raw.split(",") if v.strip()]
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for value in values:
        try:
            if "/" in value:
                network = ipaddress.ip_network(value, strict=False)
            else:
                host = ipaddress.ip_address(value)
                network = ipaddress.ip_network(f"{host}/{host.max_prefixlen}", strict=False)
        except ValueError as exc:
            raise RuntimeError(
                f"Invalid APP_ALLOWED_IPS entry '{value}'. Use IPv4/IPv6 or CIDR values."
            ) from exc
        networks.append(network)
    return networks


def resolve_client_ip(
    direct_host: str | None,
    x_forwarded_for: str | None,
    *,
    trust_x_forwarded_for: bool,
) -> str | None:
    if trust_x_forwarded_for and x_forwarded_for:
        forwarded = x_forwarded_for.split(",", 1)[0].strip()
        if forwarded:
            return forwarded
    return direct_host


def is_ip_allowed(
    client_ip: str | None,
    allowed_ip_networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network],
) -> bool:
    if not allowed_ip_networks:
        return True
    if not client_ip:
        return False
    try:
        parsed_ip = ipaddress.ip_address(client_ip)
    except ValueError:
        return False
    return any(parsed_ip in network for network in allowed_ip_networks)


def parse_webhook_target_allowlist(raw: str) -> list[str]:
    hosts = [v.strip().lower().lstrip(".") for v in raw.split(",") if v.strip()]
    for host in hosts:
        if "://" in host or "/" in host:
            raise RuntimeError(
                f"Invalid APP_WEBHOOK_TARGET_ALLOWLIST entry '{host}'. Use hostnames only."
            )
    return hosts


def host_matches_allowlist(host: str, allowlist: list[str]) -> bool:
    normalized = host.strip().lower().rstrip(".")
    if not allowlist:
        return True
    return any(normalized == allowed or normalized.endswith(f".{allowed}") for allowed in allowlist)


def is_public_ip_address(value: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return not (
        value.is_private
        or value.is_loopback
        or value.is_link_local
        or value.is_multicast
        or value.is_reserved
        or value.is_unspecified
    )


class WebhookTargetValidator:
    def __init__(self, allowlist: list[str], block_private_targets: bool) -> None:
        self.allowlist = allowlist
        self.block_private_targets = block_private_targets

    async def validate(self, raw_url: str) -> None:
        split = urlsplit(raw_url)
        if split.scheme not in {"http", "https"}:
            raise HTTPException(status_code=422, detail="webhook_url must use http or https")
        if split.username or split.password:
            raise HTTPException(status_code=422, detail="webhook_url must not contain userinfo credentials")
        host = (split.hostname or "").strip().lower().rstrip(".")
        if not host:
            raise HTTPException(status_code=422, detail="webhook_url host is required")
        if not host_matches_allowlist(host, self.allowlist):
            raise HTTPException(
                status_code=422,
                detail="webhook_url host is not allowed by APP_WEBHOOK_TARGET_ALLOWLIST",
            )
        if not self.block_private_targets:
            return
        try:
            parsed = ipaddress.ip_address(host)
        except ValueError:
            parsed = None
        if parsed:
            if not is_public_ip_address(parsed):
                raise HTTPException(status_code=422, detail="webhook_url target IP must be public")
            return
        if host.endswith((".localhost", ".local", ".internal")):
            raise HTTPException(status_code=422, detail="webhook_url target host is not public")
        port = split.port or (443 if split.scheme == "https" else 80)
        try:
            infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            raise HTTPException(status_code=422, detail=f"webhook_url host resolution failed: {exc}") from exc
        if not infos:
            raise HTTPException(status_code=422, detail="webhook_url host resolution returned no addresses")
        resolved_ips: set[str] = set()
        for family, _, _, _, sockaddr in infos:
            if family == socket.AF_INET:
                resolved_ips.add(str(sockaddr[0]))
            elif family == socket.AF_INET6:
                resolved_ips.add(str(sockaddr[0]))
        if not resolved_ips:
            raise HTTPException(status_code=422, detail="webhook_url host resolution returned no usable IP addresses")
        for raw_ip in resolved_ips:
            try:
                ip_value = ipaddress.ip_address(raw_ip)
            except ValueError:
                continue
            if not is_public_ip_address(ip_value):
                raise HTTPException(
                    status_code=422,
                    detail="webhook_url target host resolves to non-public IP address",
                )

