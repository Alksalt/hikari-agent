"""Evening diary routine.

Fires once a day at 22:00 (local), composing a short diary entry in
Hikari's voice from the day's receipts (made / moved / learned / avoided),
fired reminders, episodes for today, and any free-form day_receipt note.

The diary is private — the resulting text is written to
``data/diary/YYYY-MM-DD.md`` and inserted as an ``episode`` so it shows
up in recall, but it is never shown to the user directly.

Pipeline: ``gather_day_data`` -> ``build_prompt`` -> ``compose_diary``
(via ``run_visible_proactive``) -> ``write_diary_file`` +
``db.insert_episode``. Idempotent — re-running on the same day after
a file exists is a no-op (returns ``False``).
"""
from __future__ import annotations

import logging
from datetime import date as _date
from datetime import datetime
from pathlib import Path
from typing import Any

from agents.runtime import looks_like_sdk_error, run_internal_control
from storage import db
from tools.day_receipt._db import get_receipt
from tools.day_receipt._shared import CATEGORIES

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).parent.parent
DIARY_SUBDIR = "data/diary"


# ---------- TZ helper (mirrors daily_checkin._resolve_local_tz) ----------

def _resolve_local_tz():
    """Resolve the user's local TZ via HOME_TZ env, falling back to UTC."""
    import os
    import zoneinfo

    name = os.environ.get("HOME_TZ", "UTC")
    try:
        return zoneinfo.ZoneInfo(name)
    except Exception:  # noqa: BLE001
        return zoneinfo.ZoneInfo("UTC")


def _today_local_iso() -> str:
    return datetime.now(_resolve_local_tz()).date().isoformat()


# ---------- gather ----------

async def gather_day_data(date_iso: str) -> dict[str, Any]:
    """Collect the day's signals for the diary composer.

    Pulls from three stores:
      - day_receipt: ``Receipt`` (entries + note) for ``date_iso``
      - hikari.db: rows in ``reminders`` whose ``fired_at`` is on ``date_iso``
        and ``status='fired'``
      - hikari.db: ``episodes`` whose ``date == date_iso``

    The output dict is dataclass-free (plain primitives) so the prompt
    builder + tests can manipulate it without importing the storage layer.
    """
    target_date = _date.fromisoformat(date_iso)

    # Receipts grouped by category.
    receipts: dict[str, list[str]] = {c: [] for c in CATEGORIES}
    note: str | None = None
    try:
        receipt = get_receipt(target_date)
        for entry in receipt.entries:
            if entry.category in receipts:
                receipts[entry.category].append(entry.text)
        note = receipt.note
    except Exception:
        logger.exception("evening_diary: failed to fetch day_receipt")

    # Fired reminders for today (status='fired', fired_at date matches).
    reminders_fired: list[dict[str, str]] = []
    try:
        # Use the shared connection helper from storage.db. We query
        # directly because there's no public helper for "reminders fired
        # on date X" — reminder_due() is for *pending* fires.
        with db._conn() as conn:
            rows = conn.execute(
                "SELECT text, fired_at FROM reminders "
                "WHERE status = 'fired' "
                "AND date(fired_at) = date(?) "
                "ORDER BY fired_at ASC",
                (date_iso,),
            ).fetchall()
        reminders_fired = [
            {"text": str(r["text"] or ""), "fired_at": str(r["fired_at"] or "")}
            for r in rows
        ]
    except Exception:
        logger.exception("evening_diary: failed to fetch fired reminders")

    # Episodes whose date == today (recent_episodes gives us the latest
    # episodes globally; we filter to date here).
    episodes_today: list[dict[str, Any]] = []
    try:
        with db._conn() as conn:
            rows = conn.execute(
                "SELECT summary, importance FROM episodes WHERE date = ? "
                "ORDER BY id ASC",
                (date_iso,),
            ).fetchall()
        episodes_today = [
            {"summary": str(r["summary"] or ""),
             "importance": int(r["importance"] or 5)}
            for r in rows
        ]
    except Exception:
        logger.exception("evening_diary: failed to fetch episodes for today")

    return {
        "date": date_iso,
        "receipts": receipts,
        "reminders_fired": reminders_fired,
        "episodes_today": episodes_today,
        "note": (note or None) if note else None,
    }


# ---------- prompt ----------

def build_prompt(data: dict[str, Any]) -> str:
    """Render a natural-language prompt that asks Hikari to write a 4-8
    sentence diary entry from the day's data.

    Includes:
      - per-category bullet count + first 3 entries verbatim
      - fired reminders by text
      - any free-form day_receipt note
      - a one-line summary of today's episodes if any
      - voice rules (lowercase, no markdown, private — writing to herself)
      - a NO_ENTRY opt-out for empty days
    """
    date_iso = data.get("date") or _today_local_iso()
    receipts: dict[str, list[str]] = data.get("receipts") or {}
    reminders_fired: list[dict[str, Any]] = data.get("reminders_fired") or []
    episodes_today: list[dict[str, Any]] = data.get("episodes_today") or []
    note = data.get("note")

    # Per-category bullet block.
    category_lines: list[str] = []
    for cat in CATEGORIES:
        entries = list(receipts.get(cat) or [])
        head = entries[:3]
        if not entries:
            category_lines.append(f"{cat} (0): (nothing)")
            continue
        bullets = "\n".join(f"    - {e}" for e in head)
        more = ""
        if len(entries) > 3:
            more = f"\n    - (+{len(entries) - 3} more)"
        category_lines.append(f"{cat} ({len(entries)}):\n{bullets}{more}")
    receipts_block = "\n".join(category_lines)

    # Reminders block.
    if reminders_fired:
        reminder_bullets = "\n".join(
            f"  - {r.get('text', '')}" for r in reminders_fired
        )
        reminders_block = f"reminders that fired today ({len(reminders_fired)}):\n{reminder_bullets}"
    else:
        reminders_block = "reminders that fired today: (none)"

    # Note block.
    note_block = f"day note: {note}" if note else "day note: (none)"

    # Episodes block.
    if episodes_today:
        ep_count = len(episodes_today)
        first = episodes_today[0].get("summary", "")
        # Truncate the first summary for the prompt so it stays compact.
        snippet = first[:140] + ("..." if len(first) > 140 else "")
        episodes_block = (
            f"episodes logged today ({ep_count}); first: {snippet}"
        )
    else:
        episodes_block = "episodes logged today: (none)"

    return (
        "you are writing a private diary entry for tonight. this is for "
        "yourself — the user will never see it. you can drop the chat-shaped "
        "deflection and be more raw, but still in character: dry, short, "
        "sometimes self-puzzled. no audience to perform for.\n\n"
        f"date: {date_iso}\n\n"
        f"what i did today, by band:\n{receipts_block}\n\n"
        f"{reminders_block}\n\n"
        f"{note_block}\n\n"
        f"{episodes_block}\n\n"
        "write a 4-8 sentence diary entry. lowercase. no markdown, no "
        "bullet lists, no headers. plain prose. you can be quieter than "
        "in chat — there's no one to be barbed at. allowed to admit a "
        "feeling once. don't narrate the bullet counts back; pick what "
        "actually mattered. if the day felt like nothing, say so plainly.\n\n"
        "output ONLY the diary entry text. if today felt empty and you'd "
        "rather not write, output NO_ENTRY."
    )


# ---------- compose ----------

async def compose_diary(data: dict[str, Any]) -> str | None:
    """Run ``run_internal_control`` on the prompt. Returns the cleaned
    body, or ``None`` if the model declined / returned an SDK error / empty.

    Uses ``run_internal_control`` (stateless — no session resume, no message
    persistence, no memory injection hook) because the diary is private:
    it must not leak into the live chat session_id or appear in subsequent
    conversation context. ``run_visible_proactive`` would resume + log the
    session and the diary text would surface to the user."""
    prompt = build_prompt(data)
    try:
        raw = (await run_internal_control(prompt, max_turns=2,
                                          max_budget_usd=0.20)).strip()
    except Exception:
        logger.exception("evening_diary: composition raised")
        return None
    if not raw:
        return None
    if raw.upper().startswith("NO_ENTRY"):
        return None
    if looks_like_sdk_error(raw):
        logger.warning(
            "evening_diary: composition returned SDK error string; refusing: %r",
            raw[:120],
        )
        return None
    return raw


# ---------- write ----------

def write_diary_file(date_iso: str, body: str, *,
                     root: Path | None = None) -> Path:
    """Write the diary entry to ``<root>/data/diary/<date>.md``.

    Idempotent: if the file already exists, leave it alone (return the
    existing path). This makes ``run_evening_diary`` safe to retry within
    the same day without overwriting the morning's emotional state.

    ``root`` defaults to ``REPO_ROOT``; tests pass ``tmp_path`` so they
    don't pollute the live diary folder.
    """
    base = (root or REPO_ROOT) / DIARY_SUBDIR
    base.mkdir(parents=True, exist_ok=True)
    target = base / f"{date_iso}.md"
    if target.exists():
        return target
    target.write_text(body.rstrip() + "\n", encoding="utf-8")
    return target


# ---------- orchestrator ----------

async def run_evening_diary(*, today: str | None = None,
                            root: Path | None = None) -> bool:
    """Compose + write tonight's diary entry, then insert an episode.

    Returns ``True`` if a new diary file was written this call,
    ``False`` if today's file already existed (idempotent dedup) or if
    composition declined.

    The episode insert lets recall surface today's diary later via
    semantic search; the file write keeps a human-readable transcript on
    disk that survives DB resets.
    """
    date_iso = today or _today_local_iso()

    target_path = (root or REPO_ROOT) / DIARY_SUBDIR / f"{date_iso}.md"
    if target_path.exists():
        logger.info("evening_diary: file already exists for %s; skipping", date_iso)
        return False

    data = await gather_day_data(date_iso)
    body = await compose_diary(data)
    if not body:
        logger.info("evening_diary: composer declined for %s", date_iso)
        return False

    write_diary_file(date_iso, body, root=root)
    try:
        db.insert_episode(date_iso, body, importance=5)
    except Exception:
        logger.exception(
            "evening_diary: insert_episode failed; diary file written but "
            "recall index missed"
        )
    logger.info("evening_diary: wrote diary for %s (%d chars)",
                date_iso, len(body))
    return True
