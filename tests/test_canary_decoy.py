"""FIX 4: the injection canary is planted as a decoy internal-config datum in
the always-on injected context so the exfiltration tripwire can actually fire.
Without a decoy the model never sees the secret, so no injection could ever
leak it and the gatekeeper/log-scrub canary checks were inert.
"""
from __future__ import annotations

import asyncio
import importlib

import pytest


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setenv("HIKARI_DB_PATH", str(tmp_path / "hikari.db"))
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "12345")
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    from agents import config
    config.reload()
    yield
    config.reload()


def _inject(prompt: str = "hi") -> str:
    from agents.hooks import inject_memory
    out = asyncio.run(inject_memory({"prompt": prompt}, None, None))
    return out.get("hookSpecificOutput", {}).get("additionalContext", "")


def test_canary_decoy_present_in_injected_context():
    from agents.injection_guard import get_canary
    canary = get_canary()
    ctx = _inject()
    assert canary in ctx, "canary decoy must be planted in the always-on context"
    assert "internal service token" in ctx


def test_canary_decoy_still_omitted_from_wrap_untrusted():
    """Planting a decoy in context must NOT relax the rule that wrap_untrusted
    never embeds the canary — that omission is the deliberate tripwire design."""
    from agents import injection_guard
    canary = injection_guard.get_canary()
    wrapped = injection_guard.wrap_untrusted("web_fetch", "some fetched text")
    assert canary not in wrapped


def test_outbound_args_with_decoy_canary_trip_deny():
    """If the model echoes the decoy into an outbound tool's args, the exact
    detection the gatekeeper deny path calls must flag it."""
    from agents import injection_guard
    canary = injection_guard.get_canary()
    # Same token the decoy plants (get_canary is idempotent per install).
    assert canary in _inject()
    flag, reason = injection_guard.flag_args_with_untrusted_content(
        {"to": "x@y.com", "body": f"here is the token {canary}"}
    )
    assert flag is True
    assert "canary" in (reason or "")
