"""Phase J cleanup regression tests.

Confirms that every deletion and structural change from Phase J is complete:
  1. Compat shims can_send_proactive / record_proactive_sent are gone from cadence.
  2. proactive_events.chat_id column exists and the silence-window filter works.
  3. maybe_send_heartbeat / maybe_send_reengagement / maybe_send_calendar_heartbeat
     are gone from proactive.
  4. config/scopes.yaml is deleted.
  5. tools.yaml has the auth_providers top-level block.
  6. tools.yaml has per-tool scopes blocks parsed into ToolSpec.
"""
from __future__ import annotations

import importlib
import sqlite3
from pathlib import Path

import pytest

from agents import config
from storage import db


# ---------------------------------------------------------------------------
# Shared isolated-DB fixture
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolated_db(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "hikari.db"
    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "12345")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    monkeypatch.setattr(db, "_DB_PATH", db_path)
    config.reload()
    yield


# ---------------------------------------------------------------------------
# 1+2. Compat shims absent from cadence
# ---------------------------------------------------------------------------

def test_can_send_proactive_compat_shim_absent():
    """can_send_proactive must not exist on the cadence module post-Phase J."""
    from agents import cadence
    assert not hasattr(cadence, "can_send_proactive"), (
        "can_send_proactive compat shim still present in agents/cadence.py"
    )


def test_record_proactive_sent_compat_shim_absent():
    """record_proactive_sent must not exist on the cadence module post-Phase J."""
    from agents import cadence
    assert not hasattr(cadence, "record_proactive_sent"), (
        "record_proactive_sent compat shim still present in agents/cadence.py"
    )


# ---------------------------------------------------------------------------
# 3+4. proactive_events.chat_id column + silence-window chat_id filter
# ---------------------------------------------------------------------------

def test_proactive_events_table_has_chat_id_column():
    """Phase J migration must add chat_id column to proactive_events table."""
    # Touch the DB (autouse fixture already inits it via reload).
    with db._conn() as conn:
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(proactive_events)").fetchall()}
    assert "chat_id" in cols, "proactive_events missing chat_id column after Phase J migration"


def test_proactive_event_silence_window_filters_by_chat_id():
    """record_silence_window with chat_id only silences rows for that chat."""
    # Insert two events for different chat_ids.
    db.proactive_event_insert(
        source="open_loop", pattern="test", payload_json="{}",
        telegram_message_id=1, chat_id=111,
    )
    db.proactive_event_insert(
        source="open_loop", pattern="test", payload_json="{}",
        telegram_message_id=2, chat_id=222,
    )

    # Silence only chat 111.
    silenced = db.proactive_event_record_silence_window(chat_id=111)
    assert silenced == 1, f"Expected 1 row silenced for chat 111, got {silenced}"

    # Verify chat 222 row is NOT silenced.
    with db._conn() as conn:
        rows = conn.execute(
            "SELECT chat_id, silenced_within_1h FROM proactive_events ORDER BY rowid"
        ).fetchall()
    by_chat = {r["chat_id"]: r["silenced_within_1h"] for r in rows}
    assert by_chat.get(111) == 1, "chat 111 row should be silenced"
    assert by_chat.get(222) in (0, None), "chat 222 row should NOT be silenced"


# ---------------------------------------------------------------------------
# 5–7. Deleted send functions absent from proactive
# ---------------------------------------------------------------------------

def test_maybe_send_heartbeat_deleted():
    """maybe_send_heartbeat must not exist in agents.proactive post-Phase J."""
    from agents import proactive
    assert not hasattr(proactive, "maybe_send_heartbeat"), (
        "maybe_send_heartbeat still present in agents/proactive.py"
    )


def test_maybe_send_reengagement_deleted():
    """maybe_send_reengagement must not exist in agents.proactive post-Phase J."""
    from agents import proactive
    assert not hasattr(proactive, "maybe_send_reengagement"), (
        "maybe_send_reengagement still present in agents/proactive.py"
    )


def test_maybe_send_calendar_heartbeat_deleted():
    """maybe_send_calendar_heartbeat must not exist in agents.proactive post-Phase J."""
    from agents import proactive
    assert not hasattr(proactive, "maybe_send_calendar_heartbeat"), (
        "maybe_send_calendar_heartbeat still present in agents/proactive.py"
    )


# ---------------------------------------------------------------------------
# 8. config/scopes.yaml deleted
# ---------------------------------------------------------------------------

def test_scopes_yaml_deleted():
    """config/scopes.yaml must not exist — merged into config/tools.yaml in Phase J."""
    from tools._tools_yaml import REPO_ROOT
    scopes_path = REPO_ROOT / "config" / "scopes.yaml"
    assert not scopes_path.exists(), (
        f"config/scopes.yaml still exists at {scopes_path} — Phase J merge incomplete"
    )


# ---------------------------------------------------------------------------
# 9+10. tools.yaml has auth_providers block + per-tool scopes
# ---------------------------------------------------------------------------

def test_tools_yaml_has_auth_providers_block():
    """tools.yaml must expose an auth_providers block via the registry."""
    from tools._tools_yaml import load_registry
    reg = load_registry()
    providers = reg.auth_providers()
    assert providers, "auth_providers block missing from tools.yaml / registry"
    assert "google" in providers, "google provider missing from auth_providers"
    google_cfg = providers["google"]
    assert "provider_class" in google_cfg, "google provider_class missing"
    assert "voice_template" in google_cfg, "google voice_template missing"


def test_tools_yaml_has_per_tool_scopes():
    """At least one tool spec must carry scopes_provider + scopes_required after merge."""
    from tools._tools_yaml import load_registry
    reg = load_registry()
    scoped = [s for s in reg.specs() if s.scopes_provider]
    assert scoped, "No tool specs have scopes_provider — per-tool scopes block not parsed"
    # Spot-check: gmail_send_email should require gmail.modify.
    gmail_spec = reg.spec("mcp__google_workspace__gmail_send_email")
    assert gmail_spec is not None, "mcp__google_workspace__gmail_send_email not in registry"
    assert gmail_spec.scopes_provider == "google"
    assert any("gmail" in s for s in gmail_spec.scopes_required), (
        f"Expected a gmail scope in {gmail_spec.scopes_required}"
    )
