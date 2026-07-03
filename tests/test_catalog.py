"""Tests for tools/catalog.py — BM25 semantic tool catalog.

Requirements from sprints.md Sprint A row 24:
  - "email" → gmail tools in top 3
  - "receipt", "youtube", "weather", "calendar", "wiki", "github", "notion"
    all rank correctly (domain-correct tools in top 3)
"""
from __future__ import annotations

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_catalog_singleton():
    """Reset the module-level singleton between tests."""
    from tools.catalog import _reset_catalog
    _reset_catalog()
    yield
    _reset_catalog()


def top3_names(query: str) -> list[str]:
    from tools.catalog import get_catalog
    return [e.name for e in get_catalog().search(query, k=3)]


def top3_domains(query: str) -> list[str]:
    from tools.catalog import get_catalog
    return [e.domain for e in get_catalog().search(query, k=3)]


# ---------------------------------------------------------------------------
# Smoke: catalog loads and indexes correctly
# ---------------------------------------------------------------------------

def test_catalog_loads():
    from tools.catalog import get_catalog
    cat = get_catalog()
    assert len(cat.entries) > 50, "Expected >50 tool entries"


def test_catalog_has_bucket_field():
    from tools.catalog import get_catalog
    cat = get_catalog()
    buckets = {e.bucket for e in cat.entries}
    assert 1 in buckets
    assert 3 in buckets


def test_catalog_search_returns_entries():
    from tools.catalog import get_catalog
    results = get_catalog().search("email", k=5)
    assert len(results) > 0
    assert all(hasattr(r, "name") for r in results)


def test_catalog_search_k_limit():
    from tools.catalog import get_catalog
    results = get_catalog().search("email", k=2)
    assert len(results) <= 2


# ---------------------------------------------------------------------------
# Correctness: domain-specific queries
# ---------------------------------------------------------------------------

def test_email_top3_are_gmail():
    names = top3_names("email")
    assert any("gmail" in n or "google_workspace" in n for n in names), (
        f"Expected gmail/google tools in top-3 for 'email', got: {names}"
    )


def test_email_all_top3_gmail():
    """All top-3 results for 'email' should be gmail/google tools."""
    from tools.catalog import get_catalog
    results = get_catalog().search("email", k=3)
    for entry in results:
        assert "gmail" in entry.name or "google_workspace" in entry.name or entry.domain in ("gmail", "google"), (
            f"Non-gmail tool ranked in top-3 for 'email': {entry.name} (domain={entry.domain})"
        )


def test_receipt_top3_are_receipt():
    from tools.catalog import get_catalog
    results = get_catalog().search("receipt", k=3)
    names = [e.name for e in results]
    assert any("receipt" in n for n in names), (
        f"Expected receipt tools in top-3, got: {names}"
    )


def test_receipt_all_top3_receipt():
    from tools.catalog import get_catalog
    results = get_catalog().search("receipt", k=3)
    for entry in results:
        assert "receipt" in entry.name or entry.domain in ("tracking", "receipt"), (
            f"Non-receipt tool in top-3 for 'receipt': {entry.name}"
        )


def test_youtube_top3_contains_youtube_tool():
    names = top3_names("youtube")
    assert any(
        "ytmusic" in n
        for n in names
    ), f"Expected ytmusic tool in top-3 for 'youtube', got: {names}"


def test_weather_top1_is_weather():
    from tools.catalog import get_catalog
    results = get_catalog().search("weather", k=1)
    assert len(results) == 1
    assert "weather" in results[0].name, (
        f"Expected weather_fetch as top result, got: {results[0].name}"
    )


def test_weather_top3_starts_with_weather():
    names = top3_names("weather")
    assert names[0] == "mcp__hikari_utility__weather_fetch", (
        f"Expected weather_fetch as #1 for 'weather', got: {names}"
    )


def test_calendar_top3_contains_calendar():
    names = top3_names("calendar")
    assert any("calendar" in n for n in names), (
        f"Expected calendar tool in top-3, got: {names}"
    )


def test_calendar_top1_is_calendar():
    from tools.catalog import get_catalog
    results = get_catalog().search("calendar", k=1)
    assert "calendar" in results[0].name, (
        f"Expected calendar tool as top result, got: {results[0].name}"
    )


def test_wiki_top3_are_wiki():
    from tools.catalog import get_catalog
    results = get_catalog().search("wiki", k=3)
    for entry in results:
        assert entry.domain == "wiki" or "wiki" in entry.name, (
            f"Non-wiki tool in top-3 for 'wiki': {entry.name}"
        )


def test_github_top3_are_github():
    from tools.catalog import get_catalog
    results = get_catalog().search("github", k=3)
    for entry in results:
        assert "github" in entry.name or entry.domain == "github", (
            f"Non-github tool in top-3 for 'github': {entry.name}"
        )


def test_notion_top3_are_notion():
    from tools.catalog import get_catalog
    results = get_catalog().search("notion", k=3)
    for entry in results:
        assert "notion" in entry.name or entry.domain == "notion", (
            f"Non-notion tool in top-3 for 'notion': {entry.name}"
        )


# ---------------------------------------------------------------------------
# ToolEntry shape
# ---------------------------------------------------------------------------

def test_entry_has_required_fields():
    from tools.catalog import get_catalog
    cat = get_catalog()
    entry = cat.entries[0]
    assert entry.name
    assert isinstance(entry.description, str)
    assert isinstance(entry.domain, str)
    assert isinstance(entry.operation, str)
    assert isinstance(entry.risk_tier, str)
    assert isinstance(entry.credentials, list)
    assert isinstance(entry.examples, list)
    assert isinstance(entry.tags, list)
    assert isinstance(entry.bucket, int)


def test_entry_tags_not_empty():
    from tools.catalog import get_catalog
    cat = get_catalog()
    # All entries should have at least one tag
    empty_tags = [e.name for e in cat.entries if not e.tags]
    assert not empty_tags, f"Entries with empty tags: {empty_tags[:5]}"


# ---------------------------------------------------------------------------
# Singleton behaviour
# ---------------------------------------------------------------------------

def test_singleton_returns_same_object():
    from tools.catalog import get_catalog
    a = get_catalog()
    b = get_catalog()
    assert a is b, "get_catalog() should return the same Catalog instance"


def test_reset_clears_singleton():
    from tools.catalog import _reset_catalog, get_catalog
    a = get_catalog()
    _reset_catalog()
    b = get_catalog()
    assert a is not b, "get_catalog() should return a new Catalog after reset"


# ---------------------------------------------------------------------------
# tools.yaml `example:` (singular) must reach ToolEntry.examples
# ---------------------------------------------------------------------------

def test_yaml_singular_example_reaches_entries():
    """tools.yaml uses `example:` (singular str); the catalog must pick it up
    AND keep the synthesized natural-language asks for BM25 recall."""
    from tools.catalog import _entries_from_registry
    entries = {e.name: e for e in _entries_from_registry()}
    entry = entries["mcp__hikari_utility__reminder_list"]
    assert "reminder_list()" in entry.examples          # yaml example: value
    # NL default must survive the merge (description mentions reminders → no
    # keyword hit in _default_examples, so just assert yaml value is present
    # and examples is a list)
    assert isinstance(entry.examples, list)
