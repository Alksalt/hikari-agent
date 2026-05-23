"""Regression: reengage_silence producer dedup must survive its own proactive send.

After Phase 4A, every proactive send writes a `messages` row. The reengage
producer used to anchor on recent_messages(limit=1) — which would have flipped
to point at the reengage's own row, breaking dedup. The anchor is now
runtime_state['last_user_message']. This test pins that contract.
"""
import importlib
from datetime import UTC
from pathlib import Path
from unittest.mock import patch

import pytest

import storage.db as db_mod


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "hikari.db"
    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
    importlib.reload(db_mod)
    db_mod._reset_schema_sentinel()
    monkeypatch.setattr(db_mod, "_DB_PATH", db_path)
    yield


@pytest.mark.asyncio
async def test_reengage_dedup_holds_after_proactive_send():
    # Seed user last-seen at 4 hours ago.
    from datetime import datetime, timedelta
    four_hours_ago = (datetime.now(UTC) - timedelta(hours=4)).isoformat()
    db_mod.runtime_set("last_user_message", four_hours_ago)

    from agents.engagement.producers import reengage_silence
    importlib.reload(reengage_silence)

    with (
        patch("agents.config.get", return_value=True),
        patch("agents.config.section", return_value={
            "reengage_min_hours": 2, "reengage_max_hours": 6,
            "quiet_start_hour": 23, "quiet_end_hour": 8,
        }),
        patch.object(reengage_silence, "_is_quiet_now", return_value=False),
    ):
        candidates = reengage_silence.collect()
        assert len(candidates) == 1, "first collect should emit one candidate"

        reengage_silence.mark_consumed(candidates[0])

        # Simulate the reengage having been sent: writes an assistant row.
        db_mod.append_message("assistant", "still up?", source="proactive")

        # Second collect (same silence window, no new user message) must NOT
        # re-emit a candidate.
        candidates_again = reengage_silence.collect()

    assert candidates_again == [], (
        "reengage dedup must hold across its own proactive persist — "
        f"got {candidates_again!r}"
    )
