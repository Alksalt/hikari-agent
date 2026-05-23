"""Producer: detects new .md files under the wiki and emits one trigger
per file, deduped by file path. Cap: 2 per 24h."""
from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

from agents import config as cfg
from agents.engagement.triggers import TriggerCandidate
from storage import db

# Filenames that survive: word-chars, dots, hyphens, underscores, spaces.
# 128-char cap blocks prompt-injection payloads stuffed into filenames
# (the guard requires filename echo, which would otherwise force the model
# to repeat hostile content into the Telegram outbound).
_SAFE_FILENAME = re.compile(r"^[\w\-. ]{1,128}\.md$")
# Strip control chars + newlines from h1 — same rationale.
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")

logger = logging.getLogger(__name__)

_RUNTIME_STATE_LAST_SEEN = "engagement.wiki_new_file.last_seen_ts"


def _wiki_root() -> Path | None:
    """Resolve the wiki path. Prefers cfg-set values; falls back to the
    canonical VAULT_ROOT the rest of the wiki tools use (read from the
    Ship-profile / global CLAUDE.md by tools.wiki._shared). Returns None
    if no wiki is configured or the path doesn't exist."""
    p = cfg.get("wiki_path") or cfg.get("morning_brief.wiki_path")
    if p:
        candidate = Path(str(p)).expanduser()
        return candidate if candidate.exists() else None
    try:
        from tools.wiki._shared import VAULT_ROOT
    except Exception:
        return None
    return VAULT_ROOT if VAULT_ROOT.exists() else None


def collect() -> list[TriggerCandidate]:
    """Scan the wiki for .md files modified since the last seen timestamp.
    Returns ≤cap candidates per 24h. Updates the seen-timestamp marker
    only after at least one new file is found."""
    if not bool(cfg.get("engagement.wiki_new_file.enabled", True)):
        return []
    root = _wiki_root()
    if root is None:
        return []
    cap = int(cfg.get("engagement.wiki_new_file.max_per_24h", 2))
    cutoff_raw = db.runtime_get(_RUNTIME_STATE_LAST_SEEN)
    try:
        last_seen = (
            datetime.fromisoformat(cutoff_raw).replace(tzinfo=UTC)
            if cutoff_raw
            else datetime.now(UTC) - timedelta(hours=24)
        )
    except (ValueError, TypeError):
        last_seen = datetime.now(UTC) - timedelta(hours=24)

    candidates: list[TriggerCandidate] = []
    root_resolved = root.resolve()
    try:
        for md in root.rglob("*.md"):
            if md.name.startswith("."):
                continue
            # Reject symlinks pointing outside the vault — rglob follows
            # symlinks by default; a vault-internal symlink to ~/.ssh
            # would otherwise leak filenames + h1 snippets through the
            # composer prompt and the proactive_events table.
            if md.is_symlink():
                continue
            try:
                resolved = md.resolve(strict=True)
                resolved.relative_to(root_resolved)
            except (OSError, ValueError):
                continue
            if not _SAFE_FILENAME.match(md.name):
                continue
            try:
                mtime = datetime.fromtimestamp(md.stat().st_mtime, tz=UTC)
            except OSError:
                continue
            if mtime <= last_seen:
                continue
            try:
                rel = str(md.relative_to(root))
            except ValueError:
                rel = md.name
            h1 = ""
            try:
                with md.open("r", encoding="utf-8", errors="replace") as f:
                    for line in f:
                        s = line.strip()
                        if s.startswith("# "):
                            h1 = s[2:].strip()
                            break
                        if s:
                            h1 = s[:80]
                            break
            except OSError:
                pass
            h1 = _CONTROL_CHARS.sub("", h1)[:80]
            candidates.append(TriggerCandidate(
                source="wiki_new_file",
                pool="user_anchored",
                pattern="question",
                novelty=0.8,
                actionability=0.6,
                confidence=0.9,
                payload={
                    "filename": md.name,
                    "relative_path": rel,
                    "folder": str(Path(rel).parent) if rel != md.name else "",
                    "h1": h1,
                    "mtime": mtime.isoformat(),
                },
                dedup_key=f"wiki_new_file:{rel}",
                decay_at=mtime + timedelta(hours=1),
            ))
    except OSError:
        logger.exception("wiki_new_file.collect: vault walk failed")
        return []

    if not candidates:
        return []

    candidates.sort(key=lambda c: c.payload["mtime"], reverse=True)
    return candidates[:cap]


def mark_consumed(candidate: TriggerCandidate) -> None:
    """Advance the dedup watermark to the consumed candidate's mtime.
    Called by the scheduler AFTER sender.send returns a row id — so
    guard-rejected and send-failed candidates stay eligible for the
    next tick (the watermark only moves forward on successful sends).

    Note: advances by max() so out-of-order consumption (scheduler
    consumes newest first, then older) doesn't roll the watermark
    backwards. The cap-dropped re-surface property lands in Sprint 2
    when the producer switches to per-path consumed-set tracking."""
    try:
        mtime = datetime.fromisoformat(str(candidate.payload.get("mtime") or ""))
        if mtime.tzinfo is None:
            mtime = mtime.replace(tzinfo=UTC)
    except (ValueError, TypeError):
        logger.exception("mark_consumed: failed to parse mtime")
        return
    existing_raw = db.runtime_get(_RUNTIME_STATE_LAST_SEEN)
    existing = None
    if existing_raw:
        try:
            parsed = datetime.fromisoformat(existing_raw)
            existing = parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        except (ValueError, TypeError):
            existing = None
    watermark = max(mtime, existing) if existing else mtime
    db.runtime_set(_RUNTIME_STATE_LAST_SEEN, watermark.isoformat())
