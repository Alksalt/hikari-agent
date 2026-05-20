"""Post-send bookkeeping. Phase 13 (Stream C).

Mark observations and noticings as surfaced only after the final reply has
actually been delivered to the user. Previously this happened during
``inject_memory`` (the UserPromptSubmit hook) — but the model might not
mention the observation, the post_filter could rewrite it out, or the
Telegram send could fail entirely. In all of those cases the observation
was permanently consumed without the user ever seeing it.

The flow:

1. ``inject_memory`` collects the IDs of observations/noticings it injected
   for this turn and stashes them in ``runtime_state`` under
   ``pending_surfaced_observation_ids`` / ``pending_surfaced_noticing_ids``.
2. ``_send_with_choreography`` calls ``mark_pending_surfaced()`` after a
   successful Telegram send + DB append.
3. ``mark_pending_surfaced`` reads the stashed IDs, marks them surfaced
   via ``db.observation_mark_surfaced`` / ``db.noticing_mark_surfaced``,
   then clears the runtime_state keys.

On send failure the runtime_state keys remain populated; the next user
turn's ``inject_memory`` overwrites them (it always sets them anew,
possibly to ``[]``). That's the correct behavior: a missed send doesn't
re-surface the same observation indefinitely — the next turn either
re-injects it (still unsurfaced) or moves on.
"""

from __future__ import annotations

import json
import logging

from storage import db

logger = logging.getLogger(__name__)

OBS_KEY = "pending_surfaced_observation_ids"
NOT_KEY = "pending_surfaced_noticing_ids"


def _pop_ids(key: str) -> list[int]:
    raw = db.runtime_get(key)
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        logger.warning("postsend: %s payload was not valid JSON: %r", key, raw[:120])
        db.runtime_set(key, None)
        return []
    if not isinstance(data, list):
        db.runtime_set(key, None)
        return []
    out: list[int] = []
    for x in data:
        try:
            out.append(int(x))
        except (TypeError, ValueError):
            continue
    db.runtime_set(key, None)
    return out


def mark_pending_surfaced() -> None:
    """Drain the runtime_state keys populated by ``inject_memory`` and mark
    every observation/noticing surfaced. Call after a successful send + DB
    append in ``_send_with_choreography``."""
    for obs_id in _pop_ids(OBS_KEY):
        try:
            db.observation_mark_surfaced(obs_id)
        except Exception:
            logger.exception(
                "postsend: observation_mark_surfaced failed for id=%s", obs_id,
            )
    for not_id in _pop_ids(NOT_KEY):
        try:
            db.noticing_mark_surfaced(not_id)
        except Exception:
            logger.exception(
                "postsend: noticing_mark_surfaced failed for id=%s", not_id,
            )
