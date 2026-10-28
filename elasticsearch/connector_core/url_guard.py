"""Validate the cluster URL a caller supplies (SSRF protection).

The caller chooses which server this connector talks to, so without checks
they could point it at internal services (cloud metadata, localhost, the
company network). Two modes, chosen by the operator:

* ES_ALLOWED_HOSTS set (comma-separated, e.g. "es.example.com,*.es.cloud.io"):
  only those hosts are accepted. Use this in production.
* ES_ALLOWED_HOSTS unset: any host is accepted over https, but only if every
  address it resolves to is a public one (no private, loopback or link-local).

Known limit: in the second mode the name is resolved here and again when the
request is sent, so a hostile DNS server could still switch answers in
between (DNS rebinding). The allowlist closes that gap.
"""

import asyncio
import ipaddress
import os
import socket
from urllib.parse import urlsplit

from fastmcp.exceptions import ToolError


def _allowed_patterns() -> list[str]:
    raw = os.environ.get("ES_ALLOWED_HOSTS", "")
    return [p.strip().lower() for p in raw.split(",") if p.strip()]


def _host_allowed(host: str, patterns: list[str]) -> bool:
    for pattern in patterns:
        if pattern.startswith("*."):
            # "*.es.io" matches "a.es.io" but not "es.io" or "a.es.io.evil.com"
            if host.endswith(pattern[1:]):
                return True
        elif host == pattern:
            return True
    return False


async def _require_public(host: str, port: int) -> None:
    try:
        infos = await asyncio.to_thread(socket.getaddrinfo, host, port, type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise ToolError(f"Could not resolve host '{host}'.") from None
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        mapped = getattr(ip, "ipv4_mapped", None)
        if mapped is not None:
            ip = mapped
        if not ip.is_global:
            raise ToolError("That address is not allowed (private, loopback or link-local).")


async def validate_base_url(raw: str) -> str:
    """Return a cleaned base URL, or raise ToolError if it is not acceptable."""
    try:
        parts = urlsplit(raw.strip())
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError:
        raise ToolError("X-Elasticsearch-URL is not a valid URL.") from None

    if parts.scheme not in {"http", "https"} or not host:
        raise ToolError("X-Elasticsearch-URL must look like https://host:port.")
    if parts.username or parts.password:
        raise ToolError("Do not put credentials in the URL; send them in the Authorization header.")
    if parts.query or parts.fragment:
        raise ToolError("X-Elasticsearch-URL must not contain a query string or fragment.")

    patterns = _allowed_patterns()
    if patterns:
        if not _host_allowed(host, patterns):
            raise ToolError(f"Host '{host}' is not allowed on this server.")
    else:
        if parts.scheme != "https":
            raise ToolError("Only https URLs are accepted.")
        await _require_public(host, port or 443)

    return f"{parts.scheme}://{parts.netloc}{parts.path.rstrip('/')}"
