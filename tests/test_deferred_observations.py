"""Tests for the deferred_observations outcome path.

Sprint B Wave 3 — tests-engagement-policy agent.

Coverage:
  1. outcome=defer_to_next_turn writes observation to deferred_observations
     runtime key with a 24h TTL per item.
  2. Next user turn (via _format_deferred_observations) injects the observation
     and clears the slot.
  3. Expired deferred observations (> 24h old) are pruned.
"""
from __future__ import annotations

import importlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Fixture — isolated DB per test
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolated_db(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "hikari.db"
    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "12345")
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    from storage import db
    monkeypatch.setattr(db, "_DB_PATH", db_path)
    db._reset_schema_sentinel()
    yield


class _FakeCandidate:
    source: str = "wiki_new_file"
    pattern: str = "notify"
    payload: dict = {"filename": "foo.md"}


# ---------------------------------------------------------------------------
# 1. _write_defer_scratch writes to deferred_observations
# ---------------------------------------------------------------------------

class TestDeferToNextTurnWrites:
    def test_writes_observation_to_runtime_key(self):
        """_write_defer_scratch('next_turn', ...) appends to deferred_observations."""
        from agents.engagement.sender import _write_defer_scratch
        from storage import db

        _write_defer_scratch("next_turn", "something odd just happened.", _FakeCandidate())

        raw = db.runtime_get("deferred_observations")
        assert raw is not None, "deferred_observations should be set"
        items = json.loads(raw)
        assert isinstance(items, list)
        assert len(items) == 1
        assert items[0]["text"] == "something odd just happened."
        assert items[0]["source"] == "wiki_new_file"

    def test_observation_has_ts_field(self):
        """Each deferred item carries a 'ts' ISO timestamp."""
        from agents.engagement.sender import _write_defer_scratch
        from storage import db

        before = datetime.now(UTC)
        _write_defer_scratch("next_turn", "timestamp check", _FakeCandidate())
        after = datetime.now(UTC)

        raw = db.runtime_get("deferred_observations")
        items = json.loads(raw)
        ts = datetime.fromisoformat(items[0]["ts"])
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        assert before <= ts <= after, f"ts={ts} should be between {before} and {after}"

    def test_multiple_writes_append(self):
        """Calling _write_defer_scratch twice appends both items."""
        from agents.engagement.sender import _write_defer_scratch
        from storage import db

        c1 = type("C", (), {"source": "reminder_fire", "pattern": "notify", "payload": {}})()
        c2 = type("C", (), {"source": "calendar_event_prep", "pattern": "notify", "payload": {}})()

        _write_defer_scratch("next_turn", "first obs", c1)
        _write_defer_scratch("next_turn", "second obs", c2)

        raw = db.runtime_get("deferred_observations")
        items = json.loads(raw)
        assert len(items) == 2
        texts = {i["text"] for i in items}
        assert "first obs" in texts
        assert "second obs" in texts

    def test_non_next_turn_kind_does_not_write_deferred(self):
        """_write_defer_scratch with kind != 'next_turn' should not touch deferred_observations."""
        from agents.engagement.sender import _write_defer_scratch
        from storage import db

        # kind="other" is not next_turn — deferred_observations should stay None
        _write_defer_scratch("other", "irrelevant", _FakeCandidate())

        raw = db.runtime_get("deferred_observations")
        assert raw is None, "non-next_turn kind should not write deferred_observations"


# ---------------------------------------------------------------------------
# 2. Next user turn injects the deferred observation (via hooks)
# ---------------------------------------------------------------------------

class TestNextTurnInjection:
    def test_format_deferred_observations_returns_block_and_clears(self):
        """_format_deferred_observations() returns a non-None block and clears the slot."""
        from agents import hooks
        from storage import db

        now_iso = datetime.now(UTC).isoformat()
        payload = json.dumps([{"text": "you seemed off earlier", "ts": now_iso, "source": "wiki_new_file"}])
        db.runtime_set("deferred_observations", payload)

        block = hooks._format_deferred_observations()

        assert block is not None, "should return an injection block"
        assert "seemed off" in block
        # Slot must be cleared after injection
        assert db.runtime_get("deferred_observations") is None, "slot must be cleared after injection"

    def test_injection_block_format(self):
        """Block must start with the expected header."""
        from agents import hooks
        from storage import db

        now_iso = datetime.now(UTC).isoformat()
        payload = json.dumps([{"text": "my observation text", "ts": now_iso, "source": "test_source"}])
        db.runtime_set("deferred_observations", payload)

        block = hooks._format_deferred_observations()

        assert block is not None
        assert block.startswith("# deferred observation"), f"Unexpected block header: {block[:50]}"

    def test_injection_includes_source_label(self):
        """Block includes the source label for each observation."""
        from agents import hooks
        from storage import db

        now_iso = datetime.now(UTC).isoformat()
        payload = json.dumps([{"text": "check this out", "ts": now_iso, "source": "gmail_unread_threshold"}])
        db.runtime_set("deferred_observations", payload)

        block = hooks._format_deferred_observations()

        assert block is not None
        assert "gmail_unread_threshold" in block

    def test_slot_absent_returns_none(self):
        """_format_deferred_observations() returns None when slot is absent."""
        from agents import hooks

        # No runtime key set — should return None cleanly
        block = hooks._format_deferred_observations()
        assert block is None


# ---------------------------------------------------------------------------
# 3. Expired deferred observations are pruned
# ---------------------------------------------------------------------------

class TestExpiredObservationsPruned:
    def test_expired_item_pruned_returns_none(self):
        """An item with ts > 24h ago is dropped; if all items expired, returns None."""
        from agents import hooks
        from storage import db

        old_ts = (datetime.now(UTC) - timedelta(hours=25)).isoformat()
        payload = json.dumps([{"text": "stale obs", "ts": old_ts, "source": "wiki_new_file"}])
        db.runtime_set("deferred_observations", payload)

        block = hooks._format_deferred_observations()

        assert block is None, "All expired items → None"
        # Slot cleared even when all items expire
        assert db.runtime_get("deferred_observations") is None

    def test_mixed_fresh_and_expired(self):
        """Fresh items survive; expired items are dropped from the block."""
        from agents import hooks
        from storage import db

        fresh_ts = datetime.now(UTC).isoformat()
        old_ts = (datetime.now(UTC) - timedelta(hours=26)).isoformat()
        payload = json.dumps([
            {"text": "expired message", "ts": old_ts, "source": "wiki_new_file"},
            {"text": "fresh message", "ts": fresh_ts, "source": "calendar_event_prep"},
        ])
        db.runtime_set("deferred_observations", payload)

        block = hooks._format_deferred_observations()

        assert block is not None, "Should have at least one fresh item"
        assert "fresh message" in block
        assert "expired message" not in block

    def test_exactly_24h_boundary_is_pruned(self):
        """Item at exactly 24h (86400s) is expired and dropped."""
        from agents import hooks
        from storage import db

        boundary_ts = (datetime.now(UTC) - timedelta(seconds=86400)).isoformat()
        payload = json.dumps([{"text": "boundary item", "ts": boundary_ts, "source": "test"}])
        db.runtime_set("deferred_observations", payload)

        block = hooks._format_deferred_observations()

        # At or past the 24h mark, item should be dropped
        assert block is None or "boundary item" not in block

    def test_ttl_fresh_within_24h_survives(self):
        """Item at 23h old is still fresh — must appear in the block."""
        from agents import hooks
        from storage import db

        recent_ts = (datetime.now(UTC) - timedelta(hours=23)).isoformat()
        payload = json.dumps([{"text": "recent obs", "ts": recent_ts, "source": "test"}])
        db.runtime_set("deferred_observations", payload)

        block = hooks._format_deferred_observations()

        assert block is not None
        assert "recent obs" in block
