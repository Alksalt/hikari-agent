"""morning_brief tool — read codex briefings from alt-wiki/briefings/{ai,noise,vibecode}/<date>.md.

Distinct from the existing `agents/morning_brief.py` proactive weather brief —
this one is the on-demand wiki-reader tool surfaced to Hikari as
`mcp__hikari_wiki__morning_brief`. The test file is named *_tool.py to keep
the two test surfaces unambiguous.
"""
from __future__ import annotations

import importlib
from pathlib import Path

import pytest


def _handler(tool_obj):
    """Unwrap the SDK @tool decorator to access the underlying async fn."""
    return getattr(tool_obj, "handler", tool_obj)


@pytest.fixture
def vault(tmp_path: Path, monkeypatch):
    """Point both the wiki package's VAULT_ROOT and the env var at a temp dir."""
    monkeypatch.setenv("HIKARI_WIKI_VAULT", str(tmp_path))
    import tools.wiki._shared as _shared
    importlib.reload(_shared)
    import tools.wiki.morning_brief as _mb
    importlib.reload(_mb)
    return tmp_path, _mb


def _write_brief(vault_root: Path, topic: str, date_str: str, *, quiet=False, items=4, body="# heading\n\nbody text"):
    p = vault_root / "briefings" / topic / f"{date_str}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    front = (
        f"---\n"
        f"date: {date_str}\n"
        f"topic: {topic}\n"
        f"items_found: {items}\n"
        f"quiet_day: {'true' if quiet else 'false'}\n"
        f"---\n\n"
        f"{body}\n"
    )
    p.write_text(front, encoding="utf-8")


@pytest.mark.asyncio
async def test_morning_brief_tool_reads_single_topic(vault):
    vault_root, mb = vault
    _write_brief(vault_root, "ai", "2026-05-25", items=4, body="## TL;DR\n\n- thing happened")
    result = await _handler(mb.morning_brief_tool)({"topic": "ai", "date": "2026-05-25"})
    text = result["content"][0]["text"]
    assert "## ai — 2026-05-25" in text
    assert "thing happened" in text
    assert "briefings/ai/2026-05-25.md" in text


@pytest.mark.asyncio
async def test_morning_brief_tool_all_concatenates_three(vault):
    vault_root, mb = vault
    _write_brief(vault_root, "ai", "2026-05-25", items=4, body="ai stuff")
    _write_brief(vault_root, "noise", "2026-05-25", items=7, body="noise stuff")
    _write_brief(vault_root, "vibecode", "2026-05-25", items=3, body="vibecode stuff")
    result = await _handler(mb.morning_brief_tool)({"topic": "all", "date": "2026-05-25"})
    text = result["content"][0]["text"]
    assert "## ai — 2026-05-25" in text
    assert "## noise — 2026-05-25" in text
    assert "## vibecode — 2026-05-25" in text
    assert "ai stuff" in text and "noise stuff" in text and "vibecode stuff" in text
    # Sections separated by ---
    assert text.count("\n---\n") >= 2


@pytest.mark.asyncio
async def test_morning_brief_tool_quiet_day_marker(vault):
    vault_root, mb = vault
    _write_brief(vault_root, "ai", "2026-05-25", quiet=True, items=0, body="quiet body")
    result = await _handler(mb.morning_brief_tool)({"topic": "ai", "date": "2026-05-25"})
    text = result["content"][0]["text"]
    assert "(quiet day on ai)" in text


@pytest.mark.asyncio
async def test_morning_brief_tool_single_missing_returns_placeholder_block(vault):
    """Single-topic missing → per-topic placeholder block, not the whole-call fallback.
    The fallback is reserved for `all` mode with NONE present."""
    _vault_root, mb = vault
    result = await _handler(mb.morning_brief_tool)({"topic": "ai", "date": "2099-12-31"})
    text = result["content"][0]["text"]
    assert "## ai — 2099-12-31" in text
    assert "(no brief for this date)" in text
    # The whole-call fallback must NOT fire for single-topic requests.
    assert "no briefings on disk" not in text.lower()


@pytest.mark.asyncio
async def test_morning_brief_tool_all_missing_returns_fallback(vault):
    """`all` mode with no briefings on disk → whole-call fallback message."""
    _vault_root, mb = vault
    # No files written for any topic.
    result = await _handler(mb.morning_brief_tool)({"topic": "all", "date": "2099-12-31"})
    text = result["content"][0]["text"]
    assert "no briefings on disk" in text.lower()


@pytest.mark.asyncio
async def test_morning_brief_tool_unknown_topic_returns_voice_redirect(vault):
    _vault_root, mb = vault
    result = await _handler(mb.morning_brief_tool)({"topic": "wrong", "date": "2026-05-25"})
    text = result["content"][0]["text"]
    assert "unknown topic" in text.lower()
    assert "ai, noise, vibecode, all" in text


@pytest.mark.asyncio
async def test_morning_brief_tool_default_date_is_today(vault):
    vault_root, mb = vault
    from datetime import date as _date
    today = _date.today().isoformat()
    _write_brief(vault_root, "ai", today, items=4, body="today's body")
    result = await _handler(mb.morning_brief_tool)({"topic": "ai"})
    text = result["content"][0]["text"]
    assert today in text
    assert "today's body" in text


@pytest.mark.asyncio
async def test_morning_brief_tool_partial_present_is_ok(vault):
    """If two of three topics exist, return what's there + 'no brief' for the missing one."""
    vault_root, mb = vault
    _write_brief(vault_root, "ai", "2026-05-25", items=4, body="ai body")
    _write_brief(vault_root, "noise", "2026-05-25", items=7, body="noise body")
    # vibecode missing
    result = await _handler(mb.morning_brief_tool)({"topic": "all", "date": "2026-05-25"})
    text = result["content"][0]["text"]
    assert "ai body" in text
    assert "noise body" in text
    assert "(no brief for this date)" in text


@pytest.mark.asyncio
async def test_morning_brief_tool_emits_presentation_hint(vault):
    vault_root, mb = vault
    _write_brief(vault_root, "ai", "2026-05-25", items=4, body="x")
    result = await _handler(mb.morning_brief_tool)({"topic": "ai", "date": "2026-05-25"})
    text = result["content"][0]["text"]
    assert "### presentation_hint\nmorning_brief_digest" in text


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_date", [
    "../../../etc/passwd",
    "2026-05-25/../../../tmp",
    "2026/05/25",
    "26-05-25",
    "2026-5-25",
    "",  # caught by the today-fallback path, so should fail differently
    "2026-13-99",  # regex passes but it's still validated at the file-not-found layer
])
async def test_morning_brief_tool_rejects_path_traversal_in_date(vault, bad_date):
    """The `date` arg must be strict YYYY-MM-DD. Anything that could break
    out of briefings/{topic}/ via `..` or `/` is refused before path build."""
    _vault_root, mb = vault
    result = await _handler(mb.morning_brief_tool)({"topic": "ai", "date": bad_date})
    text = result["content"][0]["text"]
    if bad_date == "":
        # Empty string falls back to today.today().isoformat() — valid path.
        # We can't assert this case here; sanity: the call should not crash.
        assert "morning_brief" in text or "## ai" in text
    elif bad_date == "2026-13-99":
        # Regex accepts shape; file doesn't exist; placeholder fires.
        assert "(no brief for this date)" in text
    else:
        assert "invalid date" in text.lower()
        assert "yyyy-mm-dd" in text.lower()
