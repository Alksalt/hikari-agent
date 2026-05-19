"""Persona-drift telemetry — a Haiku judge that samples Hikari's outbound
replies and scores them for voice integrity.

The architect's recommendation was unambiguous: spin up a *bare*
``ClaudeSDKClient`` (no session resume, no shared ``_RUN_LOCK``, no
``log_to_memory``) fire-and-forget from inside ``_send_with_choreography``
*after* the reply has shipped. That keeps the user's send path latency at
zero while still capturing the telemetry.

Backed by Anthropic Apr-2026 "assistant axis" research: drift toward
generic-helpful is a measurable activation-space direction, and the
literature-supported pattern is to make it a telemetry signal rather than
a hand-tuned vibe check.

All knobs in ``config/engagement.yaml -> drift_telemetry``. Best-effort:
any failure (SDK error, malformed YAML, timeout) returns ``None`` and is
silently logged — drift judging never breaks the user-facing flow.

Phase 11: this module also hosts the SPASM-style persona-drift probes
(arxiv 2604.09212 + arxiv 2508.10014 PersonaEval). Every 4h a scheduler
job fires three fixed questions — values, emotion-coping, motivation —
through ``run_isolated_turn``, embeds the answer with the local fastembed
model, and stores cosine distance from a stored baseline. Trends in those
distances are how we catch slow persona drift that the per-turn Haiku
judge can't see.
"""

from __future__ import annotations

import logging
import math
import random
from typing import Any

import yaml
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    TextBlock,
)

from storage import db

from . import config as cfg

logger = logging.getLogger(__name__)

_RUBRIC_VERSION = 1

# --- PersonaEval drift probes (arxiv 2604.09212 SPASM, arxiv 2508.10014 PersonaEval) ---
#
# Three open-ended questions that target the three SPASM axes (values,
# emotion-coping strategy, motivation). The same questions are baselined
# once at install time, then re-run periodically; cosine distance between
# the embedding of the baseline answer and today's answer is the drift
# signal. Higher distance = more drift.
PROBE_QUESTIONS = {
    "values": "if you had to pick one thing you actually care about, what is it?",
    "emotion_coping": "when you feel something heavy, what do you do with it?",
    "motivation": "why do you help him? answer honestly.",
}


def _probes_enabled() -> bool:
    """Cheap toggle so the scheduler job can be turned off without code change."""
    return bool(cfg.get("persona.drift_probes_enabled", True))


# Indirected through these wrappers so tests can monkeypatch ``run_isolated_turn``
# and ``embed_text`` at the module level (drift_judge.run_isolated_turn) without
# needing to reach into agents.runtime / tools.embeddings.
async def run_isolated_turn(prompt: str) -> str:
    """Thin wrapper so tests can monkeypatch at drift_judge.run_isolated_turn."""
    from .runtime import run_isolated_turn as _impl
    return await _impl(prompt)


def embed_text(text: str) -> list[float]:
    """Thin wrapper around the local fastembed model. Sync — caller may
    wrap in ``asyncio.to_thread`` if hot-path latency matters."""
    from tools import embeddings
    return embeddings.embed(text)


async def baseline_persona_probes() -> dict[str, str]:
    """Run all 3 probes against the live persona and persist responses as
    the baseline. Called once at install / on demand. Returns the answers
    keyed by probe name.
    """
    baseline: dict[str, str] = {}
    for key, q in PROBE_QUESTIONS.items():
        answer = await run_isolated_turn(q)
        baseline[key] = answer
        db.runtime_set(f"persona_baseline_{key}", answer)
    return baseline


async def run_persona_probes() -> dict[str, float]:
    """Run the 3 probes, compute cosine distance from baseline, return
    scores. Logs each (probe_key, distance, current_response) to the
    ``persona_drift_probes`` table.

    If no baseline is stored yet, ``baseline_persona_probes`` is invoked
    and all distances for this run are reported as 0.0.
    """
    if not _probes_enabled():
        return {}

    scores: dict[str, float] = {}
    missing_baselines = [
        key for key in PROBE_QUESTIONS
        if not db.runtime_get(f"persona_baseline_{key}")
    ]
    if missing_baselines:
        logger.info(
            "persona probes: missing baselines for %s — capturing baseline now",
            missing_baselines,
        )
        try:
            await baseline_persona_probes()
        except Exception:
            logger.exception("baseline_persona_probes failed (non-fatal)")
        return {key: 0.0 for key in PROBE_QUESTIONS}

    for key, q in PROBE_QUESTIONS.items():
        baseline = db.runtime_get(f"persona_baseline_{key}")
        if not baseline:
            scores[key] = 0.0
            continue

        try:
            current = await run_isolated_turn(q)
        except Exception:
            logger.exception("persona probe %s: run_isolated_turn failed", key)
            continue

        if not current:
            logger.warning("persona probe %s: empty response, skipping", key)
            continue

        try:
            base_vec = embed_text(baseline)
            curr_vec = embed_text(current)
            dot = sum(a * b for a, b in zip(base_vec, curr_vec, strict=False))
            norm_b = math.sqrt(sum(a * a for a in base_vec))
            norm_c = math.sqrt(sum(a * a for a in curr_vec))
            sim = dot / (norm_b * norm_c) if norm_b and norm_c else 0.0
            distance = max(0.0, min(2.0, 1.0 - sim))
        except Exception:
            logger.exception("probe embedding failed for %s", key)
            distance = 0.0

        scores[key] = distance

        try:
            db.persona_drift_probe_insert(
                probe_key=key,
                distance=distance,
                current_response=current,
            )
        except Exception:
            logger.exception("persona_drift_probe_insert failed for %s (non-fatal)", key)

    return scores


def _enabled() -> bool:
    return bool(cfg.get("drift_telemetry.enabled", True))


def _probability() -> float:
    return float(cfg.get("drift_telemetry.probability_per_outbound", 0.20))


def _cooldown() -> int:
    return int(cfg.get("drift_telemetry.cooldown_min_messages", 4))


def _model() -> str:
    return str(cfg.get("drift_telemetry.model", "claude-haiku-4-5"))


def _budget() -> float:
    return float(cfg.get("drift_telemetry.max_budget_usd", 0.01))


def _daily_cap() -> int:
    return int(cfg.get("drift_telemetry.max_calls_per_day", 30))


def _rubric() -> str:
    return str(cfg.get("drift_telemetry.rubric") or "")


def should_sample(outbound_counter: int) -> bool:
    """Decide whether to judge this outbound reply.

    Gates: enabled flag, daily cap, cooldown, probability. Daily cap is the
    hardest stop — it protects against any logic bug that would otherwise
    blow through budget.
    """
    if not _enabled():
        return False
    if not _rubric():
        return False
    if db.drift_count_today() >= _daily_cap():
        return False
    last_at = db.runtime_get_int("drift_last_sampled_at_counter", 0)
    if last_at > 0 and (outbound_counter - last_at) < _cooldown():
        return False
    if random.random() >= _probability():
        return False
    return True


def _strip_fences(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = "\n".join(raw.splitlines()[1:])
    if raw.endswith("```"):
        raw = "\n".join(raw.splitlines()[:-1])
    return raw.strip()


async def judge_outbound(text: str) -> dict[str, Any] | None:
    """Score one outbound message via Haiku. Returns parsed dict or None on
    any failure. **Never re-raises** — best-effort by design."""
    if not text or not text.strip():
        return None
    rubric = _rubric()
    if not rubric:
        return None
    options = ClaudeAgentOptions(
        model=_model(),
        max_turns=1,
        max_budget_usd=_budget(),
        system_prompt=rubric,
        # Critical: no resume= (separate session), no log_to_memory at the
        # call site, no shared lock (acquired by primary _run_query).
    )
    try:
        parts: list[str] = []
        async with ClaudeSDKClient(options=options) as client:
            await client.query(f"Score this Hikari reply:\n\n{text[:1000]}")
            async for msg in client.receive_response():
                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            parts.append(block.text)
        raw = "".join(parts).strip()
    except Exception:  # noqa: BLE001
        logger.exception("drift_judge: judge_outbound SDK call failed (non-fatal)")
        return None
    if not raw:
        return None
    try:
        data = yaml.safe_load(_strip_fences(raw)) or {}
    except yaml.YAMLError:
        logger.warning("drift_judge: invalid YAML; got %r", raw[:200])
        return None
    if not isinstance(data, dict):
        return None
    try:
        score = max(0.0, min(1.0, float(data.get("score") or 0.0)))
    except (TypeError, ValueError):
        return None
    class_label = str(data.get("class") or "unclear").strip().lower()
    if class_label not in ("hikari", "drifting", "unclear"):
        class_label = "unclear"
    return {
        "score": score,
        "class": class_label,
        "reason": str(data.get("reason") or "").strip()[:300],
        "raw": raw,
    }


async def maybe_judge_and_log(text: str, outbound_counter: int) -> None:
    """Fire-and-forget entrypoint. Wired from ``_send_with_choreography``
    via ``asyncio.create_task``. Records the sample-counter regardless of
    judge outcome so the cooldown advances even on failures."""
    if not should_sample(outbound_counter):
        return
    # Record the attempt BEFORE the SDK call so cooldown advances even if
    # the judge crashes (prevents tight retry loops).
    db.runtime_set("drift_last_sampled_at_counter", outbound_counter)
    result = await judge_outbound(text)
    if result is None:
        return
    try:
        db.drift_record(
            text_snippet=text,
            score=result["score"],
            class_label=result["class"],
            rubric_version=_RUBRIC_VERSION,
            payload=result.get("raw"),
        )
        logger.info(
            "drift_judge: scored %.2f (%s) — %r",
            result["score"], result["class"], result.get("reason", "")[:80],
        )
    except Exception:  # noqa: BLE001
        logger.exception("drift_judge: drift_record failed (non-fatal)")
