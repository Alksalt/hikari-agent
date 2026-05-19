"""One-shot migration from the old hikari-tsukino-bot markdown layout to SQLite.

Run from the hikari-agent repo root:
    uv run python scripts/migrate_from_current.py /path/to/hikari-tsukino-bot/data/users/<owner_id>
    uv run python scripts/migrate_from_current.py <src> --fresh   # wipe target tables first

Effects:
- USER.md known_facts                 -> facts table (deduped by object text)
- USER.md basics                       -> user_profile core_block (no stage)
- MEMORY.md `## about the user`        -> about_user core_block
- MEMORY.md `## shared canon`          -> shared_canon core_block
- SELF.md `## preoccupation`           -> preoccupation core_block
- SELF.md `## staged disclosures`      -> staged_disclosures core_block (historical)
- SELF.md `## things she told the user` -> things_told_user core_block
- SELF.md `## established joke`        -> established_joke core_block
- episodes/YYYY-MM-DD.md               -> episodes table (deduped by date)
- MOOD.md current_arc                  -> mood_arc core_block
- THOUGHTS.md entries                  -> character_thoughts (deduped by content)
- HEARTBEAT.md                         -> runtime_state keys

Idempotency: default behavior dedupes by content hash on facts, episodes, and
thoughts. Core blocks are upserts so they're always idempotent. Use --fresh to
truncate target tables before migrating.
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

import yaml

# Allow running from any cwd
sys.path.insert(0, str(Path(__file__).parent.parent))

from storage import db  # noqa: E402


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def _section(text: str, header: str) -> str:
    """Pull a `## {header}` section body out of a markdown file."""
    pat = re.compile(rf"##\s+{re.escape(header)}\s*\n(.*?)(?=\n##|\Z)",
                     re.DOTALL | re.IGNORECASE)
    m = pat.search(text)
    return m.group(1).strip() if m else ""


# ---------- USER.md ----------

def _parse_user_md(text: str) -> dict:
    out: dict = {"name": "unknown", "exchanges": 0,
                 "open_loops": [], "known_facts": []}
    m = re.search(r"- name:\s*(.+)", text)
    if m:
        out["name"] = m.group(1).strip()
    m = re.search(r"meaningful_exchanges:\s*(\d+)", text)
    if m:
        out["exchanges"] = int(m.group(1))

    loops_body = _section(text, "open_loops")
    if loops_body and loops_body.lower() != "none":
        out["open_loops"] = [
            ln.lstrip("- ").strip()
            for ln in loops_body.splitlines()
            if ln.strip() and not ln.strip().startswith("#")
        ]

    facts_body = _section(text, "known_facts")
    if facts_body and facts_body.lower() != "none yet":
        out["known_facts"] = [
            ln.lstrip("- ").strip()
            for ln in facts_body.splitlines()
            if ln.strip() and not ln.strip().startswith("#")
        ]
    return out


def _migrate_user_md(src: Path) -> None:
    text = _read(src / "USER.md")
    if not text:
        print("  USER.md: missing, skip")
        return
    state = _parse_user_md(text)

    db.upsert_core_block(
        "user_profile",
        f"name: {state['name']}\nmeaningful_exchanges: {state['exchanges']}",
    )
    print(f"  user_profile: name={state['name']!r} exchanges={state['exchanges']}")

    facts_added = facts_skipped = 0
    dated_re = re.compile(r"^\[(\d{4}-\d{2}-\d{2})\]\s+(.*)")
    existing_objects = {
        f["object"]
        for f in db.active_facts(limit=10_000)
        if f.get("subject") == "user"
    }
    for fact in state["known_facts"]:
        m = dated_re.match(fact)
        if m:
            ts = f"{m.group(1)}T00:00:00+00:00"
            text_only = m.group(2).strip()
        else:
            ts = datetime.now(UTC).isoformat()
            text_only = fact
        if text_only in existing_objects:
            facts_skipped += 1
            continue
        db.bulk_insert_facts([{
            "subject": "user", "predicate": "fact", "object": text_only,
            "importance": 5, "confidence": 0.6,
            "valid_from": ts, "created_at": ts,
        }])
        existing_objects.add(text_only)
        facts_added += 1
    print(f"  facts inserted: {facts_added} (skipped dupes: {facts_skipped})")

    loops_added = loops_skipped = 0
    existing_loop_subjects = {t["subject"] for t in db.open_tasks()}
    for loop in state["open_loops"]:
        if loop in existing_loop_subjects:
            loops_skipped += 1
            continue
        db.create_task(subject=loop)
        existing_loop_subjects.add(loop)
        loops_added += 1
    print(f"  open_loops -> tasks: {loops_added} (skipped dupes: {loops_skipped})")


# ---------- MEMORY.md ----------

def _migrate_memory_md(src: Path) -> None:
    text = _read(src / "MEMORY.md")
    if not text.strip():
        print("  MEMORY.md: empty, skip")
        return
    about = _section(text, "about the user")
    canon = _section(text, "shared canon")
    if about:
        db.upsert_core_block("about_user", about[:4000])
        print(f"  about_user: {len(about)} chars")
    if canon:
        db.upsert_core_block("shared_canon", canon[:4000])
        print(f"  shared_canon: {len(canon)} chars")
    if not about and not canon:
        # Fall back to the full file as long_term_memory if neither section parsed
        db.upsert_core_block("long_term_memory", text.strip()[:4000])
        print(f"  long_term_memory (fallback): {len(text)} chars")


# ---------- SELF.md ----------

def _migrate_self_md(src: Path) -> None:
    text = _read(src / "SELF.md")
    if not text.strip():
        print("  SELF.md: missing, skip")
        return
    sections = {
        "preoccupation": _section(text, "preoccupation"),
        "staged_disclosures": _section(text, "staged disclosures"),
        "things_told_user": _section(text, "things she told the user"),
        "established_joke": _section(text, "established joke"),
    }
    written = 0
    for label, body in sections.items():
        if body:
            db.upsert_core_block(label, body[:4000])
            written += 1
    print(f"  SELF.md core_blocks written: {written}")


# ---------- episodes ----------

def _migrate_episodes(src: Path) -> None:
    ep_dir = src / "episodes"
    if not ep_dir.exists():
        print("  episodes/: missing, skip")
        return
    existing_dates = {e["date"] for e in db.recent_episodes(limit=10_000)}
    rows = []
    for p in sorted(ep_dir.glob("????-??-??.md")):
        body = _read(p).strip()
        if not body or p.stem in existing_dates:
            continue
        rows.append({
            "date": p.stem, "summary": body[:4000], "importance": 5,
            "created_at": f"{p.stem}T12:00:00+00:00",
        })
    n = db.bulk_insert_episodes(rows)
    skipped = len(list(ep_dir.glob("????-??-??.md"))) - n
    print(f"  episodes migrated: {n} (skipped dupes: {skipped})")


# ---------- mood ----------

def _migrate_mood(src: Path) -> None:
    text = _read(src / "MOOD.md")
    if not text.strip():
        return
    arc_m = re.search(r"current_arc:\s*(\w+)", text)
    note_m = re.search(r"arc_note:\s*\|\n(.+?)(?=\n\w|$)", text, re.DOTALL)
    arc = arc_m.group(1) if arc_m else "stable"
    note = (note_m.group(1).strip() if note_m else "")
    db.upsert_core_block("mood_arc", f"current_arc: {arc}\nnote: {note}")
    print(f"  mood_arc: {arc}")


# ---------- thoughts ----------

def _migrate_thoughts(src: Path) -> None:
    text = _read(src / "THOUGHTS.md")
    if not text.strip():
        return
    with db._conn() as c:
        existing = {r["thought"] for r in c.execute(
            "SELECT thought FROM character_thoughts"
        ).fetchall()}
    added = skipped = 0
    date_head = re.compile(r"^\d{4}-\d{2}-\d{2}\b")
    for entry in re.split(r"\n## ", text):
        entry = entry.strip()
        if not entry or "\n" not in entry:
            continue
        head, _, body = entry.partition("\n")
        if not date_head.match(head.strip()):
            # not a date-headed entry — the leading file title block
            continue
        body = body.strip()
        if not body:
            continue
        if body in existing:
            skipped += 1
            continue
        db.append_thought(body)
        existing.add(body)
        added += 1
    print(f"  character_thoughts migrated: {added} (skipped dupes: {skipped})")


# ---------- heartbeat ----------

def _migrate_heartbeat(src: Path) -> None:
    text = _read(src / "HEARTBEAT.md")
    if not text.strip():
        return
    lines = [ln for ln in text.splitlines()
             if ln.strip() and not ln.strip().startswith("#")]
    try:
        state = yaml.safe_load("\n".join(lines)) or {}
    except yaml.YAMLError:
        state = {}
    keys = ["silence_until", "last_proactive_sent", "last_user_message",
            "warmth_floor_modifier", "photos_sent_today", "photos_sent_date"]
    n = 0
    for k in keys:
        if state.get(k) is not None:
            db.runtime_set(k, state[k])
            n += 1
    print(f"  runtime_state keys migrated: {n}")


# ---------- fresh ----------

_TRUNCATE_TABLES = (
    "facts", "fts", "vec_facts", "vec_episodes", "episodes", "tasks",
    "character_thoughts", "runtime_state",
)


def _truncate_target() -> None:
    """For --fresh: wipe migration-target tables so the migration produces a clean state.
    Does NOT touch session, core_blocks, or messages (preserved across runs)."""
    with db._conn() as c:
        for t in _TRUNCATE_TABLES:
            try:
                c.execute(f"DELETE FROM {t}")
            except Exception:  # noqa: BLE001
                pass
    print("  --fresh: target tables truncated")


# ---------- main ----------

def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("src", type=Path,
                   help="path to old data/users/<owner_id> directory")
    p.add_argument("--fresh", action="store_true",
                   help="truncate target tables before migrating")
    args = p.parse_args()

    if not args.src.is_dir():
        print(f"error: {args.src} is not a directory")
        sys.exit(2)
    print(f"migrating from: {args.src}")
    if args.fresh:
        _truncate_target()

    _migrate_user_md(args.src)
    _migrate_memory_md(args.src)
    _migrate_self_md(args.src)
    _migrate_episodes(args.src)
    _migrate_mood(args.src)
    _migrate_thoughts(args.src)
    _migrate_heartbeat(args.src)
    print("done.")


if __name__ == "__main__":
    main()
