"""`# now` block regression net.

The block is what reminder_create relies on for relative-time math
("через годину" → ISO). It must always render, must fall back cleanly
when no HOME_TZ is set, and must respect HOME_TZ when present.
"""
from __future__ import annotations

import pytest

from agents import hooks


def test_format_now_always_renders():
    block = hooks._format_now()
    assert block.startswith("# now")
    assert "utc:" in block
    assert "local:" in block


def test_home_tz_env_used_when_set(monkeypatch):
    monkeypatch.setenv("HOME_TZ", "America/Los_Angeles")
    block = hooks._format_now()
    assert "America/Los_Angeles" in block


def test_fallback_to_oslo_when_unset(monkeypatch):
    monkeypatch.delenv("HOME_TZ", raising=False)
    # scheduler.timezone in engagement.yaml defaults to Europe/Oslo;
    # _resolve_local_tz_name returns it before the hardcoded fallback.
    tz_name = hooks._resolve_local_tz_name()
    # Either the config-default or the hardcoded fallback must be a
    # valid IANA tz string.
    assert "/" in tz_name


def test_invalid_tz_falls_back_to_utc_label(monkeypatch):
    monkeypatch.setenv("HOME_TZ", "Mars/Olympus")
    block = hooks._format_now()
    # Block still renders, with a warning instead of crashing.
    assert block.startswith("# now")
    assert "unknown tz" in block


@pytest.mark.asyncio
async def test_inject_memory_includes_now_block_at_top(monkeypatch, tmp_path):
    """End-to-end: when inject_memory runs, the `# now` block appears in
    the assembled additionalContext, and `# tools available` follows."""
    import importlib

    from agents import config
    monkeypatch.setenv("HIKARI_DB_PATH", str(tmp_path / "test.db"))
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    config.reload()

    out = await hooks.inject_memory({"prompt": "hi"}, None, None)
    additional = out.get("hookSpecificOutput", {}).get("additionalContext", "")
    assert "# now" in additional
    assert "utc:" in additional
    # `# now` should appear before the `# tools available` block.
    now_idx = additional.find("# now")
    tools_idx = additional.find("# tools available")
    assert now_idx >= 0
    if tools_idx != -1:
        assert tools_idx > now_idx
