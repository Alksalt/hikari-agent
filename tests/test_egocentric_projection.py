"""Phase 11: SPASM Egocentric Context Projection.

Tests for ``agents.ecp.project_egocentric``. The function rewrites
``User:``/``Assistant:`` (third-person dialog log) into
``[partner]:``/``[self]:`` (first-person memory). Reference: arxiv 2604.09212
(ACL 2026), Cohen's d=-0.75 reduction in emotion drift over 18-turn chats.
"""
from __future__ import annotations

import pytest


def test_project_egocentric_basic():
    from agents.ecp import project_egocentric
    text = "User: hey\nAssistant: hi\nUser: how are you"
    out = project_egocentric(text)
    assert "[partner]: hey" in out
    assert "[self]: hi" in out
    assert "User:" not in out
    assert "Assistant:" not in out


def test_project_egocentric_preserves_body():
    from agents.ecp import project_egocentric
    text = "User: i'm sad\nAssistant: that's exhausting."
    out = project_egocentric(text)
    assert "i'm sad" in out
    assert "that's exhausting." in out


def test_project_egocentric_idempotent():
    from agents.ecp import project_egocentric
    text = "[partner]: hey\n[self]: hi"
    assert project_egocentric(text) == text


def test_project_egocentric_handles_mixed_case():
    from agents.ecp import project_egocentric
    text = "USER: yelling\nassistant: quietly"
    out = project_egocentric(text)
    assert "[partner]: yelling" in out
    assert "[self]: quietly" in out


def test_project_egocentric_handles_said_form():
    from agents.ecp import project_egocentric
    text = "in the last session user said they were tired"
    out = project_egocentric(text)
    assert "[partner] said" in out


def test_project_egocentric_handles_assistant_said_form():
    from agents.ecp import project_egocentric
    text = "summary: assistant said it was fine"
    out = project_egocentric(text)
    assert "[self] said" in out


def test_project_egocentric_handles_hikari_and_bot_labels():
    """Older summaries sometimes use 'Hikari:' or 'Bot:' instead of 'Assistant:'."""
    from agents.ecp import project_egocentric
    text = "Hikari: thinking out loud\nBot: alternate phrasing"
    out = project_egocentric(text)
    assert "[self]: thinking out loud" in out
    assert "[self]: alternate phrasing" in out
    assert "Hikari:" not in out
    assert "Bot:" not in out


def test_project_egocentric_empty_input():
    from agents.ecp import project_egocentric
    assert project_egocentric("") == ""


def test_project_egocentric_preserves_non_role_text():
    """The word 'user' inside running prose should NOT be rewritten — only
    line-start label patterns and the explicit '<role> said' forms."""
    from agents.ecp import project_egocentric
    text = "the user interface was clunky and the user-agent string was weird"
    out = project_egocentric(text)
    # No accidental rewrites to '[partner]' inside prose.
    assert "user interface" in out
    assert "user-agent" in out


def test_handoff_format_applies_projection(monkeypatch):
    """Integration: handoff.format_for_injection runs the output through ECP
    when the config flag is on."""
    from agents import config, handoff
    config.reload()
    data = {
        "ts": "2026-05-19T10:00:00+00:00",
        "turns": [
            {"role": "user", "content": "where were we"},
            {"role": "assistant", "content": "i was mid-sentence."},
        ],
    }
    out = handoff.format_for_injection(data)
    assert "[partner]: where were we" in out
    assert "[self]: i was mid-sentence." in out
    assert "USER:" not in out
    assert "ASSISTANT:" not in out


def test_maybe_project_respects_config_off(tmp_path, monkeypatch):
    """When persona.egocentric_projection is false, the projection is a no-op."""
    cfg_path = tmp_path / "engagement.yaml"
    cfg_path.write_text(
        "persona:\n  egocentric_projection: false\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HIKARI_CONFIG_PATH", str(cfg_path))
    from agents import config, ecp
    config.reload()
    try:
        text = "User: hey\nAssistant: hi"
        # maybe_project returns the input unchanged when disabled.
        assert ecp.maybe_project(text) == text
        # The pure function still works (it's the config gate that's off).
        assert "[partner]:" in ecp.project_egocentric(text)
    finally:
        config.reload()


def test_maybe_project_defaults_on(tmp_path, monkeypatch):
    """When the persona section is missing, the default is ON (projection applied)."""
    cfg_path = tmp_path / "engagement.yaml"
    cfg_path.write_text("typing:\n  base_sec: 1.5\n", encoding="utf-8")
    monkeypatch.setenv("HIKARI_CONFIG_PATH", str(cfg_path))
    from agents import config, ecp
    config.reload()
    try:
        text = "User: hey\nAssistant: hi"
        out = ecp.maybe_project(text)
        assert "[partner]: hey" in out
        assert "[self]: hi" in out
    finally:
        config.reload()
