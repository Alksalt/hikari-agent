"""Tests for agents.engagement.producers.wiki_new_file.collect()."""
from __future__ import annotations

import importlib
import time
from pathlib import Path

import pytest


def _reload_all(monkeypatch, tmp_path, wiki_root: Path | None = None):
    """Reload db with a fresh tmp DB, then reload config and the producer."""
    monkeypatch.setenv("HIKARI_DB_PATH", str(tmp_path / "hikari.db"))
    from storage import db
    importlib.reload(db)
    # Clear the process-level schema sentinel so the fresh DB gets migrated.
    db._reset_schema_sentinel()

    # Patch cfg.get to return our wiki_root for wiki_path keys.
    from agents import config as cfg
    _orig_get = cfg.get

    def _patched_get(path, default=None):
        if path in ("wiki_path", "morning_brief.wiki_path"):
            return str(wiki_root) if wiki_root is not None else None
        return _orig_get(path, default)

    monkeypatch.setattr(cfg, "get", _patched_get)

    from agents.engagement.producers import wiki_new_file
    importlib.reload(wiki_new_file)
    return wiki_new_file


def test_collect_returns_candidate_for_new_file(tmp_path, monkeypatch):
    wiki_root = tmp_path / "wiki"
    wiki_root.mkdir()
    producer = _reload_all(monkeypatch, tmp_path, wiki_root)

    # Create a file with a short delay so mtime > last_seen (which defaults to 24h ago)
    md = wiki_root / "test.md"
    md.write_text("# Hello World\nsome content")

    candidates = producer.collect()
    assert len(candidates) == 1
    c = candidates[0]
    assert c.source == "wiki_new_file"
    assert c.pattern == "question"
    assert c.payload["filename"] == "test.md"
    assert "test.md" in c.payload["relative_path"]
    assert c.dedup_key == "wiki_new_file:test.md"


def test_collect_dedupes_after_mark_consumed(tmp_path, monkeypatch):
    wiki_root = tmp_path / "wiki"
    wiki_root.mkdir()
    producer = _reload_all(monkeypatch, tmp_path, wiki_root)

    md = wiki_root / "seen.md"
    md.write_text("# Seen\nbody")

    first = producer.collect()
    assert len(first) == 1
    # Watermark advances only when the scheduler calls mark_consumed
    # AFTER a successful send — simulate that here.
    producer.mark_consumed(first[0])

    second = producer.collect()
    assert second == []


def test_collect_does_not_advance_without_mark_consumed(tmp_path, monkeypatch):
    """Guard-rejected or send-failed candidates must remain eligible next tick."""
    wiki_root = tmp_path / "wiki"
    wiki_root.mkdir()
    producer = _reload_all(monkeypatch, tmp_path, wiki_root)

    md = wiki_root / "ungated.md"
    md.write_text("# Ungated\nbody")

    first = producer.collect()
    assert len(first) == 1
    # No mark_consumed → next tick still sees the file.
    second = producer.collect()
    assert len(second) == 1
    assert second[0].payload["filename"] == "ungated.md"


def test_collect_respects_max_per_24h(tmp_path, monkeypatch):
    wiki_root = tmp_path / "wiki"
    wiki_root.mkdir()
    producer = _reload_all(monkeypatch, tmp_path, wiki_root)

    # Create 5 files
    for i in range(5):
        (wiki_root / f"file{i}.md").write_text(f"# File {i}")

    candidates = producer.collect()
    # cap=2, so at most 2 candidates returned
    assert len(candidates) <= 2


def test_collect_handles_missing_wiki_path(tmp_path, monkeypatch):
    # cfg.get returns None AND the VAULT_ROOT fallback points at a path
    # that doesn't exist — producer must no-op cleanly without crashing.
    producer = _reload_all(monkeypatch, tmp_path, wiki_root=None)
    from tools.wiki import _shared as ws
    monkeypatch.setattr(ws, "VAULT_ROOT", tmp_path / "nonexistent_vault")
    candidates = producer.collect()
    assert candidates == []
