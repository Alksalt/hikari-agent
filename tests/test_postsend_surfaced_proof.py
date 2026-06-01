"""postsend.mark_pending_surfaced: surfaced-proof substring matching.

- Empty sent_text → all pending IDs re-stashed (not marked surfaced).
- Non-empty sent_text with match → observation/noticing marked surfaced.
- Non-empty sent_text without match → ID re-stashed for next turn.
- Multiple pending IDs: each independently matched/restashed.
"""
from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

from storage import db

# ---------------------------------------------------------------------------
# DB isolation
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolated_db(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "hikari.db"
    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "12345")
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    monkeypatch.setattr(db, "_DB_PATH", db_path)
    yield


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

OBS_KEY = "pending_surfaced_observation_ids"
NOT_KEY = "pending_surfaced_noticing_ids"


def _stash_obs(ids: list[int]) -> None:
    db.runtime_set(OBS_KEY, json.dumps(ids))


def _stash_not(ids: list[int]) -> None:
    db.runtime_set(NOT_KEY, json.dumps(ids))


def _pending_obs() -> list[int]:
    raw = db.runtime_get(OBS_KEY)
    if not raw:
        return []
    return json.loads(raw)


def _pending_not() -> list[int]:
    raw = db.runtime_get(NOT_KEY)
    if not raw:
        return []
    return json.loads(raw)


def _insert_observation(summary: str) -> int:
    """Insert a minimal observation row and return its id."""
    with db._conn() as c:
        cur = c.execute(
            "INSERT INTO observations (kind, signature, summary, created_at) "
            "VALUES ('pattern_break', ?, ?, datetime('now'))",
            (f"sig-{summary}", summary),
        )
        return cur.lastrowid


def _insert_noticing(summary: str) -> int:
    """Insert a minimal noticing row and return its id."""
    with db._conn() as c:
        cur = c.execute(
            "INSERT INTO noticings (signal, summary, created_at) "
            "VALUES ('sentiment_drop', ?, datetime('now'))",
            (summary,),
        )
        return cur.lastrowid


def _obs_surfaced(obs_id: int) -> bool:
    """observations: surfaced when last_surfaced_at IS NOT NULL."""
    with db._conn() as c:
        row = c.execute("SELECT last_surfaced_at FROM observations WHERE id=?", (obs_id,)).fetchone()
    return bool(row and row["last_surfaced_at"] is not None)


def _not_surfaced(not_id: int) -> bool:
    """noticings: surfaced when surfaced_at IS NOT NULL."""
    with db._conn() as c:
        row = c.execute("SELECT surfaced_at FROM noticings WHERE id=?", (not_id,)).fetchone()
    return bool(row and row["surfaced_at"] is not None)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_empty_sent_text_restashes_all():
    """sent_text='' → all IDs re-stashed, nothing marked surfaced."""
    obs_id = _insert_observation("you seem tired today")
    not_id = _insert_noticing("you went quiet earlier")
    _stash_obs([obs_id])
    _stash_not([not_id])

    from agents.postsend import mark_pending_surfaced
    mark_pending_surfaced("")

    assert not _obs_surfaced(obs_id)
    assert not _not_surfaced(not_id)
    assert obs_id in _pending_obs()
    assert not_id in _pending_not()


def test_matching_text_marks_surfaced():
    """sent_text contains observation summary → marked surfaced, not restashed."""
    obs_id = _insert_observation("you seem tired today")
    _stash_obs([obs_id])

    from agents.postsend import mark_pending_surfaced
    mark_pending_surfaced("i noticed — you seem tired today. you okay?")

    assert _obs_surfaced(obs_id)
    assert _pending_obs() == []


def test_non_matching_text_restashes():
    """sent_text does not contain summary → not marked surfaced, ID re-stashed."""
    obs_id = _insert_observation("you seem tired today")
    _stash_obs([obs_id])

    from agents.postsend import mark_pending_surfaced
    mark_pending_surfaced("completely unrelated message about shipping a feature")

    assert not _obs_surfaced(obs_id)
    assert obs_id in _pending_obs()


def test_case_insensitive_normalization():
    """Matching is case-insensitive and whitespace-normalized."""
    obs_id = _insert_observation("You Went  Quiet")
    _stash_obs([obs_id])

    from agents.postsend import mark_pending_surfaced
    mark_pending_surfaced("you went quiet earlier, that's disruptive.")

    assert _obs_surfaced(obs_id)


def test_mixed_ids_independently_matched():
    """Multiple IDs: one matches → surfaced; one doesn't → restashed."""
    obs_match = _insert_observation("shipped the prototype")
    obs_miss = _insert_observation("you seem worried about the deadline")
    _stash_obs([obs_match, obs_miss])

    from agents.postsend import mark_pending_surfaced
    mark_pending_surfaced("nice — you shipped the prototype. about time.")

    assert _obs_surfaced(obs_match)
    assert not _obs_surfaced(obs_miss)
    assert obs_miss in _pending_obs()
    assert obs_match not in _pending_obs()


def test_noticings_same_semantics():
    """Noticings follow the same match/restash logic as observations."""
    not_id = _insert_noticing("went quiet for two days")
    _stash_not([not_id])

    from agents.postsend import mark_pending_surfaced
    mark_pending_surfaced("i noticed — you went quiet for two days.")

    assert _not_surfaced(not_id)
    assert _pending_not() == []
