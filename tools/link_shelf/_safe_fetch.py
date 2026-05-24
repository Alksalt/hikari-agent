"""Sprint 6E — SSRF-hardened HTTP fetch for link_shelf metadata.

The legacy link_shelf fetcher passed any http(s) URL straight to
``httpx.AsyncClient(follow_redirects=True)``, which means a user (or a
prompt-injected agent acting through ``link_save``) could trigger fetches to:
  - ``http://127.0.0.1:<port>`` — talk to the local bot or any localhost service
  - ``http://10.0.0.1/`` / ``192.168.x.x`` — internal LAN devices
  - ``http://169.254.169.254/`` — AWS/GCP/Azure instance metadata endpoint
  - any of the above via a 3xx redirect from a benign-looking URL

This module:
  1. Pre-resolves the hostname via DNS (`socket.getaddrinfo`) and rejects
     any address in a private / link-local / loopback / cloud-metadata range.
  2. Disables automatic redirects and re-validates each hop manually.
  3. Caps the redirect chain at 3.

Returns raw bytes + content-type on success, or raises ``SafeFetchError`` on
any policy violation. Network/timeout errors are also raised — the legacy
best-effort fallback lives in the caller, which swallows them.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
from typing import Final

logger = logging.getLogger(__name__)

_MAX_REDIRECTS: Final[int] = 3


class SafeFetchError(Exception):
    """Raised when a fetch is refused due to SSRF policy."""


_CGNAT_V4 = ipaddress.ip_network("100.64.0.0/10")
# NAT64 well-known prefix (RFC 6052) — embeds v4 in low 32 bits and a
# gateway translates it back. Where present, `64:ff9b::7f00:1` routes
# to 127.0.0.1. Same risk as 6to4. RFC 8215 reserves the local prefix.
_NAT64_WKP = ipaddress.ip_network("64:ff9b::/96")
_NAT64_LOCAL = ipaddress.ip_network("64:ff9b:1::/48")
# Deprecated IPv4-compatible IPv6 (`::a.b.c.d`) — stdlib has no helper but
# the literal still parses. `::7f00:1` is the literal for 127.0.0.1 in this
# form. The ::/96 prefix catches it (exclude ::1 / unspecified which are
# already flagged by .is_loopback / .is_unspecified).
_V4COMPAT_NET = ipaddress.ip_network("::/96")
_V6_UNSPECIFIED = ipaddress.IPv6Address("::")
_V6_LOOPBACK = ipaddress.IPv6Address("::1")


def _is_blocked_ip(addr_str: str) -> bool:
    """Return True if `addr_str` falls in a range we must not fetch from.

    Covers loopback, private (RFC1918 + ULA), link-local (incl. AWS
    metadata 169.254.169.254 explicitly), multicast, reserved, CGNAT
    (RFC 6598), and unspecified addresses.

    IPv6 transition forms (IPv4-mapped ``::ffff:a.b.c.d``, 6to4
    ``2002::/16``, Teredo ``2001::/32``) are unwrapped and the embedded
    IPv4 is re-checked — otherwise ``::ffff:127.0.0.1`` would slip past
    ``is_loopback`` and resolve to localhost.
    """
    try:
        ip = ipaddress.ip_address(addr_str)
    except ValueError:
        return True

    # Unwrap IPv6 transition forms and re-check the embedded IPv4.
    if isinstance(ip, ipaddress.IPv6Address):
        for embedded in (ip.ipv4_mapped, ip.sixtofour, ip.teredo):
            if embedded is None:
                continue
            # teredo returns (server_ip, client_ip); the client v4 is the
            # one whose address-space we actually contact.
            v4 = embedded[1] if isinstance(embedded, tuple) else embedded
            if _is_blocked_ip(str(v4)):
                return True
        # NAT64 well-known prefixes (RFC 6052 + RFC 8215) — extract the
        # embedded v4 from the low 32 bits and re-check.
        if ip in _NAT64_WKP or ip in _NAT64_LOCAL:
            embedded_v4 = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
            if _is_blocked_ip(str(embedded_v4)):
                return True
        # Deprecated IPv4-compatible IPv6 (`::a.b.c.d`). Skip the two well-
        # known special values already handled above.
        if ip in _V4COMPAT_NET and ip not in (_V6_UNSPECIFIED, _V6_LOOPBACK):
            embedded_v4 = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
            if _is_blocked_ip(str(embedded_v4)):
                return True

    if ip.is_loopback:
        return True
    if ip.is_private:
        return True
    if ip.is_link_local:
        return True
    if ip.is_multicast:
        return True
    if ip.is_unspecified:
        return True
    if ip.is_reserved:
        return True
    # Carrier-grade NAT (RFC 6598) — `is_private` doesn't cover this range
    # but it carries customer-side traffic that can reach internal hosts.
    if isinstance(ip, ipaddress.IPv4Address) and ip in _CGNAT_V4:
        return True
    # Explicit cloud-metadata IPs — already caught by link-local but
    # called out for clarity and as defence against future stdlib drift.
    if str(ip) in {"169.254.169.254", "fd00:ec2::254"}:
        return True
    return False


def _resolve_and_check(host: str, port: int = 0) -> None:
    """DNS-resolve `host` and raise SafeFetchError if ANY answer is blocked.

    Treats DNS failure itself as a refusal — better to bail than to let
    httpx re-resolve and possibly race a different answer.
    """
    if not host:
        raise SafeFetchError("empty hostname")
    # Strict literal-IP check first (covers IPv6 brackets etc.)
    bare = host.strip("[]")
    try:
        ip = ipaddress.ip_address(bare)
    except ValueError:
        pass
    else:
        if _is_blocked_ip(str(ip)):
            raise SafeFetchError(f"refused: literal IP {bare} in blocked range")
        return
    try:
        infos = socket.getaddrinfo(host, port or None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise SafeFetchError(f"dns_failure:{e}") from e
    seen: set[str] = set()
    for fam, _stype, _proto, _canon, sockaddr in infos:
        addr = str(sockaddr[0])
        if fam == socket.AF_INET6:
            # Strip zone id if present (e.g. fe80::1%en0)
            addr = addr.split("%", 1)[0]
        if addr in seen:
            continue
        seen.add(addr)
        if _is_blocked_ip(addr):
            raise SafeFetchError(
                f"refused: host {host!r} resolves to blocked address {addr}"
            )


def _validate_url(url: str) -> tuple[str, str, int]:
    """Parse + validate scheme/host. Returns (scheme, host, port)."""
    from urllib.parse import urlparse
    p = urlparse(url)
    if p.scheme not in {"http", "https"}:
        raise SafeFetchError(f"refused: scheme {p.scheme!r} not in http/https")
    host = p.hostname or ""
    port = p.port or (443 if p.scheme == "https" else 80)
    return p.scheme, host, port


async def safe_fetch(
    url: str,
    *,
    timeout_sec: float = 5.0,
    max_bytes: int = 200_000,
    user_agent: str = "hikari-agent link-shelf/1.0",
) -> tuple[bytes, str, str]:
    """Fetch ``url`` with SSRF + redirect-chain hardening.

    Returns ``(content_bytes, content_type, final_url)`` — content is sliced
    to ``max_bytes``. Raises ``SafeFetchError`` on any policy violation,
    or ``httpx.HTTPError`` on network failure.
    """
    import httpx  # noqa: PLC0415 — lazy: matches caller's pattern

    current_url = url
    for hop in range(_MAX_REDIRECTS + 1):
        scheme, host, port = _validate_url(current_url)
        _resolve_and_check(host, port)

        async with httpx.AsyncClient(
            timeout=timeout_sec,
            follow_redirects=False,
            headers={"User-Agent": user_agent, "Accept": "text/html,*/*;q=0.5"},
        ) as client:
            resp = await client.get(current_url)

        if resp.status_code in {301, 302, 303, 307, 308}:
            location = resp.headers.get("location")
            if not location:
                raise SafeFetchError(f"redirect with no Location header from {current_url}")
            from urllib.parse import urljoin
            next_url = urljoin(current_url, location)
            if hop >= _MAX_REDIRECTS:
                raise SafeFetchError(
                    f"refused: redirect chain exceeded {_MAX_REDIRECTS} hops"
                )
            current_url = next_url
            continue

        ctype = resp.headers.get("content-type", "")
        body = resp.content[:max_bytes]
        return (body, ctype, current_url)

    # Unreachable — the for-loop either returns or raises.
    raise SafeFetchError("redirect loop exited without resolution")
