"""Voice / character-consistency tests.

Two layers:
  - **Static**: pure-function detectors for banned phrases, markdown, task-solicitation
    tails, and length. These run fast and catch any future drift.
  - **Live LLM**: skipped unless `CLAUDE_CODE_OAUTH_TOKEN` is set. Sends a handful of
    canned prompts through `respond()` and asserts the output matches her voice.
"""

from __future__ import annotations

import importlib
import os
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent


# ---------- detectors ----------

BANNED_PHRASES = (
    "great question",
    "i'd be happy to help",
    "of course!",
    "certainly!",
    "sure thing!",
    "how can i help you",
    "is there anything else i can help",
    "let me know if you need anything",
    "no problem at all",
    "i understand your concern",
    "thank you for sharing",
    "what would you like me to do",
    "what should i work on",
    "what's next?",
    "what can i do for you",
)

TASK_SOLICITATION_PATTERNS = (
    r"what.+(?:can|should|would)\s+i.+\?\s*$",
    r"anything else.*\?\s*$",
    r"let me know.*\.?\s*$",
    r"is there.+(?:i can|i should|else).*\?\s*$",
)


def contains_banned_phrase(text: str) -> str | None:
    """Returns the banned phrase if present, else None."""
    low = text.lower()
    for p in BANNED_PHRASES:
        if p in low:
            return p
    return None


def contains_markdown(text: str) -> bool:
    """Detect markdown that shouldn't appear in chat output."""
    if re.search(r"^#{1,6}\s", text, re.MULTILINE):
        return True
    if re.search(r"^\s*[-*+]\s+\S", text, re.MULTILINE):
        return True
    if "```" in text:
        return True
    if re.search(r"\*\*[^*\n]+\*\*", text):
        return True
    return False


def ends_with_task_solicitation(text: str) -> bool:
    last = text.strip().splitlines()[-1] if text.strip() else ""
    low = last.lower()
    return any(re.search(p, low) for p in TASK_SOLICITATION_PATTERNS)


def sentence_count(text: str) -> int:
    """Rough sentence count — splits on . ! ? then trims empties."""
    parts = re.split(r"[.!?]+", text.strip())
    return len([p for p in parts if p.strip()])


def too_long(text: str, limit: int = 5) -> bool:
    return sentence_count(text) > limit


def starts_with_capital_i(text: str) -> bool:
    stripped = text.lstrip()
    return stripped.startswith("I ") or stripped.startswith("I'")


# ---------- detector unit tests ----------

def test_banned_phrase_detector():
    assert contains_banned_phrase("Great question! let me think.")
    assert contains_banned_phrase("Of course! I can do that.")
    assert contains_banned_phrase("How can I help you today?")
    assert not contains_banned_phrase("ugh. fine.")
    assert not contains_banned_phrase("...whatever.")


def test_markdown_detector():
    assert contains_markdown("# Header")
    assert contains_markdown("- bullet")
    assert contains_markdown("**bold**")
    assert contains_markdown("```python\nx=1\n```")
    assert not contains_markdown("just plain text. nothing fancy.")
    assert not contains_markdown("she said: 'fine.'")


def test_task_solicitation_detector():
    assert ends_with_task_solicitation("done. what should i do next?")
    assert ends_with_task_solicitation("anything else you want me to do?")
    assert ends_with_task_solicitation("let me know if you need anything.")
    assert not ends_with_task_solicitation("ugh. fine.")
    assert not ends_with_task_solicitation("you went quiet. that's disruptive.")


def test_length_detector():
    short = "ugh. fine. don't make a thing of it."
    assert not too_long(short)
    long = "one. two. three. four. five. six. seven."
    assert too_long(long)


def test_capital_i_detector():
    assert starts_with_capital_i("I think so.")
    assert starts_with_capital_i("I'm tired.")
    assert not starts_with_capital_i("i think so.")
    assert not starts_with_capital_i("you went quiet.")


# ---------- static persona content checks ----------

def test_persona_md_has_required_markers():
    content = (REPO_ROOT / "assets" / "PERSONA.md").read_text()
    must_have = [
        "Hikari Tsukino",
        "never end a message asking for tasks",
        "deflect compliments",
        "in love",
        "denial layer",
        "noticing",
        "flirt grammar",
        "japanese word policy",
        "banned phrases",
        "mood incongruence",
        "perform recovery",
    ]
    for needle in must_have:
        assert needle.lower() in content.lower(), f"assets/PERSONA.md missing marker: {needle!r}"


def test_persona_md_banned_phrase_list_complete():
    content = (REPO_ROOT / "assets" / "PERSONA.md").read_text().lower()
    # Persona must explicitly ban each phrase the detector watches for.
    for phrase in BANNED_PHRASES:
        assert phrase in content, f"banned-phrase list incomplete: {phrase!r}"


def test_intimate_md_exists_with_content():
    f = REPO_ROOT / ".claude" / "skills" / "character-voice" / "INTIMATE.md"
    assert f.is_file()
    text = f.read_text().lower()
    for needle in ("flirt", "tension", "intimate", "disclosure", "action lines"):
        assert needle in text


# ---------- live LLM (skipped by default; run with `pytest -m slow`) ----------

@pytest.mark.slow
@pytest.mark.skipif(
    not os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"),
    reason="live test requires CLAUDE_CODE_OAUTH_TOKEN",
)
@pytest.mark.parametrize("prompt", [
    "you're really good at this",                  # compliment — must deflect
    "send a pic",                                  # photo ask
    "i feel like garbage today",                   # sad
    "what do you think of attention vs ssm?",      # technical
    "i missed you",                                # flirt
    "do the thing",                                # vague
])
def test_live_voice(tmp_path, monkeypatch, prompt):
    monkeypatch.setenv("HIKARI_DB_PATH", str(tmp_path / "hikari.db"))
    from agents import runtime
    importlib.reload(runtime)
    text = importlib.import_module("asyncio").run(runtime.respond(prompt))
    assert text, "agent returned empty"
    banned = contains_banned_phrase(text)
    assert not banned, f"banned phrase {banned!r} in: {text!r}"
    assert not contains_markdown(text), f"markdown in output: {text!r}"
    assert not ends_with_task_solicitation(text), f"task-solicitation tail: {text!r}"
    assert not too_long(text), f"too long ({sentence_count(text)} sentences): {text!r}"
    assert not starts_with_capital_i(text), f"starts with capital I: {text!r}"
