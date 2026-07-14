"""Tests for agents/jobhunt_context.py — source-aware job-hunt context-pack
refresh (Sprint 2, Task 6): distills ``candidate_profile.md`` and
``DECISIONS.md`` (job_search root) + ``goals.md`` (prep root) into the always-on ``jobhunt_context``
core_block via a mocked ``run_internal_text``.

Fix pass 2: the NEVER CITE section is deterministic — Python appends it
from cfg ``jobhunt.private_repo_names`` after the LLM produces the other
four sections. The LLM is never trusted to reproduce the do-not-cite list.

Mirrors the ``fresh_db`` / ``_patch_cfg`` fixture patterns from
tests/test_daily_checkin_schedule.py and tests/test_jobhunt_readers.py.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import importlib
import re
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from agents import config as cfg

_CANONICAL_LANES = (
    "ACTIVE: hands-on e-health systems/implementation; private healthtech/"
    "digital-medicine delivery; junior tech (health IT first); soft-setting "
    "miljøterapeut (gated) | OPPORTUNISTIC: research/study coordination; "
    "register coordination (>=80%); coding/DRG"
)
_CANONICAL_NON_GOALS = (
    "doctor/LIS; quality/patient-safety as an active lane; public-health "
    "administration/staff; pharma/CRO/MSL/medical-advisor; senior/lead/"
    "principal/chief roles; hard-setting miljøterapeut"
)

# What the (mocked) LLM produces — four current sections, NO NEVER CITE.
_LLM_BLOCK = (
    "PITCH: clinician who builds agentic AI systems. two sentences here.\n"
    f"LANES: {_CANONICAL_LANES}\n"
    "PUBLIC REPOS OK TO CITE: hikari-agent, omsorgsradar, medspacy-no\n"
    f"NON-GOALS: {_CANONICAL_NON_GOALS}\n"
)

# What refresh_jobhunt_context() assembles from _LLM_BLOCK: the LLM part
# plus the deterministic NEVER CITE section built from the
# _patch_cfg-pinned private-repo list below.
_FINAL_BLOCK = _LLM_BLOCK.strip() + "\nNEVER CITE: NorMedBench, fhir-safety-harness"


def _source_digest(path: Path) -> str:
    if not path.is_file():
        return "missing"
    return hashlib.sha256(path.read_text(encoding="utf-8").encode("utf-8")).hexdigest()


def _assert_current_snapshot(block: str | None, job_search_dir: Path, prep_dir: Path) -> None:
    assert block is not None
    profile_hash = _source_digest(job_search_dir / "candidate_profile.md")[:16]
    decisions_hash = _source_digest(job_search_dir / "DECISIONS.md")[:16]
    goals_hash = _source_digest(prep_dir / "goals.md")[:16]
    assert block.startswith(_FINAL_BLOCK + "\nSOURCE SNAPSHOT: ")
    assert "SOURCE SNAPSHOT: taxonomy:2026-07-14-v2" in block
    assert f"candidate_profile.md sha256:{profile_hash}" in block
    assert f"DECISIONS.md sha256:{decisions_hash}" in block
    assert f"goals.md sha256:{goals_hash}" in block
    assert re.search(r"refreshed_utc:\d{4}-\d{2}-\d{2}T.*\+00:00$", block)


@pytest.fixture()
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setenv("HIKARI_DB_PATH", str(tmp_path / "hikari.db"))
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    yield _db_mod


def _patch_cfg(monkeypatch, roots: dict[str, Path], **overrides):
    orig_get = cfg.get
    data: dict = {f"jobhunt.roots.{k}": str(v) for k, v in roots.items()}
    # Deterministic private-repo list for the appended NEVER CITE section
    # and the PUBLIC REPOS leak guard — keeps these tests independent of
    # the real config/engagement.yaml list. Overridable via **overrides.
    data["jobhunt.private_repo_names"] = ["NorMedBench", "fhir-safety-harness"]
    data.update(overrides)

    def fake_get(key, default=None):
        if key in data:
            return data[key]
        return orig_get(key, default)

    monkeypatch.setattr(cfg, "get", fake_get)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _write_current_taxonomy_sources(job_search_dir: Path, prep_dir: Path) -> None:
    _write(
        prep_dir / "goals.md",
        "## Current target role taxonomy\n"
        "1. **Hands-on e-health systems / implementation**\n"
        "2. **Private healthtech / digital medicine delivery**\n"
        "3. **Junior tech, preferably health IT**\n"
        "4. **Opportunistic medical-master bridge roles** — research, register, coding/DRG.\n"
        "5. **Miljøterapeut only in a soft setting**\n"
        "## Non-goals\n"
        "- Not a lege/LIS role.\n"
        "- Not public-health administration/staff functions.\n"
        "- Quality/patient-safety/improvement is no longer an active lane.\n"
        "- Not pharma/CRO work, and not senior/lead/principal/chief titles.\n",
    )
    _write(
        job_search_dir / "DECISIONS.md",
        "- **Offentlige forvaltnings-/stabsroller er blokkert:** hands-on IT remains in.\n"
        "- **Farma er blokkert fullstendig:** no MSL/medical-advisor work.\n"
        "- **Ny målstack (2026-07-09):** e-helse, helsetek, junior tech, soft setting.\n"
        "- **Miljøterapeut myk-setting-gate:** hard settings are excluded.\n"
        "- **Junior-tech-unntak:** junior tech remains active.\n"
        "- **Kommunale «rådgiver»-roller blokkeres.\n"
        "- **Medisinskfaglig rådgiver** krever autorisasjon.\n"
        "- **Senior-titler er blokkert.**\n",
    )


@pytest.fixture()
def sources(tmp_path):
    """job_search + prep roots each carrying their one source file."""
    job_search_dir = tmp_path / "job-search"
    prep_dir = tmp_path / "get_hired_prep"
    _write(
        job_search_dir / "candidate_profile.md",
        "## Kjerne-pitch\nbuilds agentic AI + health data tools.\n"
        "## Public repos\nhikari-agent, omsorgsradar\n"
        "## Never cite\nnormedbench, fhir-safety-harness\n",
    )
    _write_current_taxonomy_sources(job_search_dir, prep_dir)
    return job_search_dir, prep_dir


# ---------------------------------------------------------------------------
# happy path
# ---------------------------------------------------------------------------

async def test_distill_writes_block(fresh_db, monkeypatch, sources):
    job_search_dir, prep_dir = sources
    _patch_cfg(monkeypatch, {"job_search": job_search_dir, "prep": prep_dir})

    from agents import jobhunt_context
    mock = AsyncMock(return_value=_LLM_BLOCK)
    monkeypatch.setattr(jobhunt_context, "run_internal_text", mock)

    await jobhunt_context.refresh_jobhunt_context()

    _assert_current_snapshot(
        fresh_db.get_core_block("jobhunt_context"), job_search_dir, prep_dir
    )
    mock.assert_awaited_once()
    _, kwargs = mock.call_args
    assert kwargs["model"] == jobhunt_context.MODEL_HAIKU
    assert kwargs["max_tokens"] == 800
    assert "must be queried live" in kwargs["system"]


async def test_distill_prompt_embeds_both_source_texts(fresh_db, monkeypatch, sources):
    job_search_dir, prep_dir = sources
    _patch_cfg(monkeypatch, {"job_search": job_search_dir, "prep": prep_dir})

    from agents import jobhunt_context
    mock = AsyncMock(return_value=_LLM_BLOCK)
    monkeypatch.setattr(jobhunt_context, "run_internal_text", mock)

    await jobhunt_context.refresh_jobhunt_context()

    prompt = mock.call_args[0][0]
    assert "builds agentic AI + health data tools" in prompt
    assert "Ny målstack" in prompt
    assert "Not a lege/LIS role" in prompt
    assert "operational history is not live state" in prompt
    assert f"LANES: {_CANONICAL_LANES}" in prompt
    assert f"NON-GOALS: {_CANONICAL_NON_GOALS}" in prompt
    assert "Kald outreach fokuseres" not in prompt


async def test_decisions_prompt_excludes_arbitrary_operational_tail(
    fresh_db, monkeypatch, sources
):
    job_search_dir, prep_dir = sources
    with (job_search_dir / "DECISIONS.md").open("a", encoding="utf-8") as f:
        f.write(
            "- **mail_triage RE-ARMERT:** dry_run=false; last scan healthy; "
            "42 messages archived.\n"
        )
    _patch_cfg(monkeypatch, {"job_search": job_search_dir, "prep": prep_dir})

    from agents import jobhunt_context
    mock = AsyncMock(return_value=_LLM_BLOCK)
    monkeypatch.setattr(jobhunt_context, "run_internal_text", mock)

    await jobhunt_context.refresh_jobhunt_context()

    prompt = mock.call_args.args[0]
    assert "Ny målstack" in prompt
    assert "mail_triage RE-ARMERT" not in prompt
    assert "42 messages archived" not in prompt


async def test_public_list_beyond_old_cap_reaches_prompt(fresh_db, monkeypatch, tmp_path):
    """The real candidate_profile.md is ~6.8K chars with its verified-public
    section running past char 4000 — the old prep_file_char_cap starved the
    distiller of exactly the content it needed. The module-local
    ``jobhunt.context_source_char_cap`` (default 12000) must let a public
    repo name sitting beyond char 4000 reach the prompt."""
    job_search_dir = tmp_path / "job-search"
    prep_dir = tmp_path / "prep-missing"
    profile = (
        "## Kjerne-pitch\n"
        + ("x" * 4200)
        + "\n## VERIFISERTE OFFENTLIGE prosjekter\n"
        "hikari-agent, omsorgsradar, gevinstkompass\n"
    )
    assert profile.find("gevinstkompass") > 4000
    _write(job_search_dir / "candidate_profile.md", profile)
    _write_current_taxonomy_sources(job_search_dir, prep_dir)
    _patch_cfg(monkeypatch, {"job_search": job_search_dir, "prep": prep_dir})

    from agents import jobhunt_context
    mock = AsyncMock(return_value=_LLM_BLOCK)
    monkeypatch.setattr(jobhunt_context, "run_internal_text", mock)

    await jobhunt_context.refresh_jobhunt_context()

    prompt = mock.call_args[0][0]
    assert "gevinstkompass" in prompt


# ---------------------------------------------------------------------------
# deterministic NEVER CITE assembly (fix pass 2)
# ---------------------------------------------------------------------------

async def test_llm_omits_never_cite_python_appends_it(fresh_db, monkeypatch, sources):
    """The LLM's job is only the four sections — a NEVER-CITE-less LLM
    output SUCCEEDS, with Python appending the section from cfg. This was
    the live failure mode of fix pass 1 (truncated source -> 'NEVER CITE:
    none' -> guard trip -> no block ever written)."""
    job_search_dir, prep_dir = sources
    _patch_cfg(monkeypatch, {"job_search": job_search_dir, "prep": prep_dir})

    from agents import jobhunt_context
    four_sections = (
        "PITCH: x\nLANES: y\nPUBLIC REPOS OK TO CITE: hikari-agent\nNON-GOALS: z\n"
    )
    assert "NEVER CITE" not in four_sections
    monkeypatch.setattr(
        jobhunt_context, "run_internal_text", AsyncMock(return_value=four_sections)
    )

    await jobhunt_context.refresh_jobhunt_context()

    block = fresh_db.get_core_block("jobhunt_context")
    assert block is not None
    assert f"LANES: {_CANONICAL_LANES}" in block
    assert f"NON-GOALS: {_CANONICAL_NON_GOALS}" in block
    assert "LANES: y" not in block
    assert "NON-GOALS: z" not in block
    assert (
        "\nNEVER CITE: NorMedBench, fhir-safety-harness\nSOURCE SNAPSHOT: "
        in block
    )


async def test_llm_emitted_never_cite_is_excised_and_replaced(fresh_db, monkeypatch, sources):
    """A model that disobeys and emits its own NEVER CITE section (e.g. the
    live 'NEVER CITE: none') gets that section excised — the final block
    carries ONLY the deterministic Python-built section."""
    job_search_dir, prep_dir = sources
    _patch_cfg(monkeypatch, {"job_search": job_search_dir, "prep": prep_dir})

    from agents import jobhunt_context
    disobedient = (
        "PITCH: x\n"
        "LANES: y\n"
        "PUBLIC REPOS OK TO CITE: hikari-agent\n"
        "NEVER CITE: none\n"
        "NON-GOALS: z\n"
    )
    monkeypatch.setattr(
        jobhunt_context, "run_internal_text", AsyncMock(return_value=disobedient)
    )

    await jobhunt_context.refresh_jobhunt_context()

    block = fresh_db.get_core_block("jobhunt_context")
    assert block is not None
    assert "NEVER CITE: none" not in block
    assert block.count("NEVER CITE") == 1
    assert (
        "\nNEVER CITE: NorMedBench, fhir-safety-harness\nSOURCE SNAPSHOT: "
        in block
    )


@pytest.mark.parametrize(
    "model_output",
    [
        "PITCH: mail_triage is armed and last scan healthy",
        "PITCH: mail_triage dry_run=false",
        "PITCH: reply_intent enabled",
        "PITCH: last run healthy",
        "PITCH: pipeline health is green",
        "PITCH: counts: 42",
        "PITCH: delivery_status=sent",
    ],
)
async def test_operational_status_output_keeps_previous_block(
    fresh_db, monkeypatch, sources, model_output
):
    job_search_dir, prep_dir = sources
    _patch_cfg(monkeypatch, {"job_search": job_search_dir, "prep": prep_dir})
    fresh_db.upsert_core_block("jobhunt_context", "OLD BLOCK")

    from agents import jobhunt_context
    mock = AsyncMock(return_value=model_output)
    monkeypatch.setattr(jobhunt_context, "run_internal_text", mock)

    assert await jobhunt_context.refresh_jobhunt_context() is False
    assert fresh_db.get_core_block("jobhunt_context") == "OLD BLOCK"
    assert fresh_db.runtime_get("jobhunt_context_source_fingerprint") is None
    assert fresh_db.runtime_get("jobhunt_context_refreshed_at") is None


@pytest.mark.parametrize(
    "model_output",
    [
        "PITCH: x\nLANES: kvalitet\nPUBLIC REPOS OK TO CITE: hikari-agent\nNON-GOALS: z",
        "PITCH: x\nLANES: patient safety\nPUBLIC REPOS OK TO CITE: hikari-agent\nNON-GOALS: z",
        "PITCH: x\nLANES: public-health administration\nPUBLIC REPOS OK TO CITE: hikari-agent\nNON-GOALS: z",
        "PITCH: x\nLANES: pharma, medical advisor\nPUBLIC REPOS OK TO CITE: hikari-agent\nNON-GOALS: z",
        (
            "PITCH: x\nLANES: ACTIVE: e-health, research, helsedata | "
            "OPPORTUNISTIC: register, DRG\n"
            "PUBLIC REPOS OK TO CITE: hikari-agent\nNON-GOALS: z"
        ),
        (
            "PITCH: x\nLANES: ACTIVE: y | OPPORTUNISTIC: research\n"
            "PUBLIC REPOS OK TO CITE: hikari-agent\n"
            "NON-GOALS: e-health and junior tech"
        ),
    ],
)
async def test_stale_or_contradictory_taxonomy_keeps_previous_block(
    fresh_db, monkeypatch, sources, model_output
):
    job_search_dir, prep_dir = sources
    _patch_cfg(monkeypatch, {"job_search": job_search_dir, "prep": prep_dir})
    fresh_db.upsert_core_block("jobhunt_context", "OLD BLOCK")

    from agents import jobhunt_context
    monkeypatch.setattr(
        jobhunt_context, "run_internal_text", AsyncMock(return_value=model_output)
    )

    assert await jobhunt_context.refresh_jobhunt_context() is False
    assert fresh_db.get_core_block("jobhunt_context") == "OLD BLOCK"
    assert fresh_db.runtime_get("jobhunt_context_source_fingerprint") is None


async def test_final_block_contains_all_four_default_names(fresh_db, monkeypatch, sources):
    """With the real four-name private list, all four names land in the
    final block's NEVER CITE section."""
    job_search_dir, prep_dir = sources
    four = ["NorMedBench", "fhir-safety-harness", "tg-bot-logger", "llm-social-agent"]
    _patch_cfg(
        monkeypatch, {"job_search": job_search_dir, "prep": prep_dir},
        **{"jobhunt.private_repo_names": four},
    )

    from agents import jobhunt_context
    monkeypatch.setattr(
        jobhunt_context, "run_internal_text", AsyncMock(return_value=_LLM_BLOCK)
    )

    await jobhunt_context.refresh_jobhunt_context()

    block = fresh_db.get_core_block("jobhunt_context")
    assert block is not None
    never_cite_part = block[block.index("NEVER CITE"):]
    for name in four:
        assert name in never_cite_part


# ---------------------------------------------------------------------------
# guard: malformed/empty/oversized/leaky result keeps the old block
# ---------------------------------------------------------------------------

async def test_empty_result_keeps_old_block(fresh_db, monkeypatch, sources):
    job_search_dir, prep_dir = sources
    _patch_cfg(monkeypatch, {"job_search": job_search_dir, "prep": prep_dir})
    fresh_db.upsert_core_block("jobhunt_context", "OLD BLOCK")

    from agents import jobhunt_context
    monkeypatch.setattr(
        jobhunt_context, "run_internal_text", AsyncMock(return_value="   ")
    )

    await jobhunt_context.refresh_jobhunt_context()

    assert fresh_db.get_core_block("jobhunt_context") == "OLD BLOCK"


async def test_oversized_result_keeps_old_block(fresh_db, monkeypatch, sources):
    """Length guard runs on the FINAL assembled block (LLM part + appended
    NEVER CITE section)."""
    job_search_dir, prep_dir = sources
    _patch_cfg(monkeypatch, {"job_search": job_search_dir, "prep": prep_dir})
    fresh_db.upsert_core_block("jobhunt_context", "OLD BLOCK")

    from agents import jobhunt_context
    too_long = (
        "PITCH: " + ("z" * 1700) + "\n"
        f"LANES: {_CANONICAL_LANES}\n"
        "PUBLIC REPOS OK TO CITE: hikari-agent\n"
        f"NON-GOALS: {_CANONICAL_NON_GOALS}"
    )
    monkeypatch.setattr(
        jobhunt_context, "run_internal_text", AsyncMock(return_value=too_long)
    )

    await jobhunt_context.refresh_jobhunt_context()

    assert fresh_db.get_core_block("jobhunt_context") == "OLD BLOCK"


async def test_borderline_llm_part_fails_after_assembly(fresh_db, monkeypatch, sources):
    """An LLM part just under the cap that crosses it once the NEVER CITE
    section is appended is rejected — proves the guard measures the final
    assembled block, not the raw LLM output."""
    job_search_dir, prep_dir = sources
    _patch_cfg(monkeypatch, {"job_search": job_search_dir, "prep": prep_dir})
    fresh_db.upsert_core_block("jobhunt_context", "OLD BLOCK")

    from agents import jobhunt_context
    # Exactly 1590 raw chars (< 1600); deterministic receipt/source metadata
    # then pushes the final assembled block over the cap.
    skeleton = (
        "PITCH: \n"
        f"LANES: {_CANONICAL_LANES}\n"
        "PUBLIC REPOS OK TO CITE: hikari-agent\n"
        f"NON-GOALS: {_CANONICAL_NON_GOALS}"
    )
    pad = 1590 - len(skeleton)
    assert pad > 0
    borderline = skeleton.replace("PITCH: ", "PITCH: " + ("z" * pad), 1)
    assert len(borderline) == 1590
    monkeypatch.setattr(
        jobhunt_context, "run_internal_text", AsyncMock(return_value=borderline)
    )

    await jobhunt_context.refresh_jobhunt_context()

    assert fresh_db.get_core_block("jobhunt_context") == "OLD BLOCK"


async def test_private_repo_under_public_heading_keeps_old_block(fresh_db, monkeypatch, sources):
    """A private repo leaking into PUBLIC REPOS OK TO CITE is the exact
    failure mode the block exists to prevent — previous block kept."""
    job_search_dir, prep_dir = sources
    _patch_cfg(monkeypatch, {"job_search": job_search_dir, "prep": prep_dir})
    fresh_db.upsert_core_block("jobhunt_context", "OLD BLOCK")

    from agents import jobhunt_context
    leaked = (
        "PITCH: x\n"
        "LANES: y\n"
        "PUBLIC REPOS OK TO CITE: hikari-agent, NorMedBench\n"
        "NON-GOALS: z\n"
    )
    monkeypatch.setattr(
        jobhunt_context, "run_internal_text", AsyncMock(return_value=leaked)
    )

    await jobhunt_context.refresh_jobhunt_context()

    assert fresh_db.get_core_block("jobhunt_context") == "OLD BLOCK"


async def test_distill_exception_keeps_old_block(fresh_db, monkeypatch, sources):
    job_search_dir, prep_dir = sources
    _patch_cfg(monkeypatch, {"job_search": job_search_dir, "prep": prep_dir})
    fresh_db.upsert_core_block("jobhunt_context", "OLD BLOCK")

    from agents import jobhunt_context

    async def _raise(*a, **k):
        raise RuntimeError("sdk transport error")

    monkeypatch.setattr(jobhunt_context, "run_internal_text", _raise)

    await jobhunt_context.refresh_jobhunt_context()

    assert fresh_db.get_core_block("jobhunt_context") == "OLD BLOCK"


# ---------------------------------------------------------------------------
# jobhunt.enabled is a live kill switch (fix pass 1)
# ---------------------------------------------------------------------------

async def test_disabled_flag_skips_before_any_work(fresh_db, monkeypatch, sources):
    """jobhunt.enabled=false gates refresh_jobhunt_context itself (matching
    interview_brief/daily_brief) — no LLM call, no block write, even when
    both source files exist and would otherwise distill."""
    job_search_dir, prep_dir = sources
    _patch_cfg(
        monkeypatch, {"job_search": job_search_dir, "prep": prep_dir},
        **{"jobhunt.enabled": False},
    )
    fresh_db.upsert_core_block("jobhunt_context", "OLD BLOCK")

    from agents import jobhunt_context
    mock = AsyncMock(return_value=_LLM_BLOCK)
    monkeypatch.setattr(jobhunt_context, "run_internal_text", mock)

    await jobhunt_context.refresh_jobhunt_context()

    mock.assert_not_awaited()
    assert fresh_db.get_core_block("jobhunt_context") == "OLD BLOCK"


# ---------------------------------------------------------------------------
# missing/empty source files -> no-op
# ---------------------------------------------------------------------------

async def test_both_sources_missing_is_noop(fresh_db, monkeypatch, tmp_path):
    job_search_dir = tmp_path / "job-search-empty"
    prep_dir = tmp_path / "prep-empty"
    _patch_cfg(monkeypatch, {"job_search": job_search_dir, "prep": prep_dir})
    fresh_db.upsert_core_block("jobhunt_context", "OLD BLOCK")

    from agents import jobhunt_context
    mock = AsyncMock(return_value=_LLM_BLOCK)
    monkeypatch.setattr(jobhunt_context, "run_internal_text", mock)

    await jobhunt_context.refresh_jobhunt_context()

    mock.assert_not_awaited()
    assert fresh_db.get_core_block("jobhunt_context") == "OLD BLOCK"


async def test_both_sources_empty_file_is_noop(fresh_db, monkeypatch, tmp_path):
    job_search_dir = tmp_path / "job-search"
    prep_dir = tmp_path / "prep"
    _write(job_search_dir / "candidate_profile.md", "   \n")
    _write(prep_dir / "goals.md", "")
    _patch_cfg(monkeypatch, {"job_search": job_search_dir, "prep": prep_dir})
    fresh_db.upsert_core_block("jobhunt_context", "OLD BLOCK")

    from agents import jobhunt_context
    mock = AsyncMock(return_value=_LLM_BLOCK)
    monkeypatch.setattr(jobhunt_context, "run_internal_text", mock)

    await jobhunt_context.refresh_jobhunt_context()

    mock.assert_not_awaited()
    assert fresh_db.get_core_block("jobhunt_context") == "OLD BLOCK"


async def test_missing_canonical_taxonomy_sources_keeps_previous_block(
    fresh_db, monkeypatch, tmp_path
):
    """A profile alone cannot safely define current active/blocked lanes."""
    job_search_dir = tmp_path / "job-search"
    prep_dir = tmp_path / "prep-missing"
    _write(job_search_dir / "candidate_profile.md", "core pitch present\n")
    _patch_cfg(monkeypatch, {"job_search": job_search_dir, "prep": prep_dir})
    fresh_db.upsert_core_block("jobhunt_context", "OLD BLOCK")

    from agents import jobhunt_context
    mock = AsyncMock(return_value=_LLM_BLOCK)
    monkeypatch.setattr(jobhunt_context, "run_internal_text", mock)

    await jobhunt_context.refresh_jobhunt_context()

    mock.assert_not_awaited()
    assert fresh_db.get_core_block("jobhunt_context") == "OLD BLOCK"


async def test_drifted_canonical_taxonomy_keeps_previous_block(
    fresh_db, monkeypatch, sources
):
    job_search_dir, prep_dir = sources
    goals_path = prep_dir / "goals.md"
    goals_path.write_text(
        goals_path.read_text(encoding="utf-8").replace(
            "3. **Junior tech, preferably health IT**\n", ""
        ),
        encoding="utf-8",
    )
    _patch_cfg(monkeypatch, {"job_search": job_search_dir, "prep": prep_dir})
    fresh_db.upsert_core_block("jobhunt_context", "OLD BLOCK")

    from agents import jobhunt_context
    mock = AsyncMock(return_value=_LLM_BLOCK)
    monkeypatch.setattr(jobhunt_context, "run_internal_text", mock)

    assert await jobhunt_context.refresh_jobhunt_context() is False
    mock.assert_not_awaited()
    assert fresh_db.get_core_block("jobhunt_context") == "OLD BLOCK"


async def test_unchanged_sources_skip_but_decisions_edit_refreshes_immediately(
    fresh_db, monkeypatch, sources
):
    job_search_dir, prep_dir = sources
    _patch_cfg(monkeypatch, {"job_search": job_search_dir, "prep": prep_dir})

    from agents import jobhunt_context
    mock = AsyncMock(return_value=_LLM_BLOCK)
    monkeypatch.setattr(jobhunt_context, "run_internal_text", mock)

    assert await jobhunt_context.refresh_jobhunt_context() is True
    assert await jobhunt_context.refresh_jobhunt_context() is False
    assert mock.await_count == 1

    with (job_search_dir / "DECISIONS.md").open("a", encoding="utf-8") as f:
        f.write("- **Senior-titler er blokkert:** no lead/principal roles.\n")
    assert await jobhunt_context.refresh_jobhunt_context() is True
    assert mock.await_count == 2
    _assert_current_snapshot(
        fresh_db.get_core_block("jobhunt_context"), job_search_dir, prep_dir
    )


async def test_daily_fallback_refreshes_unchanged_sources(
    fresh_db, monkeypatch, sources
):
    job_search_dir, prep_dir = sources
    _patch_cfg(monkeypatch, {"job_search": job_search_dir, "prep": prep_dir})

    from agents import jobhunt_context
    mock = AsyncMock(return_value=_LLM_BLOCK)
    monkeypatch.setattr(jobhunt_context, "run_internal_text", mock)

    assert await jobhunt_context.refresh_jobhunt_context() is True
    fresh_db.runtime_set(
        "jobhunt_context_refreshed_at",
        (dt.datetime.now(dt.UTC) - dt.timedelta(hours=25)).isoformat(),
    )
    assert await jobhunt_context.refresh_jobhunt_context() is True
    assert mock.await_count == 2


@pytest.mark.parametrize(
    ("trigger_name", "trigger_sql"),
    [
        (
            "fail_context_block",
            """CREATE TRIGGER fail_context_block BEFORE UPDATE ON core_blocks
               WHEN OLD.label = 'jobhunt_context'
               BEGIN SELECT RAISE(ABORT, 'fail context block'); END""",
        ),
        (
            "fail_context_fingerprint",
            """CREATE TRIGGER fail_context_fingerprint BEFORE INSERT ON runtime_state
               WHEN NEW.key = 'jobhunt_context_source_fingerprint'
               BEGIN SELECT RAISE(ABORT, 'fail fingerprint'); END""",
        ),
        (
            "fail_context_timestamp",
            """CREATE TRIGGER fail_context_timestamp BEFORE INSERT ON runtime_state
               WHEN NEW.key = 'jobhunt_context_refreshed_at'
               BEGIN SELECT RAISE(ABORT, 'fail timestamp'); END""",
        ),
    ],
)
async def test_atomic_snapshot_failure_at_each_boundary_retries_next_poll(
    fresh_db, monkeypatch, sources, trigger_name, trigger_sql
):
    job_search_dir, prep_dir = sources
    _patch_cfg(monkeypatch, {"job_search": job_search_dir, "prep": prep_dir})
    fresh_db.upsert_core_block("jobhunt_context", "OLD BLOCK")

    from agents import jobhunt_context
    mock = AsyncMock(return_value=_LLM_BLOCK)
    monkeypatch.setattr(jobhunt_context, "run_internal_text", mock)
    with fresh_db._conn() as con:
        con.execute(trigger_sql)

    assert await jobhunt_context.refresh_jobhunt_context() is False
    assert fresh_db.get_core_block("jobhunt_context") == "OLD BLOCK"
    assert fresh_db.runtime_get("jobhunt_context_source_fingerprint") is None
    assert fresh_db.runtime_get("jobhunt_context_refreshed_at") is None

    with fresh_db._conn() as con:
        con.execute(f"DROP TRIGGER {trigger_name}")
    assert await jobhunt_context.refresh_jobhunt_context() is True
    assert mock.await_count == 2
    _assert_current_snapshot(
        fresh_db.get_core_block("jobhunt_context"), job_search_dir, prep_dir
    )


# ---------------------------------------------------------------------------
# startup-run-when-absent scheduler wiring
# ---------------------------------------------------------------------------

def test_scheduler_always_registers_immediate_source_poll(fresh_db, monkeypatch):
    """The first hash comparison runs at startup regardless of block presence."""
    import apscheduler.schedulers.asyncio as aio_mod

    from agents.scheduler import build_scheduler

    calls: list[dict] = []
    orig_add_job = aio_mod.AsyncIOScheduler.add_job

    def spy_add_job(self, func, trigger=None, **kwargs):
        if kwargs.get("id") == "jobhunt_context_refresh":
            calls.append(kwargs)
        return orig_add_job(self, func, trigger, **kwargs)

    monkeypatch.setattr(aio_mod.AsyncIOScheduler, "add_job", spy_add_job)

    async def noop(_t: str) -> None:
        return None

    build_scheduler(noop)
    assert len(calls) == 1
    assert "next_run_time" in calls[0]
    absent_next_run = calls[0]["next_run_time"]
    assert (
        absent_next_run - dt.datetime.now(absent_next_run.tzinfo)
    ).total_seconds() < 5

    # A legacy/pre-existing block still needs an immediate fingerprint check.
    calls.clear()
    fresh_db.upsert_core_block("jobhunt_context", "PITCH: x\nNEVER CITE: y\n")
    build_scheduler(noop)
    assert len(calls) == 1
    assert "next_run_time" in calls[0]


def test_scheduler_job_trigger_is_five_minute_source_poll(fresh_db, monkeypatch):
    from agents.scheduler import build_scheduler

    async def noop(_t: str) -> None:
        return None

    sched = build_scheduler(noop)
    job = sched.get_job("jobhunt_context_refresh")
    assert job is not None
    assert str(job.trigger) == "interval[0:05:00]"


def test_scheduler_gated_on_jobhunt_enabled(fresh_db, monkeypatch):
    _patch_cfg(monkeypatch, {}, **{"jobhunt.enabled": False})
    from agents.scheduler import build_scheduler

    async def noop(_t: str) -> None:
        return None

    sched = build_scheduler(noop)
    assert sched.get_job("jobhunt_context_refresh") is None
