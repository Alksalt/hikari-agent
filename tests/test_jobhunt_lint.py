"""Tests for tools/jobhunt/lint.py — deterministic language-rails scan for
touch-email drafts (Sprint 2, Task 4).

``check(text)`` is a pure regex/substring scan (no LLM judgment) that gates
every composed email before a Gmail draft is created — see
tools/jobhunt/drafter.py. ANY hit blocks the draft. This file proves every
banned pattern is caught, clean bokmål text passes clean, and the
private-repo do-not-cite list resolves correctly (parsed from
candidate_profile.md at call time, falling back to cfg jobhunt.private_repo_names,
falling back further to a hardcoded default).
"""
from __future__ import annotations

import pytest

from agents import config as cfg
from tools.jobhunt import lint


def _patch_cfg(monkeypatch, **overrides):
    orig_get = cfg.get

    def fake_get(key, default=None):
        if key in overrides:
            return overrides[key]
        return orig_get(key, default)

    monkeypatch.setattr(cfg, "get", fake_get)


@pytest.fixture(autouse=True)
def _isolate_job_search_root(monkeypatch, tmp_path):
    """Point jobhunt.roots.job_search at a directory that doesn't exist by
    default, so check() falls through to the cfg/default private-repo list
    — no test here should depend on the real dev machine's
    candidate_profile.md (which happens to exist on this box but must not
    be a silent dependency of the suite)."""
    _patch_cfg(monkeypatch, **{"jobhunt.roots.job_search": str(tmp_path / "no-job-search")})


CLEAN_TEXT = (
    "SUBJECT: Kort oppfolging\n\n"
    "Hei Kari,\n\n"
    "Jeg leste om satsingen deres pa digital hjemmeoppfolging og tenkte det "
    "passet godt med det jeg selv jobber med i Helseplattformen. Har bygget "
    "noen AI-verktoy pa siden og lurer pa om det er rom for en kort prat.\n\n"
    "Mvh Oleksandr"
)


# --------------------------------------------------------------------------
# clean text passes
# --------------------------------------------------------------------------

def test_clean_bokmal_text_passes():
    assert lint.check(CLEAN_TEXT) == []


# --------------------------------------------------------------------------
# individual patterns
# --------------------------------------------------------------------------

def test_semicolon_caught():
    hits = lint.check("Hei; dette er en test.")
    assert any("semicolon" in h.lower() for h in hits)


def test_bare_b2_passes_but_b2_plus_is_caught():
    assert lint.check("Jeg har B2 i norsk.") == []
    hits = lint.check("Jeg har B2+ i norsk.")
    assert any("b2+" in h.lower() for h in hits)


def test_flyktning_caught():
    hits = lint.check("Jeg kom til Norge som flyktning.")
    assert any("flyktning" in h.lower() for h in hits)


@pytest.mark.parametrize("word", ["visum", "visa", "oppholdstillatelse", "immigration"])
def test_visa_family_caught(word):
    hits = lint.check(f"Dette handler om {word} og liknende.")
    assert hits, f"{word!r} should have been caught"


def test_avisa_does_not_trigger_visa_rule():
    """Bokmal 'avisa' (the newspaper — a natural touch-1 new-angle word)
    contains 'visa' as a substring but must not trip the visa rule, which
    is left-word-bounded for exactly this reason."""
    assert lint.check("Jeg leste i avisa at dere satser pa e-helse.") == []


def test_visasoknad_still_caught():
    """Left-bounded only — 'visasøknad' (a compound starting with 'visa')
    must still hit."""
    hits = lint.check("Dette gjelder en visasøknad.")
    assert any("visa" in h.lower() for h in hits)


def test_year_2027_caught():
    hits = lint.check("Lisensen min gjelder til 2027.")
    assert any("2027" in h for h in hits)


def test_year_2027_substring_not_falsely_caught():
    """`\\b2027\\b` must not fire on a longer number that merely contains
    2027 as a substring (e.g. a phone number or an unrelated id)."""
    assert lint.check("Referanse 20270001 er registrert.") == []


def test_nynorsk_ikkje_caught():
    hits = lint.check("Eg veit ikkje heilt.")
    assert any("ikkje" in h.lower() for h in hits)


def test_nynorsk_korleis_caught():
    hits = lint.check("Korleis gar det?")
    assert any("korleis" in h.lower() for h in hits)


def test_nynorsk_eg_caught():
    hits = lint.check("Eg lurer pa noe.")
    assert any("'eg'" in h.lower() for h in hits)


def test_jeg_does_not_trigger_eg_rule():
    """The 'eg' giveaway must be word-bounded — 'jeg' (bokmal 'I') is not
    nynorsk and must never be flagged."""
    hits = lint.check("Jeg lurer pa noe, og jeg tror det passer bra.")
    assert not any("eg" in h.lower() for h in hits)


# --------------------------------------------------------------------------
# private-repo do-not-cite list
# --------------------------------------------------------------------------

def test_private_repo_name_caught_via_cfg_fallback(monkeypatch, tmp_path):
    _patch_cfg(monkeypatch, **{
        "jobhunt.roots.job_search": str(tmp_path / "no-job-search"),
        "jobhunt.private_repo_names": ["NorMedBench", "fhir-safety-harness"],
    })
    hits = lint.check("Sjekk ut NorMedBench-prosjektet mitt.")
    assert any("normedbench" in h.lower() for h in hits)


def test_private_repo_name_case_insensitive():
    hits = lint.check("Jeg jobbet pa TG-BOT-LOGGER i fjor.")
    assert any("tg-bot-logger" in h.lower() for h in hits)


def test_private_repo_name_parsed_from_candidate_profile(monkeypatch, tmp_path):
    job_search_dir = tmp_path / "job-search"
    job_search_dir.mkdir()
    (job_search_dir / "candidate_profile.md").write_text(
        "# Candidate\n\n"
        "## Kjerne-pitch\nEn kort pitch.\n\n"
        "## SKAL IKKE siteres som offentlig (private repoer)\n"
        "`normedbench` (NorMedBench), `fhir-safety-harness`, `tg-bot-logger`, "
        "`llm-social-agent` — **PRIVATE**. Aldri presenter som offentlig.\n",
        encoding="utf-8",
    )
    _patch_cfg(monkeypatch, **{"jobhunt.roots.job_search": str(job_search_dir)})
    hits = lint.check("Dette prosjektet heter fhir-safety-harness.")
    assert any("fhir-safety-harness" in h.lower() for h in hits)


def test_private_repo_parsing_stops_before_trailing_sentence(monkeypatch, tmp_path):
    """The do-not-cite line names a *different* backticked token
    (`soknad-writer`) later in the same paragraph, describing tooling, not
    a private repo. The parser must not pick it up as a do-not-cite name."""
    job_search_dir = tmp_path / "job-search"
    job_search_dir.mkdir()
    (job_search_dir / "candidate_profile.md").write_text(
        "## SKAL IKKE siteres som offentlig (private repoer)\n"
        "`normedbench` (NorMedBench) — **PRIVATE**. Pre-render-grep i "
        "`soknad-writer`-skillet stopper dem.\n",
        encoding="utf-8",
    )
    _patch_cfg(monkeypatch, **{"jobhunt.roots.job_search": str(job_search_dir)})
    assert lint.check("Jeg bruker soknad-writer daglig.") == []


def test_missing_root_and_cfg_falls_back_to_hardcoded_default(monkeypatch, tmp_path):
    _patch_cfg(monkeypatch, **{
        "jobhunt.roots.job_search": str(tmp_path / "does-not-exist"),
        "jobhunt.private_repo_names": None,
    })
    hits = lint.check("Jeg nevner tg-bot-logger her.")
    assert any("tg-bot-logger" in h.lower() for h in hits)


def test_missing_candidate_profile_file_falls_back_to_cfg(monkeypatch, tmp_path):
    job_search_dir = tmp_path / "job-search"
    job_search_dir.mkdir()  # root exists but candidate_profile.md does not
    _patch_cfg(monkeypatch, **{
        "jobhunt.roots.job_search": str(job_search_dir),
        "jobhunt.private_repo_names": ["llm-social-agent"],
    })
    hits = lint.check("llm-social-agent var et av prosjektene.")
    assert any("llm-social-agent" in h.lower() for h in hits)


# --------------------------------------------------------------------------
# multiple hits, no false positives on ordinary words
# --------------------------------------------------------------------------

def test_multiple_hits_all_reported():
    hits = lint.check("Hei; jeg har B2+ og kom som flyktning i 2027.")
    joined = " ".join(hits).lower()
    assert "semicolon" in joined
    assert "b2+" in joined
    assert "flyktning" in joined
    assert "2027" in joined
    assert len(hits) >= 4
