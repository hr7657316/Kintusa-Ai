"""Outbound-request guard for URLs that customers (not the operator) supplied.

The hosted worker fetches spec, changelog and MCP URLs that come from customer repos,
the settings API, CLI pushes and runtime samples. Without a guard, any of those could
point the worker at ``http://169.254.169.254/`` (cloud metadata), ``localhost`` admin
ports or private network services (SSRF). Operator-seeded sources are not guarded.

Stdlib only, so the open-source watchers may import it. The CLI never applies the guard:
a developer pointing ``kintsua watch`` at their own localhost server is legitimate.

Usage on the hosted side::

    check_url(url)                                   # raises BlockedURL before the first request
    httpx.AsyncClient(event_hooks={"request": [async_request_hook]})   # re-checks every redirect

Known limit: a hostname that resolves to a public address at check time and a private
one at connect time (DNS rebinding) is not caught; put the worker behind an egress proxy
for that.
"""

from __future__ import annotations

import ipaddress
import socket
from typing import Any
from urllib.parse import urlsplit

BLOCKED_HOST_SUFFIXES = (".localhost", ".local", ".internal", ".localdomain", ".home.arpa")
BLOCKED_HOSTS = frozenset({"localhost", "metadata", "metadata.google.internal", "instance-data"})
_CGNAT = ipaddress.ip_network("100.64.0.0/10")


class BlockedURL(ValueError):
    """The URL points at a non-public destination (or is not http/https)."""


def is_http_locator(locator: str | None) -> bool:
    return bool(locator) and locator.strip().lower().startswith(("http://", "https://"))


def is_blocked_ip(ip: str | ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    try:
        addr = ipaddress.ip_address(ip) if isinstance(ip, str) else ip
    except ValueError:
        return True
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    return bool(
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
        or (isinstance(addr, ipaddress.IPv4Address) and addr in _CGNAT)
        or (isinstance(addr, ipaddress.IPv6Address) and addr.is_site_local)
    )


def check_host(host: str, *, resolve: bool = True) -> None:
    h = (host or "").strip().strip("[]").rstrip(".").lower()
    if not h:
        raise BlockedURL("URL has no host")
    if h in BLOCKED_HOSTS or h.endswith(BLOCKED_HOST_SUFFIXES):
        raise BlockedURL(f"host {h!r} is not a public address")
    try:
        literal = ipaddress.ip_address(h)
    except ValueError:
        literal = None
    if literal is not None:
        if is_blocked_ip(literal):
            raise BlockedURL(f"address {h} is not a public address")
        return
    if not resolve:
        return
    try:
        infos = socket.getaddrinfo(h, None, proto=socket.IPPROTO_TCP)
    except OSError:
        return  # unresolvable: the fetch itself will fail with a clear error
    for info in infos:
        ip = info[4][0]
        if is_blocked_ip(ip):
            raise BlockedURL(f"host {h!r} resolves to non-public address {ip}")


def check_url(url: str, *, resolve: bool = True) -> None:
    """Raise ``BlockedURL`` unless ``url`` is http(s) to a public destination."""
    parts = urlsplit(url.strip())
    if parts.scheme.lower() not in ("http", "https"):
        raise BlockedURL(f"only http(s) URLs are fetched, got {parts.scheme or 'no scheme'!r}")
    if parts.username or parts.password:
        raise BlockedURL("URLs with embedded credentials are not fetched")
    check_host(parts.hostname or "", resolve=resolve)


def request_hook(request: Any) -> None:
    """httpx sync ``event_hooks['request']`` hook: applies ``check_url`` to every request,
    including each redirect hop."""
    check_url(str(request.url))


async def async_request_hook(request: Any) -> None:
    """httpx async ``event_hooks['request']`` hook (see ``request_hook``)."""
    check_url(str(request.url))
