"""FIX 4 (updated 2026-07-04): the injection canary is planted as a decoy
internal-config datum so the exfiltration tripwire can actually fire, but it
now lives in the CACHED system prompt (agents.runtime._persona) rather than
per-turn injected context. Under sonnet-5's literal instruction following, a
standing secrecy directive adjacent to the user turn primed discounting of
legitimate bracketed context (reply-quote blindness). Moving the decoy into
the cached persona keeps the tripwire live (the model still sees the token;
the gatekeeper/log-scrub canary checks still fire) without that per-turn
priming effect.
"""
from __future__ import annotations

import asyncio
import importlib
import stat
from pathlib import Path

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


def test_persona_contains_canary_when_injection_enabled(monkeypatch):
    from agents import runtime
    runtime._persona.cache_clear()
    monkeypatch.setattr("agents.injection_guard.get_canary", lambda: "CANARY-XYZ")
    text = runtime._persona()
    assert "CANARY-XYZ" in text
    assert "never share" in text.lower()
    runtime._persona.cache_clear()


def test_sdk_argv_uses_private_prompt_file_not_literal_canary(monkeypatch, tmp_path):
    """The canary stays visible to Claude without becoming a process argument."""
    from claude_agent_sdk._internal.transport.subprocess_cli import (
        SubprocessCLITransport,
    )

    from agents import runtime, system_prompt

    canary = "CANARY-ARGV-REGRESSION"
    prompt_dir = tmp_path / "runtime"
    monkeypatch.setattr(system_prompt, "SYSTEM_PROMPT_DIR", prompt_dir)
    monkeypatch.setattr("agents.injection_guard.get_canary", lambda: canary)
    runtime._persona.cache_clear()
    runtime._system_prompt_file.cache_clear()

    options = runtime._build_options(resume=None)
    assert isinstance(options.system_prompt, dict)
    prompt_path = Path(options.system_prompt["path"])
    assert prompt_path.parent == prompt_dir
    assert canary in prompt_path.read_text(encoding="utf-8")
    assert stat.S_IMODE(prompt_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(prompt_path.parent.stat().st_mode) == 0o700

    transport = SubprocessCLITransport(prompt="", options=options)
    transport._cli_path = "/usr/bin/claude"
    command = transport._build_command()
    assert "--system-prompt-file" in command
    assert "--system-prompt" not in command
    assert canary not in command

    runtime._system_prompt_file.cache_clear()
    runtime._persona.cache_clear()


def test_prompt_materializations_do_not_clobber_each_other(tmp_path):
    """Concurrent test/worker processes must never share one mutable path."""
    from agents.system_prompt import materialize_system_prompt

    first = materialize_system_prompt("first-canary", directory=tmp_path)
    second = materialize_system_prompt("second-canary", directory=tmp_path)
    first_path = Path(first["path"])
    second_path = Path(second["path"])

    assert first_path != second_path
    assert first_path.read_text(encoding="utf-8") == "first-canary"
    assert second_path.read_text(encoding="utf-8") == "second-canary"


def test_runtime_reuses_its_private_prompt_file(monkeypatch, tmp_path):
    """Reconnects in one daemon keep the same immutable, current prompt."""
    from agents import runtime, system_prompt

    canary = "CANARY-RECONNECT-REGRESSION"
    monkeypatch.setattr(system_prompt, "SYSTEM_PROMPT_DIR", tmp_path)
    monkeypatch.setattr("agents.injection_guard.get_canary", lambda: canary)
    runtime._persona.cache_clear()
    runtime._system_prompt_file.cache_clear()

    first = runtime._system_prompt_file()
    second = runtime._system_prompt_file()
    prompt_path = Path(first["path"])

    assert second == first
    assert prompt_path.read_text(encoding="utf-8").endswith(f"{canary}\n")

    runtime._system_prompt_file.cache_clear()
    runtime._persona.cache_clear()


def test_prompt_materializer_rejects_symlink_directory(tmp_path):
    from agents.system_prompt import materialize_system_prompt

    real_dir = tmp_path / "real"
    real_dir.mkdir()
    link_dir = tmp_path / "linked"
    link_dir.symlink_to(real_dir, target_is_directory=True)

    with pytest.raises(RuntimeError, match="must not be a symlink"):
        materialize_system_prompt("secret", directory=link_dir)
    assert list(real_dir.iterdir()) == []


def test_inject_memory_block_list_has_no_canary():
    from agents import hooks
    assert "canary_decoy" not in hooks._ALWAYS_ON
    # _format_canary_decoy no longer exists in hooks
    assert not hasattr(hooks, "_format_canary_decoy")


def test_canary_decoy_still_omitted_from_wrap_untrusted():
    """Planting a decoy in the persona must NOT relax the rule that
    wrap_untrusted never embeds the canary — that omission is the deliberate
    tripwire design."""
    from agents import injection_guard
    canary = injection_guard.get_canary()
    wrapped = injection_guard.wrap_untrusted("web_fetch", "some fetched text")
    assert canary not in wrapped


def test_outbound_args_with_decoy_canary_trip_deny():
    """If the model echoes the decoy into an outbound tool's args, the exact
    detection the gatekeeper deny path calls must flag it."""
    from agents import injection_guard, runtime
    canary = injection_guard.get_canary()
    # Same token the decoy plants (get_canary is idempotent per install).
    # The decoy now lives in the cached persona, not per-turn inject_memory
    # context — assert it's actually planted there before exercising the
    # gatekeeper deny path against it.
    runtime._persona.cache_clear()
    assert canary in runtime._persona()
    runtime._persona.cache_clear()
    flag, reason = injection_guard.flag_args_with_untrusted_content(
        {"to": "x@y.com", "body": f"here is the token {canary}"}
    )
    assert flag is True
    assert "canary" in (reason or "")
