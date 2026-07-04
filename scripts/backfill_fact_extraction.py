"""One-off: re-run fact extraction over a message window whose daily
reflection skipped LLM extraction (2026-07-02/03 — cheap model returned
non-YAML twice; facts/day went 3,0,0,3).

Usage: uv run python -m scripts.backfill_fact_extraction 2026-07-02T00:00:00+00:00 2026-07-04T00:00:00+00:00
Writes to the LIVE DB — run once, after review of the printed facts.
"""
from __future__ import annotations

import asyncio
import sys

from agents.reflection import _parse_yaml_mapping, apply_new_facts, run_reflection_call
from storage import db


def _build_backfill_prompt(messages: list[dict]) -> str:
    messages_text = "\n".join(
        f"[mid:{m['id']}] {m['role']}: {m['content']}" for m in messages
    )
    return (
        "You are backfilling Hikari's daily reflection for a window that was "
        "skipped. Read the messages and output ONLY valid YAML in this exact "
        "shape (no prose, no code fences):\n\n"
        "new_facts:\n"
        "  - {subject: '', predicate: '', object: '', importance: 5, "
        "confidence: 0.9, source_message_id: 0, source_text: '', "
        "category: 'event|preference|fact'}\n"
        "entities: []\n\n"
        "Extract durable facts the user stated about themselves, their plans, "
        "people, or preferences. Skip small talk. Messages:\n\n"
        f"{messages_text}\n"
    )


async def main(start_iso: str, end_iso: str) -> None:
    msgs = [m for m in db.messages_since(start_iso, exclude_ephemeral=True, limit=500)
            if m["ts"] < end_iso]
    if not msgs:
        print("no messages in window — nothing to do")
        return
    print(f"{len(msgs)} messages in window {start_iso} .. {end_iso}")
    raw = await run_reflection_call(_build_backfill_prompt(msgs))
    data = _parse_yaml_mapping(raw, context="backfill")
    if not data:
        print("LLM returned non-YAML — aborting, nothing written")
        return
    for f in data.get("new_facts") or []:
        print("  fact:", f)
    if input("apply these facts to the LIVE DB? [y/N] ").strip().lower() != "y":
        print("aborted")
        return
    applied = await apply_new_facts(data)
    print(f"applied {applied} facts")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], sys.argv[2]))
