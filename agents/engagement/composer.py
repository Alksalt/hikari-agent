"""Composer: turns a TriggerCandidate into a Hikari-voice message via
run_visible_proactive. Inline template for wiki_new_file only."""
from __future__ import annotations

import logging
from typing import Any

from agents.engagement.triggers import TriggerCandidate
from agents.runtime import run_visible_proactive

logger = logging.getLogger(__name__)


_WIKI_NEW_FILE_TEMPLATE = """\
[proactive nudge — pattern=question, source=wiki_new_file]
the user just wrote a new wiki page. payload below.
write ONE message in your voice (lowercase, 1-3 sentences, no markdown).
RULES:
  - you MUST include the filename from payload.filename VERBATIM.
  - you MUST end with a y/n offer to read/summarize it back.
  - denial layer ok ("i was already in there. anyway —"). no chirpiness.
  - never start with "hey", "how are you", "just checking", "what's up".
  - if you can't write it true to voice with the filename cited, output NO_MESSAGE.
payload:
  filename: {filename}
  folder: {folder}
  h1: {h1}
example shape: "new wiki page just landed — '{filename}'. want me to read it back at you in 3 sentences?"
"""


async def compose(candidate: TriggerCandidate) -> str | None:
    if candidate.source != "wiki_new_file":
        return None
    payload = candidate.payload
    prompt = _WIKI_NEW_FILE_TEMPLATE.format(
        filename=payload.get("filename", ""),
        folder=payload.get("folder", "") or "<root>",
        h1=payload.get("h1", "") or "<no h1>",
    )
    try:
        text = await run_visible_proactive(prompt)
    except Exception:
        logger.exception("compose: run_visible_proactive failed")
        return None
    if not text or "NO_MESSAGE" in text.upper():
        return None
    return text.strip()
