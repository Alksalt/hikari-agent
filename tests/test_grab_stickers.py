"""scripts/grab_stickers.py — one-shot sticker file_id harvester.

Phase 5b moved the /grab_stickers capture flow out of the bridge into a
standalone script. These tests cover the pure capture logic:

  - add_to_pool dedupes file_ids
  - yaml_snippet emits dict format with description placeholders
  - yaml_snippet escapes quotes/backslashes defensively
"""
from __future__ import annotations

from scripts.grab_stickers import add_to_pool, yaml_snippet


def test_add_to_pool_appends_new_file_id():
    pool: list[str] = []
    assert add_to_pool(pool, "CAACAgIAAxkBAAE1") is True
    assert pool == ["CAACAgIAAxkBAAE1"]


def test_add_to_pool_dedupes():
    pool = ["CAACAgIAAxkBAAE1"]
    assert add_to_pool(pool, "CAACAgIAAxkBAAE1") is False
    assert pool == ["CAACAgIAAxkBAAE1"]


def test_add_to_pool_preserves_order():
    pool: list[str] = []
    add_to_pool(pool, "id_b")
    add_to_pool(pool, "id_a")
    add_to_pool(pool, "id_b")  # duplicate
    assert pool == ["id_b", "id_a"]


def test_yaml_snippet_dict_format_with_descriptions():
    """Snippet must emit dict entries with empty description placeholders —
    pasting a flat-string snippet would wipe descriptions and degrade the
    situational LLM picker to random."""
    snippet = yaml_snippet(["id_one", "id_two"])
    lines = snippet.splitlines()
    assert lines[0] == "stickers:"
    assert lines[1] == "  pool:"
    assert '    - file_id: "id_one"' in lines
    assert '    - file_id: "id_two"' in lines
    # one description placeholder per file_id
    desc_lines = [ln for ln in lines if "description:" in ln]
    assert len(desc_lines) == 2
    assert all('""' in ln for ln in desc_lines)


def test_yaml_snippet_escapes_quotes_and_backslashes():
    snippet = yaml_snippet(['weird"id', "back\\slash"])
    assert '- file_id: "weird\\"id"' in snippet
    assert '- file_id: "back\\\\slash"' in snippet


def test_yaml_snippet_empty_pool():
    snippet = yaml_snippet([])
    assert snippet.splitlines() == ["stickers:", "  pool:"]
