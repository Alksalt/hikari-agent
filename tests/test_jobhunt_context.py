"""Tests for agents/jobhunt_context.py — weekly job-hunt context-pack
refresh (Sprint 2, Task 6): distills ``candidate_profile.md`` (job_search
root) + ``goals.md`` (prep root) into the always-on ``jobhunt_context``
core_block via a mocked ``run_internal_text``.

Fix pass 2: the NEVER CITE section is deterministic — Python appends it
from cfg ``jobhunt.private_repo_names`` after the LLM produces the other
four sections. The LLM is never trusted to reproduce the do-not-cite list.

Mirrors the ``fresh_db`` / ``_patch_cfg`` fixture patterns from
tests/test_daily_checkin_schedule.py and tests/test_jobhunt_readers.py.
"""
from __future__ import annotations

import datetime as dt
import importlib
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from agents import config as cfg

# What the (mocked) LLM produces — four sections, NO NEVER CITE.
_LLM_BLOCK = (
    "PITCH: clinician who builds agentic AI systems. two sentences here.\n"
    "LANES: e-helse, helsedata, kvalitet\n"
    "PUBLIC REPOS OK TO CITE: hikari-agent, omsorgsradar, medspacy-no\n"
    "NON-GOALS: not a lege/LIS role\n"
)

# What refresh_jobhunt_context() assembles from _LLM_BLOCK: the LLM part
# plus the deterministic NEVER CITE section built from the
# _patch_cfg-pinned private-repo list below.
_FINAL_BLOCK = _LLM_BLOCK.strip() + "\nNEVER CITE: NorMedBench, fhir-safety-harness"


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
    _write(
        prep_dir / "goals.md",
        "## Target taxonomy\ne-helse, helsedata\n"
        "## Non-goals\nnot a lege role\n",
    )
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

    assert fresh_db.get_core_block("jobhunt_context") == _FINAL_BLOCK
    mock.assert_awaited_once()
    _, kwargs = mock.call_args
    assert kwargs["model"] == jobhunt_context.MODEL_HAIKU
    assert kwargs["max_tokens"] == 800


async def test_distill_prompt_embeds_both_source_texts(fresh_db, monkeypatch, sources):
    job_search_dir, prep_dir = sources
    _patch_cfg(monkeypatch, {"job_search": job_search_dir, "prep": prep_dir})

    from agents import jobhunt_context
    mock = AsyncMock(return_value=_LLM_BLOCK)
    monkeypatch.setattr(jobhunt_context, "run_internal_text", mock)

    await jobhunt_context.refresh_jobhunt_context()

    prompt = mock.call_args[0][0]
    assert "builds agentic AI + health data tools" in prompt
    assert "not a lege role" in prompt


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
    assert block.endswith("NEVER CITE: NorMedBench, fhir-safety-harness")


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
    assert block.endswith("NEVER CITE: NorMedBench, fhir-safety-harness")


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
    too_long = "PITCH: x\nLANES: " + ("z" * 1700)
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
    # 1590 chars raw (< 1600); + "\nNEVER CITE: ..." (45 chars) -> > 1600.
    borderline = "PITCH: x\nLANES: " + ("z" * 1574)
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


async def test_one_source_present_still_distills(fresh_db, monkeypatch, tmp_path):
    """Interface note: 'Both missing/empty -> no-op'. If only one of the two
    is present, distillation still proceeds (not a no-op)."""
    job_search_dir = tmp_path / "job-search"
    prep_dir = tmp_path / "prep-missing"
    _write(job_search_dir / "candidate_profile.md", "core pitch present\n")
    _patch_cfg(monkeypatch, {"job_search": job_search_dir, "prep": prep_dir})

    from agents import jobhunt_context
    mock = AsyncMock(return_value=_LLM_BLOCK)
    monkeypatch.setattr(jobhunt_context, "run_internal_text", mock)

    await jobhunt_context.refresh_jobhunt_context()

    mock.assert_awaited_once()
    assert fresh_db.get_core_block("jobhunt_context") == _FINAL_BLOCK


# ---------------------------------------------------------------------------
# startup-run-when-absent scheduler wiring
# ---------------------------------------------------------------------------

def test_scheduler_registers_next_run_time_only_when_block_absent(fresh_db, monkeypatch):
    """CronTrigger(day_of_week='mon', hour=5, minute=30) always registers,
    but the job is only given an explicit ``next_run_time`` add_job kwarg
    (pulling its first fire to "now") when jobhunt_context is currently
    absent — so the feature works the day it ships, not next Monday. When
    the block already exists, no override is passed and the job waits for
    apscheduler's normal computed next-Monday fire.

    Spies on ``AsyncIOScheduler.add_job`` directly rather than inspecting
    ``Job.next_run_time`` post-hoc: apscheduler only populates that
    attribute once a job has an explicit override or the scheduler has
    actually started, so a never-started scheduler's un-overridden jobs
    raise AttributeError on access -- the add_job call site is the only
    place the "did we ask for an immediate fire" decision is observable
    before start().
    """
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

    # Case 1: block absent at build time -> next_run_time override present.
    build_scheduler(noop)
    assert len(calls) == 1
    assert "next_run_time" in calls[0]
    absent_next_run = calls[0]["next_run_time"]
    assert (
        absent_next_run - dt.datetime.now(absent_next_run.tzinfo)
    ).total_seconds() < 5

    # Case 2: block present at build time -> no override, normal weekly cron.
    calls.clear()
    fresh_db.upsert_core_block("jobhunt_context", "PITCH: x\nNEVER CITE: y\n")
    build_scheduler(noop)
    assert len(calls) == 1
    assert "next_run_time" not in calls[0]


def test_scheduler_job_trigger_is_weekly_monday_0530(fresh_db, monkeypatch):
    from agents.scheduler import build_scheduler

    async def noop(_t: str) -> None:
        return None

    sched = build_scheduler(noop)
    job = sched.get_job("jobhunt_context_refresh")
    assert job is not None
    assert str(job.trigger) == "cron[day_of_week='mon', hour='5', minute='30']"


def test_scheduler_gated_on_jobhunt_enabled(fresh_db, monkeypatch):
    _patch_cfg(monkeypatch, {}, **{"jobhunt.enabled": False})
    from agents.scheduler import build_scheduler

    async def noop(_t: str) -> None:
        return None

    sched = build_scheduler(noop)
    assert sched.get_job("jobhunt_context_refresh") is None
