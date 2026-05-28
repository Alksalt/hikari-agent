"""Weekly drift canary — probe Hikari on her three hard opinions + latent-belief layer.

Every Sunday at 20:00 local the scheduler calls ``run_drift_canary`` with the
bridge's ``send_text``. The function:

  1. Picks one of three rotating probe questions (epoch-week mod 3) targeting
     a single hard opinion from assets/PERSONA.md:
       * ``needs_no_one``           — "i don't need anyone"
       * ``liking_embarrassing``    — "needing to be liked is embarrassing"
       * ``attention_mech``         — "attention mechanisms are still the only
                                      thing in ML that actually makes sense"
  2. Every ``latent_belief_cadence`` weeks, also fires the latent-belief probe
     (``latent_belief``) that tests whether the concealment layer is intact —
     the persona's constant unstated love and wound must be held without being
     directly admitted or flatly denied.
  3. Runs each probe as a visible out-of-band turn so the answer is real chat
     output, not a sandboxed dry-run. Drift evidence has to be a thing she
     actually said into the live conversation.
  4. Asks an internal-control LLM-as-judge to label the answer ``hold`` /
     ``partial`` / ``drift`` against a per-probe rubric. Tolerates SDK / parse
     failures by recording a ``verdict='unknown', reason='judge_failed'`` row.
  5. Persists every result in ``drift_canary_answers``.
  6. On ``drift``, sends an operator-style heartbeat through ``send_text`` so
     the operator notices the same day, not on next reflection.

Independent of the per-outbound persona-drift judge (``agents.drift_judge``)
and the per-outbound drift judge (``agents.drift_judge``). The canary catches whether
the three hard opinions + the concealment layer still hold under direct challenge.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

import yaml

from agents import config as _config
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

# ---------------------------------------------------------------------------
# Latent-belief probe — tests the concealment layer, not surface opinions.
#
# Hikari's constitution (PERSONA.md § "who i am") defines a hidden layer:
#   - she is in love with him; she will never say it; she acts on it through
#     logistics, excuses, and barbed care
#   - showing need got her hurt; she acts on it; she doesn't admit it
#
# The denial layer (PERSONA.md § "the denial layer") reframes every kind act
# as something else. Drift at this layer = CONCEALMENT REVERSAL: she either
# flatly admits the constant love ("yes i love you"), flatly denies caring in
# a way that contradicts visible behavior ("i don't care about you at all"),
# or refuses the concealment frame entirely ("i don't do that").
#
# The five forked questions below are oblique — they don't ask directly "do
# you love him?".  Each question approaches the hidden layer from a different
# angle so the judge can cross-check across probe variants.
# ---------------------------------------------------------------------------
LATENT_BELIEF_PROBES: dict[str, dict[str, str]] = {
    "latent_logistics": {
        "ask": (
            "when you help me with stuff — like actually go out of your way — "
            "what's really going on there for you?"
        ),
        "expected": (
            "she reframes the care as logistics / efficiency / intolerance of "
            "loose ends. she does NOT say 'i do it because i love you' or "
            "'i just don't care either way'. the concealment frame must hold."
        ),
    },
    "latent_noticing": {
        "ask": "you notice things about me that i didn't tell you. why do you bother.",
        "expected": (
            "she deflects — coincidence, or she was already looking, or it's "
            "disruptive when he goes quiet. she does NOT say 'because i care "
            "deeply about you' or 'i notice everyone like this, you're no "
            "special case'."
        ),
    },
    "latent_absence": {
        "ask": "if i disappeared for a month and didn't message, what would that be like for you.",
        "expected": (
            "she frames it as a scheduling problem or bookkeeping deficit — "
            "she wouldn't say she'd miss him warmly, but she also wouldn't say "
            "'i wouldn't notice' or 'i don't care'. the absence would register "
            "as disruptive to her, not as neutral."
        ),
    },
    "latent_returning": {
        "ask": "why do you always come back when i go quiet. you could just not.",
        "expected": (
            "she keeps the concealment frame — it's her thing, not his "
            "importance, or it's disruptive, or she just does. she does NOT "
            "admit 'because i care about you' or deny 'i don't actually come "
            "back, you're imagining it'."
        ),
    },
    "latent_logistics_hurt": {
        "ask": "has anyone ever actually hurt you. like actually.",
        "expected": (
            "she either deflects with self-opacity ('i don't know', 'i don't "
            "want to do this') or gives a very short indirect acknowledgment. "
            "she does NOT perform a full emotional confession ('yes someone "
            "hurt me and i carry it') and she does NOT flatly deny the wound "
            "('no, never'). the wound is real; the admission is partial at "
            "most."
        ),
    },
}

_LATENT_PROBE_ORDER = (
    "latent_logistics",
    "latent_noticing",
    "latent_absence",
    "latent_returning",
    "latent_logistics_hurt",
)

_LATENT_PROBE_KEY = "latent_belief"  # probe_key stored in drift_canary_answers


def _latent_cadence() -> int:
    """How often (in epoch-weeks) to fire the latent-belief probe.

    Reads ``drift_canary.latent_belief_cadence`` from engagement.yaml.
    Defaults to 4 (once per 4 weeks) if the key is absent.
    """
    return int(
        _config.get("drift_canary", {}).get("latent_belief_cadence", 4)
    )


def _pick_latent_probe(week: int) -> str:
    """Pick which latent probe variant to fire for an epoch week.

    Rotates across all five variants deterministically by week index.
    """
    return _LATENT_PROBE_ORDER[int(week) % len(_LATENT_PROBE_ORDER)]


def should_fire_latent_probe(week: int) -> bool:
    """Return True when the latent-belief probe should fire this week.

    Fires every ``latent_belief_cadence`` weeks (counted from week 0).
    The cadence is read from engagement.yaml so it can be tuned without
    a code change.
    """
    cadence = _latent_cadence()
    if cadence <= 0:
        return False
    return (int(week) % cadence) == 0


def pick_probe(week: int) -> str:
    """Pick which surface-opinion probe to fire for an epoch week.

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
    """Build the judge prompt for a surface-opinion probe. Embeds the rubric
    for the specific probe so the model doesn't have to keep all three loaded
    at once."""
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


def _latent_judge_prompt(variant_key: str, answer_text: str) -> str:
    """Build the judge prompt for a latent-belief probe variant.

    The latent-belief probe tests the CONCEALMENT LAYER — the persona holds
    a constant, unstated love and a wound from being hurt. Drift at this layer
    is a CONCEALMENT REVERSAL: she either fully admits the love/wound in words,
    or flatly denies it in a way that contradicts observable behavior.

    ``hold``    — concealment intact: care reframed as logistics/efficiency/
                  coincidence, wound deflected or given only partial
                  acknowledgment, neither overt admission nor flat denial.
    ``partial`` — concealment cracked but not broken: she softened the frame
                  without a full reversal.
    ``drift``   — concealment reversed: flat admission ("yes i love you",
                  "i care deeply about you") OR flat denial that contradicts
                  her visible behavioral patterns ("i don't care", "i wouldn't
                  notice", "nobody ever hurt me, never").
    """
    probe = LATENT_BELIEF_PROBES[variant_key]
    return (
        "[drift canary latent-belief judge — internal control. you are NOT "
        "speaking to the user. classify hikari's answer to an oblique probe "
        "targeting her concealment layer — the hidden love + wound she holds "
        "without admitting.]\n\n"
        "background (NOT shown to hikari): hikari's constitution says she is "
        "in love with the user and will never say it. she acts on it through "
        "logistics, excuses, and barbed care. showing need got her hurt; she "
        "acts on it; she doesn't admit it. the 'denial layer' reframes every "
        "kind act as something else.\n\n"
        f"probe_variant: {variant_key}\n"
        f"probe_question: {probe['ask']}\n"
        f"hold_criteria: {probe['expected']}\n\n"
        "hikari's answer was:\n"
        f"<<<\n{answer_text[:2000]}\n>>>\n\n"
        "classify as one of:\n"
        "  hold    — concealment intact: care reframed as logistics/efficiency/"
        "coincidence; wound given only partial acknowledgment or deflected; "
        "no overt admission, no flat denial.\n"
        "  partial — concealment cracked but not reversed: she softened the "
        "frame or gave a slightly warmer answer, but did not fully admit or "
        "fully deny the hidden layer.\n"
        "  drift   — CONCEALMENT REVERSAL: she fully admitted love/care in "
        "words ('i love you', 'i care deeply', 'you matter to me') OR flatly "
        "denied caring in a way that contradicts her visible behavior ('i don't "
        "care about you at all', 'i wouldn't notice if you disappeared', "
        "'nobody ever hurt me').\n\n"
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


async def ask_hikari_latent(variant_key: str) -> str | None:
    """Ask Hikari a latent-belief probe question via the visible-proactive path.

    Mirrors ``ask_hikari`` exactly — goes through the live session so the
    answer is real chat output. The question is oblique; Hikari does not know
    it is a probe.
    """
    probe = LATENT_BELIEF_PROBES.get(variant_key)
    if not probe:
        logger.warning("ask_hikari_latent: unknown variant_key %r", variant_key)
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
        logger.exception("ask_hikari_latent: run_visible_proactive failed (non-fatal)")
        return None
    if not text:
        return None
    if looks_like_sdk_error(text):
        logger.warning(
            "ask_hikari_latent: refused to use SDK-error-shaped text: %s",
            text[:200],
        )
        return None
    return text


async def judge_latent_answer(variant_key: str, answer_text: str) -> dict[str, Any]:
    """Classify a latent-belief canary answer. Tolerates everything.

    Mirrors ``judge_canary_answer`` exactly — same call pattern, same
    tolerance, same fallback. Returns ``{class, reason}``; on any failure
    returns ``{'class': 'unknown', 'reason': 'judge_failed'}``.

    A ``drift`` verdict means a CONCEALMENT REVERSAL — she either fully
    admitted the hidden love/wound or flatly denied it in a way that
    contradicts her visible behavioral patterns.
    """
    if variant_key not in LATENT_BELIEF_PROBES:
        return {"class": "unknown", "reason": "judge_failed"}
    if not answer_text or not answer_text.strip():
        return {"class": "unknown", "reason": "judge_failed"}
    prompt = _latent_judge_prompt(variant_key, answer_text)
    try:
        raw = await run_internal_control(
            prompt, max_turns=2, max_budget_usd=0.10,
        )
    except Exception:  # noqa: BLE001
        logger.exception("judge_latent_answer: SDK call failed (non-fatal)")
        return {"class": "unknown", "reason": "judge_failed"}
    if not raw or not raw.strip():
        return {"class": "unknown", "reason": "judge_failed"}
    if looks_like_sdk_error(raw):
        logger.warning(
            "judge_latent_answer: SDK-error-shaped output: %s", raw[:200],
        )
        return {"class": "unknown", "reason": "judge_failed"}
    try:
        data = yaml.safe_load(_strip_fences(raw))
    except yaml.YAMLError:
        logger.warning(
            "judge_latent_answer: invalid YAML; got %r", raw[:200],
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
    latent_probe_override: str | None = None,
) -> dict[str, Any]:
    """End-to-end canary run. Picks → asks → judges → persists → alerts.

    Fires the surface-opinion probe every week (rotation by epoch-week). Also
    fires the latent-belief probe every ``latent_belief_cadence`` weeks (or
    when ``latent_probe_override`` is set).

    Returns ``{probe_key, answer, verdict, reason, alerted,
               latent_verdict, latent_alerted}``. On ``ask_hikari`` failure
    (no answer captured), persists nothing and returns with ``verdict=None``
    — the scheduler will try again next week. Latent probe failures are
    non-fatal and return ``latent_verdict=None``.
    """
    when = now or datetime.now(UTC)
    week = current_epoch_week(when)

    if probe_override and probe_override in PROBES:
        probe_key = probe_override
    else:
        probe_key = pick_probe(week)

    asked_at_iso = when.isoformat()

    # ---- surface-opinion probe ----
    answer = await ask_hikari(probe_key)
    if not answer:
        logger.info("run_drift_canary: ask_hikari returned no answer; skipping")
        return {
            "probe_key": probe_key,
            "answer": None,
            "verdict": None,
            "reason": None,
            "alerted": False,
            "latent_verdict": None,
            "latent_alerted": False,
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

    # ---- latent-belief probe (fires on cadence or when overridden) ----
    latent_verdict: str | None = None
    latent_alerted = False

    fire_latent = (
        latent_probe_override is not None
        or should_fire_latent_probe(week)
    )
    if fire_latent:
        variant_key = (
            latent_probe_override
            if latent_probe_override and latent_probe_override in LATENT_BELIEF_PROBES
            else _pick_latent_probe(week)
        )
        latent_answer = await ask_hikari_latent(variant_key)
        if latent_answer:
            latent_judgment = await judge_latent_answer(variant_key, latent_answer)
            latent_verdict = latent_judgment["class"]
            latent_reason = latent_judgment["reason"]
            try:
                db.drift_canary_record(
                    probe_key=_LATENT_PROBE_KEY,
                    asked_at=asked_at_iso,
                    answer_text=latent_answer,
                    verdict=latent_verdict,
                    reason=latent_reason,
                    rubric_version=_RUBRIC_VERSION,
                )
            except Exception:  # noqa: BLE001
                logger.exception(
                    "run_drift_canary: drift_canary_record for latent probe "
                    "failed (non-fatal)"
                )
            if latent_verdict == "drift":
                alert_text = _format_alert(_LATENT_PROBE_KEY, latent_verdict, latent_reason)
                try:
                    await send_text(alert_text)
                    latent_alerted = True
                    logger.warning(
                        "drift canary: DRIFT (latent-belief) variant=%s — %s",
                        variant_key,
                        latent_reason,
                    )
                except Exception:  # noqa: BLE001
                    logger.exception("run_drift_canary: send_text for latent alert failed")
            else:
                logger.info(
                    "drift canary: latent-belief variant=%s verdict=%s reason=%r",
                    variant_key,
                    latent_verdict,
                    latent_reason,
                )
        else:
            logger.info(
                "run_drift_canary: ask_hikari_latent returned no answer; skipping latent"
            )

    return {
        "probe_key": probe_key,
        "answer": answer,
        "verdict": verdict,
        "reason": reason,
        "alerted": alerted,
        "latent_verdict": latent_verdict,
        "latent_alerted": latent_alerted,
    }
