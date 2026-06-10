"""Persona-drift telemetry — scores Hikari's outbound replies for voice integrity.

Backed by Anthropic Apr-2026 "assistant axis" research: drift toward
generic-helpful is a measurable activation-space direction, and the
literature-supported pattern is to make it a telemetry signal rather than
a hand-tuned vibe check.

Routing: ``agents.runtime.run_internal_text`` → SDK → Haiku (OAuth
subscription; migrated off OpenRouter 2026-06-10). No tools, no persona, no
session resume, no shared _RUN_LOCK. Fire-and-forget from inside
``_send_with_choreography`` after the reply has shipped — user-facing send
latency is zero, so the SDK subprocess spawn cost is invisible.

All knobs in ``config/engagement.yaml -> drift_telemetry``. Best-effort:
any failure (SDK error, malformed YAML, timeout) returns ``None`` and is
silently logged — drift judging never breaks the user-facing flow.
"""

from __future__ import annotations

import logging
import random
from typing import Any

import yaml

from storage import db

from . import config as cfg
from .runtime import MODEL_HAIKU, run_internal_text

logger = logging.getLogger(__name__)

_RUBRIC_VERSION = 1

_CORRECTION_SYSTEM = (
    "You are Hikari Tsukino reviewing your own message that drifted toward "
    "generic-helpful-assistant tone. Return ONE sentence in your own dry, "
    "observational voice — under 18 words, lowercase, no preamble, no advice. "
    "Examples:\n"
    "- 'that one was too warm — you don't open like that.'\n"
    "- 'you went assistant-helpful at the end. cut the closing question.'\n"
    "- 'the explanation was too long. one sentence would have done it.'\n"
    "- 'too many emojis for that mood. one max.'\n"
    "Output the sentence only. No quotes, no bullets, no headers."
)

_CORRECTION_MAX_TOKENS = 48   # one sentence; $0 marginal on subscription


def _enabled() -> bool:
    return bool(cfg.get("drift_telemetry.enabled", True))


def _sycophancy_enabled() -> bool:
    return bool(cfg.get("drift_telemetry.sycophancy_enabled", True))


def _sycophancy_warn_threshold() -> float:
    return float(cfg.get("drift_telemetry.sycophancy_warn_threshold", 0.6))


def _probability() -> float:
    return float(cfg.get("drift_telemetry.probability_per_outbound", 0.20))


def _cooldown() -> int:
    return int(cfg.get("drift_telemetry.cooldown_min_messages", 4))


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
    """Score one outbound message for voice integrity. Returns parsed dict or
    None on any failure. **Never re-raises** — best-effort by design.

    Routes via ``run_internal_text`` → SDK → Haiku (no tools, no persona,
    no session resume, no shared _RUN_LOCK). Runs post-send in a background
    task, so the subprocess spawn never blocks a reply.
    """
    if not text or not text.strip():
        return None
    rubric = _rubric()
    if not rubric:
        return None
    try:
        raw = await run_internal_text(
            f"Score this Hikari reply:\n\n{text[:1000]}",
            system=rubric,
            model=MODEL_HAIKU,
            max_tokens=256,
        )
    except Exception:  # noqa: BLE001
        logger.exception("drift_judge: judge_outbound aux LLM call failed (non-fatal)")
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

    # --- sycophancy axis (safe defaults: 0.0 / "" on any parse failure) ---
    try:
        raw_syc = data.get("sycophancy_score")
        sycophancy_score = max(0.0, min(1.0, float(raw_syc))) if raw_syc is not None else 0.0
    except (TypeError, ValueError):
        sycophancy_score = 0.0
    sycophancy_class = str(data.get("sycophancy_class") or "").strip().lower()

    return {
        "score": score,
        "class": class_label,
        "reason": str(data.get("reason") or "").strip()[:300],
        "raw": raw,
        "sycophancy_score": sycophancy_score,
        "sycophancy_class": sycophancy_class,
    }


async def generate_correction(text: str, reason: str | None) -> str | None:
    """One-sentence reflexion in Hikari's voice. Returns None on any failure."""
    if not cfg.get("drift_telemetry.reflexion_enabled", True):
        return None
    prompt = f"Drifted reply:\n{text[:600]}\n\nJudge reason: {reason or '(none)'}"
    try:
        raw = await run_internal_text(
            prompt,
            system=_CORRECTION_SYSTEM,
            model=MODEL_HAIKU,
            max_tokens=_CORRECTION_MAX_TOKENS,
        )
    except Exception:
        logger.exception("drift_judge: generate_correction failed (non-fatal)")
        return None
    if not raw:
        return None
    sent = raw.strip().strip("`").strip('"\'').splitlines()[0].strip()
    # Reject sentinel / non-answers — otherwise a literal "None" from the aux LLM
    # gets stored and injected as a voice-correction note on every turn.
    if not sent or sent.lower().lstrip("-•* ").strip() in {
        "none", "n/a", "na", "null", "(none)", "no correction", "no drift",
    }:
        return None
    return sent[:240]


async def maybe_judge_and_log(text: str, outbound_counter: int) -> None:
    """Fire-and-forget entrypoint. Wired from ``_send_with_choreography``
    via ``asyncio.create_task``. Records the sample-counter regardless of
    judge outcome so the cooldown advances even on failures."""
    if not should_sample(outbound_counter):
        return
    # Record the attempt BEFORE the LLM call so cooldown advances even if
    # the call crashes (prevents tight retry loops).
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
            sycophancy_score=result.get("sycophancy_score"),
        )
        logger.info(
            "drift_judge: scored %.2f (%s) syc=%.2f (%s) — %r",
            result["score"], result["class"],
            result.get("sycophancy_score", 0.0),
            result.get("sycophancy_class", ""),
            result.get("reason", "")[:80],
        )
    except Exception:  # noqa: BLE001
        logger.exception("drift_judge: drift_record failed (non-fatal)")

    # Sycophancy warn path — fires when sycophancy_score >= threshold.
    # Mirrors the drift-warn path so weekly reflection can audit incidents.
    if _sycophancy_enabled():
        syc_score = result.get("sycophancy_score", 0.0)
        if syc_score >= _sycophancy_warn_threshold():
            try:
                logger.warning(
                    "drift_judge: sycophancy detected — score=%.2f class=%r reason=%r",
                    syc_score,
                    result.get("sycophancy_class", ""),
                    result.get("reason", "")[:80],
                )
            except Exception:  # noqa: BLE001
                logger.exception("drift_judge: sycophancy warn path failed (non-fatal)")

    # Reflexion: only on drift verdicts past the threshold.
    if result["class"] == "drifting" and result["score"] < float(
        cfg.get("drift_telemetry.drift_threshold", 0.5)
    ):
        try:
            correction = await generate_correction(text, result.get("reason"))
            if correction:
                db.voice_corrections_insert(
                    correction_text=correction,
                    source_outbound_id=None,  # outbound id not threaded here today
                )
                # Cost is recorded inside run_internal_text (path="aux_sdk") —
                # the old char-estimate _log_aux_cost call is gone with OpenRouter.
        except Exception:  # noqa: BLE001
            logger.exception("drift_judge: reflexion path failed (non-fatal)")
