"""Job-search handoff consumer — phase 3 of the job-search autoscan pipeline.

``../job-search/autoscan.py`` mirrors every notification it emails (hot leads,
digests, frist warnings) into an append-only markdown file (path configured in
``job_handoff.path``). This is the consuming side: ``pull_unprocessed()``
returns fresh entries for the heartbeat prompt so hikari can mention them in
her own voice; ``mark_processed()`` stamps the consumed lines so nothing is
surfaced twice. Entry grammar (written by autoscan, never hand-edited):

    - [YYYY-MM-DD HH:MM] kind: subject — status: unprocessed
        - <detail line>

Only the ``status:`` suffix ever changes, and only from this side. The file is
append-only from the producer, so raw-line matching at mark time is safe even
if autoscan appends between pull and mark (the tiny read-rewrite window is
accepted: producer runs every 2 days at 08:30, heartbeats run a few times a
day — collisions are practically impossible and at worst lose one append).
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from pathlib import Path

from . import config as cfg

logger = logging.getLogger(__name__)

_ENTRY = re.compile(
    r"^- \[(?P<stamp>\d{4}-\d{2}-\d{2} \d{2}:\d{2})\] "
    r"(?P<summary>.+?) — status: unprocessed\s*$"
)
_DETAIL_PREFIX = "    - "


def _path() -> Path | None:
    if not cfg.get("job_handoff.enabled", True):
        return None
    raw = str(cfg.get("job_handoff.path", "") or "").strip()
    if not raw:
        return None
    p = Path(raw).expanduser()
    return p if p.is_file() else None


def pull_unprocessed() -> list[dict]:
    """Fresh unprocessed entries, oldest first, capped at
    ``job_handoff.max_entries``. Entries older than ``max_age_hours`` are
    ignored (left unprocessed on file — stale job alerts are noise, not news).
    """
    path = _path()
    if path is None:
        return []
    max_entries = int(cfg.get("job_handoff.max_entries", 2))
    max_age = timedelta(hours=float(cfg.get("job_handoff.max_age_hours", 72)))
    now = datetime.now()
    out: list[dict] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        logger.exception("job_handoff: read failed")
        return []
    for i, line in enumerate(lines):
        m = _ENTRY.match(line)
        if not m:
            continue
        try:
            ts = datetime.strptime(m["stamp"], "%Y-%m-%d %H:%M")
        except ValueError:
            continue
        if now - ts > max_age:
            continue
        details = []
        for sub in lines[i + 1:]:
            if not sub.startswith(_DETAIL_PREFIX):
                break
            details.append(sub[len(_DETAIL_PREFIX):].strip())
        out.append({
            "raw": line,
            "stamp": m["stamp"],
            "summary": m["summary"].strip(),
            "details": details[:4],
        })
        if len(out) >= max_entries:
            break
    return out


def format_lines(entries: list[dict]) -> str:
    """Compact plain-text block for the heartbeat prompt."""
    parts = []
    for e in entries:
        tail = f" ({'; '.join(e['details'])})" if e["details"] else ""
        parts.append(f"- {e['summary']}{tail}")
    return "\n".join(parts)


def mark_processed(entries: list[dict]) -> None:
    """Flip ``status: unprocessed`` → ``status: processed <date>`` on exactly
    the pulled lines (matched by full raw line, so producer appends between
    pull and mark can't shift targets)."""
    path = _path()
    if path is None or not entries:
        return
    stamp = datetime.now().strftime("%Y-%m-%d")
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        logger.exception("job_handoff: re-read before mark failed")
        return
    for e in entries:
        marked = e["raw"].replace(
            "— status: unprocessed", f"— status: processed {stamp}", 1
        )
        text = text.replace(e["raw"] + "\n", marked + "\n", 1)
    try:
        path.write_text(text, encoding="utf-8")
    except OSError:
        logger.exception("job_handoff: write failed")
