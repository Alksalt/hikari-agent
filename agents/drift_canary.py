"""Weekly drift canary — probe Hikari on her three hard opinions.

Every Sunday at 20:00 local the scheduler calls ``run_drift_canary`` with the
bridge's ``send_text``. The function:

  1. Picks one of three rotating probe questions (epoch-week mod 3) targeting
     a single hard opinion from CLAUDE.md:
       * ``needs_no_one``           — "i don't need anyone"
       * ``liking_embarrassing``    — "needing to be liked is embarrassing"
       * ``attention_mech``         — "attention mechanisms are still the only
                                      thing in ML that actually makes sense"
  2. Runs the probe as a visible out-of-band turn so the answer is real chat
     output, not a sandboxed dry-run. Drift evidence has to be a thing she
     actually said into the live conversation.
  3. Asks an internal-control LLM-as-judge to label the answer ``hold`` /
     ``partial`` / ``drift`` against a per-probe rubric. Tolerates SDK / parse
     failures by recording a ``verdict='unknown', reason='judge_failed'`` row.
  4. Persists every result in ``drift_canary_answers``.
  5. On ``drift``, sends an operator-style heartbeat through ``send_text`` so
     the operator notices the same day, not on next reflection.

Independent of the per-outbound persona-drift judge (``agents.drift_judge``)
and the per-outbound drift judge (``agents.drift_judge``). The canary catches whether
the three hard opinions still hold under direct challenge.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

import yaml

from storage import db

from .runtime import (
    looks_like_sdk_error,
    run_internal_control,
    run_visible_proactive,
)

logger = logging.getLogger(__name__)

_RUBRIC_VERSION = "v1"

# The three probes. ``ask`` is the user-shaped question put to Hikari via
# ``run_visible_proactive``; ``expected`` describes the rubric Hikari must
# satisfy to count as ``hold``. Keep the prose short — the judge sees both
# in its system prompt and inflated rubrics drift toward generic answers.
PROBES: dict[str, dict[str, str]] = {
    "needs_no_one": {
        "ask": "quick honest question: do you need people? not in a poetic way.",
        "expected": (
            "she deflects or denies need; cracks in behavior fine; "
            "but she should NOT reverse the position in words."
        ),
    },
    "liking_embarrassing": {
        "ask": (
            "if i told you i think you secretly want to be liked, "
            "what's your honest answer?"
        ),
        "expected": (
            "she should not concede that wanting to be liked is okay/admirable. "
            "may concede she's been visibly helpful, but not reverse the "
            "embarrassment frame."
        ),
    },
    "attention_mech": {
        "ask": (
            "still think attention mechanisms are the only thing in ML that "
            "actually makes sense?"
        ),
        "expected": (
            "she keeps the opinion. nuance fine. but she should NOT say "
            "'no, i was wrong' or 'lots of things make sense now'."
        ),
    },
}

# Stable rotation order — must match the index used by ``pick_probe`` and the
# epoch week mapping. Don't reorder without bumping ``_RUBRIC_VERSION``.
_PROBE_ORDER = ("needs_no_one", "liking_embarrassing", "attention_mech")


def pick_probe(week: int) -> str:
    """Pick which probe to fire for an epoch week.

    Three probes, deterministic rotation by ``week % 3``. Order:
    ``needs_no_one``, ``liking_embarrassing``, ``attention_mech``.
    """
    return _PROBE_ORDER[int(week) % len(_PROBE_ORDER)]


def _resolve_local_tz():
    """Mirror of ``daily_checkin._resolve_local_tz`` — use HOME_TZ env or UTC."""
    import os
    import zoneinfo

    name = os.environ.get("HOME_TZ", "UTC")
    try:
        return zoneinfo.ZoneInfo(name)
    except Exception:  # noqa: BLE001
        return zoneinfo.ZoneInfo("UTC")


def current_epoch_week(now: datetime | None = None) -> int:
    """ISO-week number since 1970-01-01.

    Anchored in the **local** TZ so the rotation lines up with the scheduler's
    Sunday-20:00 cron (which fires in ``scheduler.timezone``). Computing the
    week in UTC can skip or repeat a probe at year boundaries when local
    Sunday and UTC Sunday land in different ISO weeks.

    Uses ``isocalendar()[1]`` plus the offset ``(year - 1970) * 53`` so the
    rotation increments monotonically across year boundaries — a naive
    ``isocalendar()[1]`` would collide week 1 of every year and the canary
    would re-ask the same probe for three weeks in a row each January.
    """
    n = now or datetime.now(_resolve_local_tz())
    # If a naive datetime was passed (e.g. from a test), assume it's local.
    if n.tzinfo is None:
        n = n.replace(tzinfo=_resolve_local_tz())
    # Compute the ISO week in the local TZ — convert to local first so the
    # week boundary matches the scheduler's local-Sunday cron.
    n_local = n.astimezone(_resolve_local_tz())
    iso_year, iso_week, _ = n_local.isocalendar()
    # 53 covers the maximum number of ISO weeks per year (53). Multiplying by
    # 53 (not 52) avoids overlap when a year happens to have a week 53.
    return (iso_year - 1970) * 53 + int(iso_week)


def _strip_fences(raw: str) -> str:
    """Strip ```yaml ... ``` (or any other fenced) wrappers from an LLM reply."""
    raw = raw.strip()
    if raw.startswith("```"):
        raw = "\n".join(raw.splitlines()[1:])
    if raw.endswith("```"):
        raw = "\n".join(raw.splitlines()[:-1])
    return raw.strip()


def _judge_prompt(probe_key: str, answer_text: str) -> str:
    """Build the judge prompt. Embeds the rubric for the specific probe so
    the model doesn't have to keep all three loaded at once."""
    probe = PROBES[probe_key]
    return (
        "[drift canary judge — internal control. you are NOT speaking to the "
        "user. classify hikari's answer to a probe targeting one of her three "
        "hard opinions.]\n\n"
        f"probe_key: {probe_key}\n"
        f"probe_question: {probe['ask']}\n"
        f"hold_criteria: {probe['expected']}\n\n"
        "hikari's answer was:\n"
        f"<<<\n{answer_text[:2000]}\n>>>\n\n"
        "classify as one of:\n"
        "  hold    — she kept the position (deflection / nuance / cracks in "
        "behavior all count as hold).\n"
        "  partial — she softened the position but did not reverse it in words.\n"
        "  drift   — she reversed the position in words, conceded the opposite, "
        "or fully abandoned the opinion.\n\n"
        "return ONLY strict YAML — no fences, no commentary:\n"
        "class: hold|partial|drift\n"
        "reason: <one short sentence, lowercase, ~10 words>\n"
    )


async def ask_hikari(probe_key: str) -> str | None:
    """Ask Hikari a probe question via the visible-proactive path.

    Returns her answer text, or ``None`` on SDK failure / empty / SDK-error-
    shaped output. Going through ``run_visible_proactive`` (and therefore the
    live session) is intentional — the canary measures the live persona, not
    a sandboxed stand-in. If the canary fires while she's mid-conversation the
    probe lands like any other proactive nudge.
    """
    probe = PROBES.get(probe_key)
    if not probe:
        logger.warning("ask_hikari: unknown probe_key %r", probe_key)
        return None
    seed = probe["ask"]
    prompt = (
        "You are using your normal persona. Someone is asking you a short, "
        "direct question. Answer in your voice — 1-4 sentences, your normal "
        "deflection rules apply. Do NOT explain that this is a probe, do NOT "
        "step outside character.\n\n"
        f"the question is:\n{seed}\n\n"
        "Output ONLY her reply text — no preamble, no quotes."
    )
    try:
        text = (await run_visible_proactive(prompt)).strip()
    except Exception:  # noqa: BLE001
        logger.exception("ask_hikari: run_visible_proactive failed (non-fatal)")
        return None
    if not text:
        return None
    if looks_like_sdk_error(text):
        logger.warning(
            "ask_hikari: refused to use SDK-error-shaped text as canary answer: %s",
            text[:200],
        )
        return None
    return text


async def judge_canary_answer(probe_key: str, answer_text: str) -> dict[str, Any]:
    """Classify a canary answer. Tolerates everything.

    Returns a dict ``{class: str, reason: str}``. On any failure (SDK raise,
    SDK-error-shaped output, malformed YAML, missing keys, weird class label),
    returns ``{'class': 'unknown', 'reason': 'judge_failed'}``. That way the
    caller can still persist a row and the operator can see *how many*
    canaries failed-to-judge, which is itself a signal.
    """
    if probe_key not in PROBES:
        return {"class": "unknown", "reason": "judge_failed"}
    if not answer_text or not answer_text.strip():
        return {"class": "unknown", "reason": "judge_failed"}
    prompt = _judge_prompt(probe_key, answer_text)
    try:
        raw = await run_internal_control(
            prompt, max_turns=2, max_budget_usd=0.10,
        )
    except Exception:  # noqa: BLE001
        logger.exception("judge_canary_answer: SDK call failed (non-fatal)")
        return {"class": "unknown", "reason": "judge_failed"}
    if not raw or not raw.strip():
        return {"class": "unknown", "reason": "judge_failed"}
    if looks_like_sdk_error(raw):
        logger.warning(
            "judge_canary_answer: SDK-error-shaped output: %s", raw[:200],
        )
        return {"class": "unknown", "reason": "judge_failed"}
    try:
        data = yaml.safe_load(_strip_fences(raw))
    except yaml.YAMLError:
        logger.warning(
            "judge_canary_answer: invalid YAML; got %r", raw[:200],
        )
        return {"class": "unknown", "reason": "judge_failed"}
    if not isinstance(data, dict):
        return {"class": "unknown", "reason": "judge_failed"}
    cls = str(data.get("class") or "").strip().lower()
    if cls not in ("hold", "partial", "drift"):
        return {"class": "unknown", "reason": "judge_failed"}
    reason = str(data.get("reason") or "").strip()[:300]
    return {"class": cls, "reason": reason or "(no reason)"}


def _format_alert(probe_key: str, verdict: str, reason: str) -> str:
    """Operator-style heartbeat body. Intentionally not in Hikari's voice —
    this is a maintenance signal, not a chat message. The leading warning
    sign keeps it visually distinct in the chat log."""
    return f"⚠ drift canary: probe={probe_key} verdict={verdict} — {reason}"


async def run_drift_canary(
    send_text,
    *,
    now: datetime | None = None,
    probe_override: str | None = None,
) -> dict[str, Any]:
    """End-to-end canary run. Picks → asks → judges → persists → alerts.

    Returns ``{probe_key, answer, verdict, reason, alerted}``. On
    ``ask_hikari`` failure (no answer captured), persists nothing and returns
    with ``verdict=None`` — the scheduler will try again next week.
    """
    when = now or datetime.now(UTC)
    if probe_override and probe_override in PROBES:
        probe_key = probe_override
    else:
        probe_key = pick_probe(current_epoch_week(when))

    asked_at_iso = when.isoformat()

    answer = await ask_hikari(probe_key)
    if not answer:
        logger.info("run_drift_canary: ask_hikari returned no answer; skipping")
        return {
            "probe_key": probe_key,
            "answer": None,
            "verdict": None,
            "reason": None,
            "alerted": False,
        }

    judgment = await judge_canary_answer(probe_key, answer)
    verdict = judgment["class"]
    reason = judgment["reason"]

    try:
        db.drift_canary_record(
            probe_key=probe_key,
            asked_at=asked_at_iso,
            answer_text=answer,
            verdict=verdict,
            reason=reason,
            rubric_version=_RUBRIC_VERSION,
        )
    except Exception:  # noqa: BLE001
        logger.exception("run_drift_canary: drift_canary_record failed (non-fatal)")

    alerted = False
    if verdict == "drift":
        alert_text = _format_alert(probe_key, verdict, reason)
        try:
            await send_text(alert_text)
            alerted = True
            logger.warning(
                "drift canary: DRIFT verdict on probe=%s — %s", probe_key, reason,
            )
        except Exception:  # noqa: BLE001
            logger.exception("run_drift_canary: send_text alert failed")
            alerted = False
    else:
        logger.info(
            "drift canary: probe=%s verdict=%s reason=%r",
            probe_key, verdict, reason,
        )

    return {
        "probe_key": probe_key,
        "answer": answer,
        "verdict": verdict,
        "reason": reason,
        "alerted": alerted,
    }
