"""Ghost-of-Future-Self letter routine.

Once a month (first Sunday of the month at 10:00 local by default), Hikari
composes a letter written AS the user, 5 years from now, reflecting on a
decision drawn from the past 30 days of real activity. Inspired by the MIT
Media Lab "Future You" project.

Pipeline: ``gather_month_data`` → ``pick_decision_theme`` (one cheap LLM
turn) → ``build_composition_prompt`` → ``compose_letter`` (one larger LLM
turn) → ``write_letter_file`` + ``db.future_letter_insert`` →
``send_text`` (chunked if needed) → ``db.future_letter_mark_sent``.

The letter is private to the user (no sharing). It's persisted in two places:
``data/future_letters/YYYY-MM.md`` for human-readable durability and
``future_letters`` table for queryable history. Idempotent — re-running on the
same month after a row exists is a no-op (returns ``False``).

Honesty mechanism (the whole point of the feature is that it can't be a toy
summary): the composition prompt enforces (a) citation requirement — must
draw on at least 4 dated entries from the evidence block; (b) friction
mandate — must include at least one "things didn't go as planned" thread;
(c) sparse-data veto — if the past 30 days produced <N receipt entries AND
no episodes, the model returns NO_LETTER and the scheduler skips the send.
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from agents import cadence, config as cfg
from agents.runtime import looks_like_sdk_error, run_internal_control
from storage import db

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).parent.parent
DEFAULT_FILE_DIR = "data/future_letters"


# ---------- TZ helper ----------

def _resolve_local_tz():
    """Resolve the user's local TZ via HOME_TZ env, falling back to UTC.
    Same pattern as evening_diary / daily_checkin so all scheduled routines
    agree on "today"."""
    import os
    import zoneinfo

    name = os.environ.get("HOME_TZ", "UTC")
    try:
        return zoneinfo.ZoneInfo(name)
    except Exception:  # noqa: BLE001
        return zoneinfo.ZoneInfo("UTC")


def _current_month_iso() -> str:
    return datetime.now(_resolve_local_tz()).strftime("%Y-%m")


# ---------- gather ----------

def _gather_receipts_30d(cutoff_iso: str) -> dict[str, list[dict[str, str]]]:
    """Pull receipt entries from the day_receipt DB for the past 30 days.

    Returns a dict keyed by category (made/moved/learned/avoided) with a
    list of ``{'date': YYYY-MM-DD, 'text': ...}`` dicts, newest-first within
    each category. We hit the day_receipt sqlite directly rather than going
    through ``get_receipt`` (which is day-by-day) because pulling 30 days
    that way is 30 connection opens.
    """
    from tools.day_receipt._db import connect

    out: dict[str, list[dict[str, str]]] = {
        "made": [], "moved": [], "learned": [], "avoided": [],
    }
    try:
        with connect() as conn:
            rows = conn.execute(
                "SELECT receipt_date, category, text FROM entries "
                "WHERE receipt_date >= ? "
                "ORDER BY receipt_date DESC, id DESC",
                (cutoff_iso,),
            ).fetchall()
        for r in rows:
            cat = str(r["category"])
            if cat in out:
                out[cat].append({
                    "date": str(r["receipt_date"]),
                    "text": str(r["text"]),
                })
    except sqlite3.OperationalError as e:
        # Brand-new install: day_receipt.db may not exist yet. Treat as
        # empty rather than failing the whole letter job.
        logger.info("future_letter: day_receipt DB not ready (%s); 0 receipts", e)
    except Exception:
        logger.exception("future_letter: failed to fetch receipts")
    return out


def _gather_episodes_30d(cutoff_iso: str) -> list[dict[str, Any]]:
    """Episodes from the past 30 days, newest-first. Capped at 30 to keep
    the prompt manageable — we want a sample of salient moments, not the
    full log."""
    try:
        with db._conn() as conn:
            rows = conn.execute(
                "SELECT date, summary, importance FROM episodes "
                "WHERE date >= ? "
                "ORDER BY date DESC, id DESC LIMIT 30",
                (cutoff_iso,),
            ).fetchall()
        return [
            {
                "date": str(r["date"]),
                "summary": str(r["summary"] or ""),
                "importance": int(r["importance"] or 5),
            }
            for r in rows
        ]
    except Exception:
        logger.exception("future_letter: failed to fetch episodes")
        return []


def _gather_character_thoughts_30d(cutoff_dt: str) -> list[dict[str, str]]:
    """Hikari's private diary entries from the past 30 days (capped at 15).

    character_thoughts is the "what Hikari noticed" feed — quieter signal
    than receipts (which are user-logged) but richer in nuance. Capped to
    avoid drowning the prompt in noise.
    """
    try:
        with db._conn() as conn:
            rows = conn.execute(
                "SELECT created_at, thought FROM character_thoughts "
                "WHERE created_at >= ? "
                "ORDER BY created_at DESC LIMIT 15",
                (cutoff_dt,),
            ).fetchall()
        return [
            {
                "created_at": str(r["created_at"]),
                "thought": str(r["thought"] or ""),
            }
            for r in rows
        ]
    except Exception:
        logger.exception("future_letter: failed to fetch character_thoughts")
        return []


def _gather_open_tasks() -> list[dict[str, Any]]:
    """Pending tasks (open loops), capped at 10. Salient because anything
    still open at month-end is something the user (or Hikari) cares about."""
    try:
        with db._conn() as conn:
            rows = conn.execute(
                "SELECT subject, importance, created_at, last_mention_at "
                "FROM tasks WHERE status IN ('pending','in_progress') "
                "ORDER BY importance DESC, last_mention_at DESC NULLS LAST "
                "LIMIT 10",
            ).fetchall()
        return [
            {
                "subject": str(r["subject"] or ""),
                "importance": int(r["importance"] or 5),
                "created_at": str(r["created_at"] or ""),
                "last_mention_at": str(r["last_mention_at"] or ""),
            }
            for r in rows
        ]
    except Exception:
        logger.exception("future_letter: failed to fetch open tasks")
        return []


def _gather_weekly_consolidations() -> list[dict[str, Any]]:
    """Last 4 weekly consolidation snapshots. These are Hikari's synthesized
    view of "what i noticed about him" each week — exactly the cross-week
    perspective the letter needs."""
    try:
        return db.weekly_consolidations_recent(limit=4)
    except Exception:
        logger.exception("future_letter: failed to fetch weekly consolidations")
        return []


async def gather_month_data(month_iso: str) -> dict[str, Any]:
    """Collect everything the composer needs into one dict of primitives.

    ``month_iso`` is informational; the actual window is the rolling 30 days
    ending now. This avoids edge-case empty months when the job fires early
    in the calendar month.
    """
    now = datetime.now(_resolve_local_tz())
    cutoff_date = (now - timedelta(days=30)).date()
    cutoff_iso = cutoff_date.isoformat()
    cutoff_dt = cutoff_date.isoformat()  # character_thoughts.created_at is ISO

    return {
        "month_iso": month_iso,
        "window_start": cutoff_iso,
        "window_end": now.date().isoformat(),
        "receipts": _gather_receipts_30d(cutoff_iso),
        "episodes": _gather_episodes_30d(cutoff_iso),
        "character_thoughts": _gather_character_thoughts_30d(cutoff_dt),
        "open_tasks": _gather_open_tasks(),
        "weekly_consolidations": _gather_weekly_consolidations(),
    }


# ---------- evidence formatting ----------

def _format_evidence_block(data: dict[str, Any], *,
                           per_cat_cap: int = 20) -> str:
    """Render the structured data as a dated-evidence block for the prompts.

    Every line is prefixed with a date so the LLM has concrete anchors to
    cite. Caps per-category at ``per_cat_cap`` so the prompt doesn't blow
    its budget on a hyper-productive month.
    """
    receipts: dict[str, list[dict[str, str]]] = data.get("receipts") or {}
    episodes = data.get("episodes") or []
    thoughts = data.get("character_thoughts") or []
    tasks = data.get("open_tasks") or []
    weekly = data.get("weekly_consolidations") or []

    parts: list[str] = []
    parts.append(
        f"WINDOW: {data.get('window_start')} → {data.get('window_end')}",
    )

    for cat in ("made", "moved", "learned", "avoided"):
        entries = list(receipts.get(cat) or [])[:per_cat_cap]
        parts.append(f"\nRECEIPTS ({cat}, {len(entries)}):")
        if not entries:
            parts.append("  (none)")
        else:
            for e in entries:
                parts.append(f"  [{e['date']}] {e['text']}")

    parts.append(f"\nEPISODES ({len(episodes)}):")
    if not episodes:
        parts.append("  (none)")
    else:
        for e in episodes[:15]:
            snippet = e["summary"][:200].replace("\n", " ")
            parts.append(f"  [{e['date']}] {snippet}")

    parts.append(f"\nCHARACTER_THOUGHTS ({len(thoughts)}):")
    if not thoughts:
        parts.append("  (none)")
    else:
        for t in thoughts[:10]:
            ts = t["created_at"][:10]
            snippet = t["thought"][:200].replace("\n", " ")
            parts.append(f"  [{ts}] {snippet}")

    parts.append(f"\nOPEN_TASKS ({len(tasks)}):")
    if not tasks:
        parts.append("  (none)")
    else:
        for t in tasks:
            parts.append(
                f"  - {t['subject']} "
                f"(importance {t['importance']}, "
                f"last_mention {t['last_mention_at'][:10] or 'never'})",
            )

    parts.append(f"\nWEEKLY_CONSOLIDATIONS ({len(weekly)}):")
    if not weekly:
        parts.append("  (none)")
    else:
        for w in weekly:
            snippet = (w.get("summary_text") or "")[:300].replace("\n", " ")
            parts.append(
                f"  [week ending {w.get('week_ending')}] {snippet}",
            )

    return "\n".join(parts)


# ---------- theme picker ----------

def _has_enough_data(data: dict[str, Any], min_receipts: int) -> bool:
    """Bar for "this month had enough signal to write about." Used by the
    orchestrator BEFORE spending money on the theme/composition passes.
    """
    receipt_count = sum(
        len(v) for v in (data.get("receipts") or {}).values()
    )
    episode_count = len(data.get("episodes") or [])
    return receipt_count >= min_receipts or episode_count > 0


async def pick_decision_theme(data: dict[str, Any]) -> str | None:
    """One cheap LLM turn: read the evidence and name a "decision X" the
    letter should center on. Returns one sentence, or None on failure.

    The prompt prefers themes that have MIXED evidence (some progress, some
    friction) over purely-positive ones — that biases the letter toward
    honest reflection rather than self-congratulation.
    """
    evidence = _format_evidence_block(data, per_cat_cap=15)
    budget = float(cfg.get("future_letter.theme_picker_max_budget_usd", 0.05))

    prompt = (
        "You are looking at one month of real activity from a user's life. "
        "Pick ONE decision or pattern that should be the center of a "
        "reflection letter — written as if from this person 5 years from "
        "now, looking back on what they decided here.\n\n"
        f"{evidence}\n\n"
        "Rules:\n"
        "- Pick something with MIXED evidence (both progress AND avoidance/"
        "friction). Pure-positive themes make the letter saccharine; pure-"
        "negative ones make it morose.\n"
        "- Prefer themes drawn from RECEIPTS or OPEN_TASKS over inferred ones — "
        "this should be a real decision the data shows, not invented.\n"
        "- One sentence. Start with 'the decision to' or 'the choice to'.\n"
        "- Be specific. 'the decision to focus on X' beats 'the choice to work hard'.\n"
        "- If the data is too sparse to name a decision honestly, output "
        "NO_THEME on its own line.\n\n"
        "Output ONLY the one sentence (or NO_THEME). No preamble."
    )

    try:
        raw = (await run_internal_control(
            prompt, max_turns=2, max_budget_usd=budget,
        )).strip()
    except Exception:
        logger.exception("future_letter: theme picker raised")
        return None
    if not raw:
        return None
    if looks_like_sdk_error(raw):
        logger.warning(
            "future_letter: theme picker returned SDK error string: %r",
            raw[:120],
        )
        return None
    if raw.upper().startswith("NO_THEME"):
        return None
    # Trim to first sentence/line — defend against the model adding extra prose.
    first_line = raw.split("\n", 1)[0].strip()
    return first_line[:200] if first_line else None


# ---------- composition ----------

def build_composition_prompt(data: dict[str, Any], theme: str,
                             user_age: int) -> str:
    """Render the letter-writing prompt. The voice trick: the model writes
    AS the user at age (user_age + 5), not as Hikari.
    """
    evidence = _format_evidence_block(data, per_cat_cap=20)
    future_age = user_age + 5
    # window_end is always populated by gather_month_data, but be defensive
    # for direct callers (e.g. tests passing a hand-rolled dict).
    window_end = str(data.get("window_end") or _current_month_iso() + "-01")
    try:
        current_year = int(window_end[:4])
    except ValueError:
        current_year = datetime.now(_resolve_local_tz()).year
    future_year = current_year + 5

    return (
        f"You are writing a letter from the user at age {future_age}, "
        f"looking back 5 years to now (age {user_age}, the month ending "
        f"{window_end}). Five-years-ago you decided {theme}\n\n"
        "Below is the evidence of what was actually happening in this past "
        "month — real data from the user's life. The letter must draw on "
        "this evidence, not invent things.\n\n"
        f"{evidence}\n\n"
        "Rules — these are not suggestions:\n"
        "1. Write in first person AS the user at age "
        f"{future_age}, not as Hikari. The reader (your past self) is the "
        f"user at age {user_age}. Address them directly: 'you' or no pronoun.\n"
        "2. Cite at least 4 specific entries from the RECEIPTS / EPISODES / "
        "CHARACTER_THOUGHTS above — by date or by their content. Do not "
        "invent facts not present in the evidence. If you want to claim "
        "something happened, point to an entry that shows it.\n"
        "3. Include at least ONE thread where things didn't go as planned — "
        "drawn from the 'avoided' receipts, an OPEN_TASK that stayed open, "
        "a thought that flagged friction, or a setback in the episodes. "
        "Pure-progress letters read like AI-generated motivation posters; "
        "this one shouldn't.\n"
        "4. Plain prose paragraphs. 500-800 words. No bullet lists, no "
        "headers, no markdown.\n"
        "5. Avoid inspirational-poster language ('embrace the journey', "
        "'lean into', 'unlock your potential', 'the only constant is "
        "change'). This is a real letter to yourself, not a TED talk.\n"
        f"6. Open with: 'hey. it's {future_year}.' — then the letter.\n"
        "7. Do NOT mention Hikari. She is not in this letter. The letter "
        "is from the user to themselves.\n\n"
        "If the evidence is too sparse to write honestly (fewer than 5 "
        "receipt entries AND no episodes), output NO_LETTER on its own "
        "line and nothing else.\n\n"
        "Output ONLY the letter (or NO_LETTER). No preamble, no quotes, no "
        "markdown fence."
    )


async def compose_letter(data: dict[str, Any], theme: str) -> str | None:
    """Run the composition prompt and return the body, or None on
    NO_LETTER / SDK error / empty / sparse-data veto."""
    user_age = int(cfg.get("future_letter.user_age", 26))
    budget = float(cfg.get("future_letter.composition_max_budget_usd", 0.30))
    prompt = build_composition_prompt(data, theme, user_age)

    try:
        raw = (await run_internal_control(
            prompt, max_turns=3, max_budget_usd=budget,
        )).strip()
    except Exception:
        logger.exception("future_letter: composition raised")
        return None
    if not raw:
        return None
    if looks_like_sdk_error(raw):
        logger.warning(
            "future_letter: composition returned SDK error string: %r",
            raw[:120],
        )
        return None
    if raw.upper().startswith("NO_LETTER"):
        logger.info("future_letter: model declined (NO_LETTER)")
        return None
    return raw


# ---------- file + chunking ----------

def write_letter_file(month_iso: str, body: str, theme: str, *,
                      root: Path | None = None) -> Path:
    """Write the letter to ``<root>/<file_dir>/<month_iso>.md`` with a small
    header for context. Idempotent — leaves existing files alone."""
    file_dir = str(cfg.get("future_letter.file_dir", DEFAULT_FILE_DIR))
    base = (root or REPO_ROOT) / file_dir
    base.mkdir(parents=True, exist_ok=True)
    target = base / f"{month_iso}.md"
    if target.exists():
        return target
    content = (
        f"# future letter — {month_iso}\n\n"
        f"theme: {theme}\n\n"
        f"---\n\n"
        f"{body.rstrip()}\n"
    )
    target.write_text(content, encoding="utf-8")
    return target


def _chunk_for_telegram(body: str, max_chars: int) -> list[str]:
    """Split ``body`` into chunks no larger than ``max_chars``, preferring
    paragraph boundaries (``\\n\\n``) and falling back to line boundaries.

    Single chunk when the body fits — most letters won't need chunking.
    """
    if len(body) <= max_chars:
        return [body]

    chunks: list[str] = []
    remaining = body
    while len(remaining) > max_chars:
        # Find the latest paragraph break before the limit.
        cut = remaining.rfind("\n\n", 0, max_chars)
        if cut <= 0:
            cut = remaining.rfind("\n", 0, max_chars)
        if cut <= 0:
            cut = max_chars  # hard cut as a last resort
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks


# ---------- orchestrator ----------

async def run_future_letter(
    send_text,
    *,
    today: str | None = None,
    root: Path | None = None,
) -> bool:
    """End-to-end orchestrator: dedup → gather → theme → compose → write →
    DB row → deliver via send_text.

    ``send_text`` matches the bridge's proactive send callback:
    ``async def send_text(text: str) -> tuple[str, int | None, bool]``.

    Returns True if a new letter was composed AND a send was attempted this
    call, False if dedup hit, sparse-data veto fired, theme/compose declined,
    or any earlier stage failed. The DB insert and file write happen even if
    the send fails — the letter is preserved for a manual re-send.

    Dedup: ``runtime_state.future_letter_last_month`` holds the YYYY-MM of
    the last successful send. Compared against the current month_iso; if
    they match we skip immediately. Also: a row in ``future_letters`` for
    this month acts as a second-line guard (the table has UNIQUE month_iso).
    """
    month_iso = today or _current_month_iso()

    # Dedup gate 1: runtime_state. Fast.
    last_sent = db.runtime_get("future_letter_last_month")
    if last_sent and str(last_sent).strip() == month_iso:
        logger.info(
            "future_letter: already ran for %s (runtime_state); skipping",
            month_iso,
        )
        return False

    # Dedup gate 2: existing row. Catches the case where last_sent wasn't
    # written (early failure) but we already composed and persisted.
    if db.future_letter_get(month_iso):
        logger.info(
            "future_letter: row exists for %s; skipping recompose",
            month_iso,
        )
        # Backfill the runtime_state marker so the cheap gate catches it next time.
        db.runtime_set("future_letter_last_month", month_iso)
        return False

    min_receipts = int(cfg.get("future_letter.min_receipts_for_letter", 5))

    data = await gather_month_data(month_iso)
    if not _has_enough_data(data, min_receipts):
        logger.info(
            "future_letter: sparse data for %s "
            "(receipts<%d, no episodes); skipping",
            month_iso, min_receipts,
        )
        return False

    theme = await pick_decision_theme(data)
    if not theme:
        logger.info("future_letter: theme picker declined for %s", month_iso)
        return False

    body = await compose_letter(data, theme)
    if not body:
        logger.info("future_letter: composer declined for %s", month_iso)
        return False

    # Persist BEFORE attempting send — if Telegram fails, the letter is
    # still recoverable. file write is idempotent; DB row is unique-per-month
    # so a re-fire on the same month after a partial run lands here cleanly.
    try:
        write_letter_file(month_iso, body, theme, root=root)
    except Exception:
        logger.exception("future_letter: write_letter_file failed (non-fatal)")
    try:
        db.future_letter_insert(month_iso, theme, body)
    except sqlite3.IntegrityError:
        # Another process beat us to the insert (UNIQUE constraint). Treat
        # as already-handled — don't double-send.
        logger.info(
            "future_letter: UNIQUE conflict on %s; another run beat us",
            month_iso,
        )
        return False
    except Exception:
        logger.exception("future_letter: future_letter_insert failed")
        return False

    # Deliver. Preamble is a single Hikari-voice line so the user knows what's
    # arriving without breaking the letter's first-person frame.
    chunk_chars = int(cfg.get("future_letter.telegram_chunk_chars", 3800))
    preamble = "i made you something. read it when you have a sec."
    chunks = _chunk_for_telegram(body, chunk_chars)

    send_ok = True
    last_tg_id: int | None = None
    try:
        await send_text(preamble)
        for chunk in chunks:
            result = await send_text(chunk)
            if isinstance(result, tuple) and len(result) == 3:
                _, raw_tg_id, ok = result
                send_ok = send_ok and bool(ok)
                try:
                    last_tg_id = int(raw_tg_id) if raw_tg_id is not None else last_tg_id
                except (TypeError, ValueError):
                    pass
            else:
                send_ok = send_ok and bool(result) if result is not None else send_ok
    except Exception:
        logger.exception("future_letter: send failed")
        send_ok = False

    if send_ok:
        try:
            db.future_letter_mark_sent(month_iso)
            db.runtime_set("future_letter_last_month", month_iso)
        except Exception:
            logger.exception(
                "future_letter: marking sent failed (letter persisted, "
                "manual re-send possible)"
            )
        try:
            db.proactive_event_insert(
                source="future_letter_send",
                pattern="ceremony",
                payload_json="{}",
                telegram_message_id=last_tg_id,
            )
        except Exception:
            logger.exception(
                "future_letter: proactive_event_insert failed (non-fatal)"
            )
        cadence.record_ceremony_sent("future_letter_send")
        logger.info(
            "future_letter: composed + delivered for %s (theme=%r, %d chars, %d chunks)",
            month_iso, theme[:60], len(body), len(chunks),
        )
    else:
        logger.warning(
            "future_letter: composed + persisted for %s but send failed; "
            "row in future_letters table is unsent",
            month_iso,
        )
    return True
