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
"""

from __future__ import annotations

import logging
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
