"""Sprint 6E — link_shelf SSRF refusal tests."""

from __future__ import annotations

import socket
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tools.link_shelf._safe_fetch import (
    SafeFetchError,
    _is_blocked_ip,
    _resolve_and_check,
    _validate_url,
    safe_fetch,
)

# ---------------------------------------------------------------------------
# _is_blocked_ip — IP classification
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("addr,expected", [
    # Blocked
    ("127.0.0.1", True),
    ("127.255.255.254", True),
    ("0.0.0.0", True),
    ("10.0.0.1", True),
    ("10.255.255.255", True),
    ("172.16.0.1", True),
    ("172.31.255.255", True),
    ("192.168.0.1", True),
    ("192.168.1.100", True),
    ("169.254.169.254", True),     # AWS/GCP/Azure metadata
    ("169.254.0.1", True),         # link-local
    ("::1", True),                  # IPv6 loopback
    ("fe80::1", True),              # IPv6 link-local
    ("fd00:abcd::1", True),         # IPv6 ULA
    ("ff02::1", True),              # IPv6 multicast
    # NOT blocked
    ("8.8.8.8", False),             # public DNS
    ("1.1.1.1", False),
    ("172.32.0.1", False),          # just outside 172.16/12
    ("192.169.0.1", False),         # just outside 192.168/16
    ("2606:4700:4700::1111", False),  # public IPv6
    # IPv6 transition forms — must unwrap embedded v4 and re-check
    ("::ffff:127.0.0.1", True),        # IPv4-mapped loopback
    ("::ffff:10.0.0.1", True),         # IPv4-mapped RFC1918
    ("::ffff:169.254.169.254", True),  # IPv4-mapped cloud metadata
    ("::ffff:a00:1", True),            # alt-form IPv4-mapped 10.0.0.1
    ("2002:7f00:1::", True),           # 6to4 wrapping 127.0.0.1
    ("2002:a00:1::", True),            # 6to4 wrapping 10.0.0.1
    # NAT64 well-known prefix (RFC 6052) — embeds v4 in low 32 bits
    ("64:ff9b::7f00:1", True),         # NAT64 → 127.0.0.1
    ("64:ff9b::a00:1", True),          # NAT64 → 10.0.0.1
    ("64:ff9b::a9fe:a9fe", True),      # NAT64 → 169.254.169.254 (metadata)
    ("64:ff9b:1::a00:1", True),        # NAT64 local prefix (RFC 8215)
    # Deprecated IPv4-compatible IPv6 (`::a.b.c.d`)
    ("::7f00:1", True),                # ::127.0.0.1
    ("::a00:1", True),                 # ::10.0.0.1
    ("::a9fe:a9fe", True),             # ::169.254.169.254
    # Carrier-grade NAT — RFC 6598
    ("100.64.0.1", True),
    ("100.127.255.254", True),
    ("100.128.0.0", False),            # just outside CGNAT range
])
def test_is_blocked_ip(addr: str, expected: bool):
    assert _is_blocked_ip(addr) is expected


def test_is_blocked_ip_garbage_string_blocked():
    assert _is_blocked_ip("not-an-ip") is True


# ---------------------------------------------------------------------------
# _validate_url — scheme + host shape
# ---------------------------------------------------------------------------

def test_validate_url_rejects_non_http_scheme():
    with pytest.raises(SafeFetchError, match="scheme"):
        _validate_url("file:///etc/passwd")
    with pytest.raises(SafeFetchError, match="scheme"):
        _validate_url("ftp://example.com/x")
    with pytest.raises(SafeFetchError, match="scheme"):
        _validate_url("gopher://example.com/x")


def test_validate_url_accepts_http_and_https():
    assert _validate_url("https://example.com/x")[0] == "https"
    assert _validate_url("http://example.com/x")[0] == "http"


# ---------------------------------------------------------------------------
# _resolve_and_check — DNS + literal-IP refusal
# ---------------------------------------------------------------------------

def test_resolve_rejects_literal_loopback_ipv4():
    with pytest.raises(SafeFetchError, match="literal IP"):
        _resolve_and_check("127.0.0.1")


def test_resolve_rejects_literal_metadata_ip():
    with pytest.raises(SafeFetchError, match="literal IP"):
        _resolve_and_check("169.254.169.254")


def test_resolve_rejects_literal_private_ipv4():
    with pytest.raises(SafeFetchError, match="literal IP"):
        _resolve_and_check("10.0.0.5")


def test_resolve_rejects_literal_loopback_ipv6():
    with pytest.raises(SafeFetchError, match="literal IP"):
        _resolve_and_check("::1")


def test_resolve_treats_dns_failure_as_refusal():
    with patch("socket.getaddrinfo", side_effect=socket.gaierror("no such host")):
        with pytest.raises(SafeFetchError, match="dns_failure"):
            _resolve_and_check("nonexistent.tld.invalid")


def test_resolve_rejects_host_resolving_to_private():
    # Mock DNS to return a private IP for a "public-looking" name.
    fake_info = [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("10.0.0.5", 80))]
    with patch("socket.getaddrinfo", return_value=fake_info):
        with pytest.raises(SafeFetchError, match="blocked address"):
            _resolve_and_check("totally-public.example.com", 80)


def test_resolve_accepts_host_resolving_to_public():
    fake_info = [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("8.8.8.8", 80))]
    with patch("socket.getaddrinfo", return_value=fake_info):
        _resolve_and_check("dns.google", 80)  # must not raise


# ---------------------------------------------------------------------------
# safe_fetch — end-to-end refusal scenarios
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_safe_fetch_refuses_literal_localhost():
    with pytest.raises(SafeFetchError):
        await safe_fetch("http://127.0.0.1:8080/")


@pytest.mark.asyncio
async def test_safe_fetch_refuses_literal_metadata():
    with pytest.raises(SafeFetchError):
        await safe_fetch("http://169.254.169.254/latest/meta-data/")


@pytest.mark.asyncio
async def test_safe_fetch_refuses_literal_private():
    with pytest.raises(SafeFetchError):
        await safe_fetch("http://10.0.0.5/")


@pytest.mark.asyncio
async def test_safe_fetch_refuses_file_scheme():
    with pytest.raises(SafeFetchError, match="scheme"):
        await safe_fetch("file:///etc/passwd")


@pytest.mark.asyncio
async def test_safe_fetch_refuses_redirect_to_private():
    """Public host responds 302 → private IP. Must refuse on the redirect hop."""
    # First hop: public host returns 302 → 10.0.0.5
    fake_resp_redirect = MagicMock(status_code=302, headers={"location": "http://10.0.0.5/"})
    fake_client_ctx = MagicMock()
    fake_client_ctx.__aenter__ = AsyncMock(return_value=MagicMock(
        get=AsyncMock(return_value=fake_resp_redirect),
    ))
    fake_client_ctx.__aexit__ = AsyncMock(return_value=False)

    public_info = [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("8.8.8.8", 80))]
    with patch("socket.getaddrinfo", return_value=public_info), \
         patch("httpx.AsyncClient", return_value=fake_client_ctx):
        with pytest.raises(SafeFetchError):
            # public-looking initial host; 302 → literal private IP
            await safe_fetch("http://example.com/")


@pytest.mark.asyncio
async def test_safe_fetch_refuses_long_redirect_chain():
    """Three+ redirects to public hosts must hit the 3-hop cap."""
    # Endpoint always 302s to itself (different path each hop).
    counter = {"n": 0}

    async def fake_get(url):
        counter["n"] += 1
        return MagicMock(status_code=302, headers={"location": f"http://example.com/hop{counter['n']}"})

    fake_client = MagicMock()
    fake_client.get = AsyncMock(side_effect=fake_get)
    fake_ctx = MagicMock()
    fake_ctx.__aenter__ = AsyncMock(return_value=fake_client)
    fake_ctx.__aexit__ = AsyncMock(return_value=False)

    public_info = [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("8.8.8.8", 80))]
    with patch("socket.getaddrinfo", return_value=public_info), \
         patch("httpx.AsyncClient", return_value=fake_ctx):
        with pytest.raises(SafeFetchError, match="redirect chain exceeded"):
            await safe_fetch("http://example.com/")


@pytest.mark.asyncio
async def test_safe_fetch_returns_body_on_success():
    fake_resp = MagicMock(
        status_code=200,
        headers={"content-type": "text/html; charset=utf-8"},
        content=b"<html><title>ok</title></html>",
    )
    fake_client = MagicMock()
    fake_client.get = AsyncMock(return_value=fake_resp)
    fake_ctx = MagicMock()
    fake_ctx.__aenter__ = AsyncMock(return_value=fake_client)
    fake_ctx.__aexit__ = AsyncMock(return_value=False)

    public_info = [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("8.8.8.8", 80))]
    with patch("socket.getaddrinfo", return_value=public_info), \
         patch("httpx.AsyncClient", return_value=fake_ctx):
        body, ctype, final_url = await safe_fetch("http://example.com/")
    assert b"<title>ok</title>" in body
    assert "html" in ctype
    assert final_url == "http://example.com/"
