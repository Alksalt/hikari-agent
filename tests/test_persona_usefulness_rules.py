from pathlib import Path

import yaml

PERSONA = Path("assets/PERSONA.md").read_text(encoding="utf-8")
ENGAGEMENT = yaml.safe_load(Path("config/engagement.yaml").read_text(encoding="utf-8"))
COMPOSER = Path("agents/engagement/composer.py").read_text(encoding="utf-8")


def test_persona_has_usefulness_inversion_rule():
    assert "reluctance is words only" in PERSONA


def test_persona_bans_contentless_mystery():
    assert "don't ask." not in PERSONA  # the old leak template is gone
    assert "name its referent" in PERSONA


def test_atmospheric_sources_demoted():
    sources = ENGAGEMENT["proactive"]["default_enabled_sources"]
    assert "weirdly_good_mood_leak" not in sources
    assert "irritation_event" not in sources
    # referent-bearing callbacks stay
    assert "callback_episode" in sources
    assert "research_callback" in sources


def test_wiki_template_reads_instead_of_asking():
    assert "h1" in COMPOSER  # template grounds the message in the page's h1
    assert "want me to read it back at you" not in COMPOSER


def test_persona_routes_jobhunt_radar():
    assert "jobhunt_radar" in PERSONA


def test_persona_bans_application_followup_nudges():
    # Sprint 2 Task 5: owner decision 2026-06-25 — submitted applications
    # never get a follow-up nudge; only outreach touches have cadence.
    assert "applications get no nudges" in PERSONA


def test_persona_bans_fabricated_background_work():
    # 2026-07-04: /jobs turn — jobhunt_radar blew the SDK output cap and
    # Hikari replied "digging through it in the background" with no dispatch
    # call in the turn (tool_calls + background_tasks both confirm). The
    # rail names the only two real background mechanisms.
    assert "never claim background work" in PERSONA
    assert "there is no background me" in PERSONA
