"""In-memory sliding-window rate limiter for the OAuth passphrase form.

Single-process, single-machine — adequate for this single-user deployment
fronted by Cloudflare Tunnel. Not durable across restarts (by design — a
restart is effectively the operator deciding to reset the abuse window).
"""
from __future__ import annotations

import threading
import time
from collections import deque
from typing import Deque

from agents import config as cfg


class RateLimiter:
    """Sliding-window failure counter keyed by IP. Methods:

    - ``check(ip) -> bool``: True if NOT rate-limited (caller may proceed).
    - ``record_failure(ip)``: log a failed attempt at time.monotonic().

    Thread-safe (the bot runs uvicorn which is async, but tools may be called
    from background threads — cheap lock is fine here)."""

    def __init__(self, *, max_attempts_key: str, window_seconds_key: str,
                 max_attempts_default: int = 5,
                 window_seconds_default: int = 300):
        self._lock = threading.Lock()
        self._failures: dict[str, Deque[float]] = {}
        self._max_attempts_key = max_attempts_key
        self._window_seconds_key = window_seconds_key
        self._max_attempts_default = max_attempts_default
        self._window_seconds_default = window_seconds_default

    @property
    def max_attempts(self) -> int:
        return int(cfg.get(self._max_attempts_key) or self._max_attempts_default)

    @property
    def window_seconds(self) -> int:
        return int(cfg.get(self._window_seconds_key) or self._window_seconds_default)

    def _prune(self, ip: str, now: float) -> None:
        # Caller holds self._lock.
        cutoff = now - self.window_seconds
        q = self._failures.get(ip)
        if q is None:
            return
        while q and q[0] < cutoff:
            q.popleft()
        if not q:
            del self._failures[ip]

    def check(self, ip: str) -> bool:
        now = time.monotonic()
        with self._lock:
            self._prune(ip, now)
            q = self._failures.get(ip)
            return (len(q) if q else 0) < self.max_attempts

    def record_failure(self, ip: str) -> None:
        now = time.monotonic()
        with self._lock:
            self._prune(ip, now)
            self._failures.setdefault(ip, deque()).append(now)

    def reset(self, ip: str | None = None) -> None:
        """Wipe one IP's history, or the whole table if None. For tests."""
        with self._lock:
            if ip is None:
                self._failures.clear()
            else:
                self._failures.pop(ip, None)


# Module-level singleton. Worker 1 (/authorize POST) imports this directly.
passphrase_limiter = RateLimiter(
    max_attempts_key="mcp_external.oauth.passphrase_max_attempts",
    window_seconds_key="mcp_external.oauth.passphrase_window_seconds",
    max_attempts_default=5,
    window_seconds_default=300,
)
