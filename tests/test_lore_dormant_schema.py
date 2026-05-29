"""tests/test_lore_dormant_schema.py — schema-validation tests for LORE_DORMANT.md.

This module validates ONLY the frontmatter schema of
.claude/skills/character-voice/LORE_DORMANT.md.  It does NOT test runtime
behaviour — there is no Python enforcement of the keyword triggers or
min_turns thresholds documented in the frontmatter.  Those gates are
model-discretion heuristics: the model reads the file and honours the intent;
no runtime code checks keywords or counts turns before surfacing dormant facts.

Spec: LORE_DORMANT.md must have YAML frontmatter with a top-level `triggers`
mapping. Each trigger entry must have:
  - `keywords`: a non-empty list of strings
  - `min_turns`: a positive integer

The five fact categories expected (by trigger key):
  - research_paper_failure
  - late_night_music
  - place_city
  - rain_weather
  - crying_vulnerability
"""
from __future__ import annotations

from pathlib import Path

import pytest


_LORE_DORMANT_PATH = (
    Path(__file__).parent.parent
    / ".claude" / "skills" / "character-voice" / "LORE_DORMANT.md"
)

_EXPECTED_TRIGGERS = {
    "research_paper_failure",
    "late_night_music",
    "place_city",
    "rain_weather",
    "crying_vulnerability",
}


def _parse_frontmatter(text: str) -> dict:
    """Extract YAML frontmatter from a markdown file.

    Returns parsed dict or raises ValueError if not found.

    Normalises the ``triggers`` value: the file uses a YAML sequence of
    single-key mappings (``- key: {…}``) for ordered output.  We convert
    that to a plain ``{key: {…}}`` dict so the rest of the tests can do
    simple dict look-ups.
    """
    import yaml  # PyYAML is a project dependency via uv

    if not text.startswith("---"):
        raise ValueError("No frontmatter delimiter found at start of file")
    end_idx = text.find("---", 3)
    if end_idx == -1:
        raise ValueError("Closing frontmatter delimiter '---' not found")
    yaml_block = text[3:end_idx].strip()
    data: dict = yaml.safe_load(yaml_block) or {}

    # Normalise list-of-single-key-dicts  →  plain dict
    # e.g. [{"research_paper_failure": {...}}, ...]  →  {"research_paper_failure": {...}}
    raw_triggers = data.get("triggers")
    if isinstance(raw_triggers, list):
        normalised: dict = {}
        for item in raw_triggers:
            if isinstance(item, dict):
                normalised.update(item)
        data["triggers"] = normalised

    return data


# ---------------------------------------------------------------------------
# 1. File exists
# ---------------------------------------------------------------------------

def test_lore_dormant_file_exists():
    assert _LORE_DORMANT_PATH.exists(), (
        f"LORE_DORMANT.md not found at {_LORE_DORMANT_PATH}"
    )


# ---------------------------------------------------------------------------
# 2. Frontmatter is valid YAML
# ---------------------------------------------------------------------------

def test_frontmatter_parses():
    text = _LORE_DORMANT_PATH.read_text(encoding="utf-8")
    data = _parse_frontmatter(text)
    assert isinstance(data, dict), "Frontmatter must be a YAML mapping"


# ---------------------------------------------------------------------------
# 3. Top-level `triggers` key exists
# ---------------------------------------------------------------------------

def test_frontmatter_has_triggers_key():
    text = _LORE_DORMANT_PATH.read_text(encoding="utf-8")
    data = _parse_frontmatter(text)
    assert "triggers" in data, "Frontmatter must have a top-level 'triggers' key"


# ---------------------------------------------------------------------------
# 4. All five expected trigger keys are present
# ---------------------------------------------------------------------------

def test_all_five_triggers_present():
    text = _LORE_DORMANT_PATH.read_text(encoding="utf-8")
    data = _parse_frontmatter(text)
    triggers = data.get("triggers") or {}
    trigger_keys = set(triggers.keys())
    missing = _EXPECTED_TRIGGERS - trigger_keys
    assert not missing, (
        f"Missing trigger keys in LORE_DORMANT.md frontmatter: {missing}"
    )


# ---------------------------------------------------------------------------
# 5. Each trigger has `keywords` (non-empty list of strings)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("trigger_key", sorted(_EXPECTED_TRIGGERS))
def test_trigger_has_keywords(trigger_key):
    text = _LORE_DORMANT_PATH.read_text(encoding="utf-8")
    data = _parse_frontmatter(text)
    triggers = data.get("triggers") or {}
    entry = triggers.get(trigger_key) or {}
    keywords = entry.get("keywords")
    assert keywords, (
        f"Trigger '{trigger_key}' must have a non-empty 'keywords' list"
    )
    assert isinstance(keywords, list), (
        f"Trigger '{trigger_key}'.keywords must be a list"
    )
    assert all(isinstance(k, str) for k in keywords), (
        f"Trigger '{trigger_key}'.keywords must contain only strings"
    )
    assert len(keywords) >= 1


# ---------------------------------------------------------------------------
# 6. Each trigger has `min_turns` (positive integer)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("trigger_key", sorted(_EXPECTED_TRIGGERS))
def test_trigger_has_min_turns(trigger_key):
    text = _LORE_DORMANT_PATH.read_text(encoding="utf-8")
    data = _parse_frontmatter(text)
    triggers = data.get("triggers") or {}
    entry = triggers.get(trigger_key) or {}
    min_turns = entry.get("min_turns")
    assert min_turns is not None, (
        f"Trigger '{trigger_key}' must have a 'min_turns' field"
    )
    assert isinstance(min_turns, int), (
        f"Trigger '{trigger_key}'.min_turns must be an integer"
    )
    assert min_turns >= 1, (
        f"Trigger '{trigger_key}'.min_turns must be a positive integer (got {min_turns})"
    )


# ---------------------------------------------------------------------------
# 7. Crying-vulnerability has highest min_turns (most gated)
# ---------------------------------------------------------------------------

def test_crying_vulnerability_is_most_gated():
    """Crying is the most sensitive lore — spec says min_turns 8."""
    text = _LORE_DORMANT_PATH.read_text(encoding="utf-8")
    data = _parse_frontmatter(text)
    triggers = data.get("triggers") or {}
    crying_turns = (triggers.get("crying_vulnerability") or {}).get("min_turns", 0)
    all_turns = [
        (triggers.get(k) or {}).get("min_turns", 0)
        for k in _EXPECTED_TRIGGERS
        if k != "crying_vulnerability"
    ]
    assert crying_turns >= max(all_turns, default=0), (
        "crying_vulnerability should have the highest (or equal) min_turns threshold"
    )


# ---------------------------------------------------------------------------
# 8. rain_weather min_turns is 1 (lightest gate)
# ---------------------------------------------------------------------------

def test_rain_weather_min_turns_is_1():
    """Rain is the lightest fact — spec explicitly sets min_turns=1."""
    text = _LORE_DORMANT_PATH.read_text(encoding="utf-8")
    data = _parse_frontmatter(text)
    triggers = data.get("triggers") or {}
    rain_turns = (triggers.get("rain_weather") or {}).get("min_turns")
    assert rain_turns == 1, (
        f"rain_weather min_turns should be 1 (lightest gate), got {rain_turns}"
    )
