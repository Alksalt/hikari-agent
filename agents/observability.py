"""Lightweight Logfire wiring. Opt-in via env (config-driven), no-op otherwise.

Why Logfire: Helicone went to maintenance March 2026 (ClickHouse acquisition);
Logfire is the May-2026 price/quality leader at small scale — 10M spans free,
OTel-native, MIT-licensed self-host option. We import lazily so a missing
``logfire`` package never breaks startup.

Wire-up: ``init_logfire()`` is called from ``telegram_bridge.main`` after
logging is configured. The ``span`` and ``instrument`` decorators are no-ops
when logfire isn't initialized.
"""

from __future__ import annotations

import contextlib
import functools
import logging
from collections.abc import Callable
from typing import Any

from . import config as cfg

logger = logging.getLogger(__name__)

_LOGFIRE_AVAILABLE = False
_LOGFIRE_MOD: Any = None


def _enabled() -> bool:
    """Logfire is enabled only when (a) the env flag is on AND (b) the package
    is importable. Both must be true; otherwise we no-op."""
    env_key = str(cfg.get("logfire.enabled_env", "HIKARI_LOGFIRE_ENABLED"))
    return cfg.env_bool(env_key, False)


def init_logfire() -> bool:
    """Idempotent init. Returns True if Logfire is active."""
    global _LOGFIRE_AVAILABLE, _LOGFIRE_MOD
    if _LOGFIRE_AVAILABLE:
        return True
    if not _enabled():
        return False
    try:
        import logfire  # type: ignore
    except ImportError:
        # The operator asked for Logfire but didn't install the package.
        # Warn loudly so a misconfiguration doesn't silently disable
        # observability in production.
        logger.warning(
            "logfire requested via env but package not installed; "
            "run `uv add logfire` to enable. continuing without it."
        )
        return False
    try:
        token_env = str(cfg.get("logfire.token_env", "LOGFIRE_TOKEN"))
        service = str(cfg.get("logfire.service_name", "hikari"))
        # Logfire reads LOGFIRE_TOKEN from env by default; our config indirection
        # lets the deployer pick a different env name without code changes.
        token = cfg.env_or(token_env, "")
        logfire.configure(
            service_name=service,
            send_to_logfire=bool(token),
            token=token or None,
        )
    except Exception:
        logger.exception("logfire configure failed (continuing without it)")
        return False
    _LOGFIRE_AVAILABLE = True
    _LOGFIRE_MOD = logfire
    logger.info("logfire initialized (service=%s)", service)
    return True


@contextlib.contextmanager
def span(name: str, **attrs: Any):
    """Start a Logfire span, or no-op. Use ``with span("name", k=v): ...``."""
    if _LOGFIRE_AVAILABLE and _LOGFIRE_MOD is not None:
        with _LOGFIRE_MOD.span(name, **attrs):
            yield
    else:
        yield


def instrument(name: str | None = None) -> Callable:
    """Decorator: instrument a function with a span. Sync or async both work."""
    def _wrap(fn: Callable) -> Callable:
        span_name = name or fn.__name__

        if _is_async(fn):
            @functools.wraps(fn)
            async def _async(*args, **kwargs):
                with span(span_name):
                    return await fn(*args, **kwargs)
            return _async
        else:
            @functools.wraps(fn)
            def _sync(*args, **kwargs):
                with span(span_name):
                    return fn(*args, **kwargs)
            return _sync
    return _wrap


def _is_async(fn: Callable) -> bool:
    import inspect
    return inspect.iscoroutinefunction(fn)
