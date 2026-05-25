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
import re

from storage import db

logger = logging.getLogger(__name__)

OBS_KEY = "pending_surfaced_observation_ids"
NOT_KEY = "pending_surfaced_noticing_ids"
DEFER_KEY = "pending_consumed_defer_ids"

_WS_RE = re.compile(r"\s+")


def _normalize(s: str) -> str:
    return _WS_RE.sub(" ", s.strip().lower())


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


def _restash(key: str, obs_id: int) -> None:
    raw = db.runtime_get(key) or "[]"
    try:
        data = json.loads(raw)
        if not isinstance(data, list):
            data = []
    except (TypeError, ValueError):
        data = []
    if obs_id not in data:
        data.append(obs_id)
    db.runtime_set(key, json.dumps(data))


def mark_pending_surfaced(sent_text: str = "") -> None:
    """Drain the runtime_state keys populated by ``inject_memory``.

    Only marks an observation/noticing surfaced if a normalized substring of
    its stored text appears in ``sent_text``. If not mentioned, re-stashes the
    ID so it stays eligible for re-injection on the next turn.

    Pass empty string (or omit) to skip content checking and re-stash all
    pending IDs — used when the send failed or produced no text.
    """
    if not sent_text:
        for obs_id in _pop_ids(OBS_KEY):
            _restash(OBS_KEY, obs_id)
        for not_id in _pop_ids(NOT_KEY):
            _restash(NOT_KEY, not_id)
        # Defer rows keep their IDs for next turn re-injection on send failure.
        # _format_deferred_proactives clears the stash on next invocation.
        return

    sent_norm = _normalize(sent_text)

    for obs_id in _pop_ids(OBS_KEY):
        try:
            obs_text = db.observation_text(obs_id)
            if obs_text and _normalize(obs_text) in sent_norm:
                db.observation_mark_surfaced(obs_id)
            else:
                _restash(OBS_KEY, obs_id)
        except Exception:
            logger.exception("postsend: observation_mark_surfaced failed id=%s", obs_id)

    for not_id in _pop_ids(NOT_KEY):
        try:
            not_text = db.noticing_text(not_id)
            if not_text and _normalize(not_text) in sent_norm:
                db.noticing_mark_surfaced(not_id)
            else:
                _restash(NOT_KEY, not_id)
        except Exception:
            logger.exception("postsend: noticing_mark_surfaced failed id=%s", not_id)

    # Delete deferred proactive rows that were injected and delivered.
    defer_ids = _pop_ids(DEFER_KEY)
    if defer_ids:
        try:
            placeholders = ",".join("?" * len(defer_ids))
            with db._conn() as conn:
                conn.execute(
                    f"DELETE FROM session_scratch WHERE id IN ({placeholders})",
                    defer_ids,
                )
            logger.debug("postsend: cleaned %d deferred proactive row(s)", len(defer_ids))
        except Exception:
            logger.exception("postsend: failed to delete deferred proactive rows %s", defer_ids)
