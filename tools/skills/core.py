"""Skill management and execution tools.

Skills live in .agents/skills/<id>/SKILL.md — a YAML-frontmatter text file
that describes what the skill does and how to invoke it.

run_skill: reads skill content, executes via run_internal_control.
skill_list: lists available skill IDs.
skill_read: returns the content of one skill file.
skill_create: stages a new skill in session_scratch for approval.
skill_approve: promotes a staged skill to .agents/skills/<id>/SKILL.md.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Any

from claude_agent_sdk import tool

from tools._annotations import annotations_for
from tools._response import ok as _ok

logger = logging.getLogger(__name__)

_SKILLS_ROOT = Path(__file__).parent.parent.parent / ".claude" / "skills"

# Warn if the old stale tree still has content (don't auto-migrate — Sprint B).
_LEGACY_SKILLS_ROOT = Path(__file__).parent.parent.parent / ".agents" / "skills"
if _LEGACY_SKILLS_ROOT.exists():
    _legacy_entries = [p.name for p in _LEGACY_SKILLS_ROOT.iterdir() if p.is_dir()]
    if _legacy_entries:
        import logging as _logging
        _logging.getLogger(__name__).warning(
            "skills: legacy .agents/skills/ still has content (%s) — "
            "active root is .claude/skills/; migrate in Sprint B",
            ", ".join(sorted(_legacy_entries)),
        )
    del _legacy_entries

_SKILL_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def _validate_skill_id(skill_id: str) -> str | None:
    """Return error string if skill_id is unsafe; None if OK."""
    if not skill_id or not _SKILL_ID_RE.match(skill_id):
        return (
            "error: skill_id must be kebab/snake-case, "
            "1-64 chars, [a-z0-9_-], starting with [a-z0-9]"
        )
    return None


def _skill_path(skill_id: str) -> Path:
    candidate = (_SKILLS_ROOT / skill_id / "SKILL.md").resolve()
    root = _SKILLS_ROOT.resolve()
    if not str(candidate).startswith(str(root) + "/"):
        raise ValueError(f"skill_id escapes skills root: {skill_id!r}")
    return candidate


def _staged_skill_preview(skill_id: str) -> tuple[str | None, str | None]:
    """Return (content_preview, sha256_prefix) for the single staged draft.

    Returns (None, None) when:
      - no staged row exists for skill_id,
      - more than one row exists (ambiguous),
      - or any exception occurs (exception-safe; runs inside can_use_tool path).

    content_preview: the full staged content (gatekeeper summarizer caps at
    2000 chars, so we return the whole string and let it truncate).
    sha256_prefix: first 12 hex chars of sha256(content.encode()).
    """
    try:
        from storage import db as _db
        topic = f"staged_skill:{skill_id}"
        with _db._conn() as conn:
            rows = conn.execute(
                "SELECT payload_json FROM session_scratch WHERE topic = ? ORDER BY created_at DESC",
                (topic,),
            ).fetchall()
        if len(rows) != 1:
            return None, None
        data = json.loads(rows[0][0])
        content = data.get("content") or ""
        digest = hashlib.sha256(content.encode()).hexdigest()[:12]
        return content, digest
    except Exception:
        logger.debug("_staged_skill_preview: failed for %r", skill_id, exc_info=True)
        return None, None


@tool(
    "skill_list",
    "List all available Hikari skills by ID. Returns a list of skill names.",
    {},
    annotations=annotations_for("skill_list"),
)
async def skill_list(args: dict[str, Any]) -> dict[str, Any]:
    if not _SKILLS_ROOT.exists():
        return _ok("[]")
    ids = sorted(
        p.name for p in _SKILLS_ROOT.iterdir()
        if p.is_dir() and (p / "SKILL.md").exists()
    )
    return _ok(json.dumps(ids))


@tool(
    "skill_read",
    "Read the content of a Hikari skill. skill_id is the folder name under .agents/skills/.",
    {"skill_id": str},
    annotations=annotations_for("skill_read"),
)
async def skill_read(args: dict[str, Any]) -> dict[str, Any]:
    skill_id = (args.get("skill_id") or "").strip()
    err = _validate_skill_id(skill_id)
    if err:
        return _ok(err)
    path = _skill_path(skill_id)
    if not path.exists():
        return _ok(f"error: skill {skill_id!r} not found")
    return _ok(path.read_text())


@tool(
    "skill_create",
    "Stage a new skill for approval. Writes to session_scratch; "
    "Hikari will announce it and wait for skill_approve. "
    "skill_id is a short kebab-case name, description is one line, "
    "content is the full skill markdown.",
    {"skill_id": str, "description": str, "content": str},
    annotations=annotations_for("skill_create"),
)
async def skill_create(args: dict[str, Any]) -> dict[str, Any]:
    skill_id = (args.get("skill_id") or "").strip()
    err = _validate_skill_id(skill_id)
    if err:
        return _ok(err)
    description = (args.get("description") or "").strip()
    content = (args.get("content") or "").strip()
    if not content:
        return _ok("error: content is required")
    from storage import db as _db
    session_id = _db.get_session_id() or "pending"
    payload = json.dumps({
        "skill_id": skill_id,
        "description": description,
        "content": content,
    }, ensure_ascii=False)
    topic = f"staged_skill:{skill_id}"
    try:
        with _db._conn() as conn:
            # Replace-on-restage: a skill_id has at most ONE staged draft. Drop
            # any prior unresolved draft for this id before inserting so that
            # re-staging to fix a draft works (and the skill_approve multi-row
            # ambiguity guard can't be tripped by a legitimate re-stage). The
            # owner still sees the exact content+hash at CONFIRM-SEND time.
            conn.execute("DELETE FROM session_scratch WHERE topic = ?", (topic,))
            conn.execute(
                "INSERT INTO session_scratch (session_id, topic, payload_json) VALUES (?, ?, ?)",
                (session_id, topic, payload),
            )
    except Exception:
        logger.exception("skill_create: failed to write to session_scratch")
        return _ok("error: failed to stage skill")
    return _ok(f"skill {skill_id!r} staged — say yes to save it")


@tool(
    "skill_approve",
    "Promote a staged skill from session_scratch to .agents/skills/. "
    "skill_id must match a skill previously staged via skill_create.",
    {"skill_id": str},
    annotations=annotations_for("skill_approve"),
)
async def skill_approve(args: dict[str, Any]) -> dict[str, Any]:
    skill_id = (args.get("skill_id") or "").strip()
    err = _validate_skill_id(skill_id)
    if err:
        return _ok(err)
    from storage import db as _db
    topic = f"staged_skill:{skill_id}"
    try:
        with _db._conn() as conn:
            rows = conn.execute(
                "SELECT id, payload_json FROM session_scratch WHERE topic = ? ORDER BY created_at DESC",
                (topic,),
            ).fetchall()
    except Exception:
        logger.exception("skill_approve: failed to read session_scratch")
        return _ok("error: could not read staged skill")
    n = len(rows)
    if n == 0:
        return _ok(f"error: no staged skill {skill_id!r} found — run skill_create first")
    if n > 1:
        return _ok(
            f"error: {n} conflicting staged drafts for {skill_id!r} exist — "
            "ambiguous; re-stage a single clean draft and approve that"
        )
    row = rows[0]
    row_id, payload_json = row
    try:
        data = json.loads(payload_json)
        content = data["content"]
    except (json.JSONDecodeError, KeyError) as exc:
        return _ok(f"error: corrupt staged skill payload ({exc})")

    # Consent-to-bytes binding (finding-1). The gatekeeper hook stamps the
    # sha256 of the staged content the owner saw at CONFIRM-SEND time into
    # ``_approved_sha256``. Recompute it from the bytes we are about to promote
    # and require an exact match. A concurrent skill_create that swapped the
    # staged payload during the ~300s approval window keeps row-count==1, so
    # the ambiguity guard above can't catch it — this does: a swap promotes
    # NOTHING (refused), not malicious bytes.
    consented = (args.get("_approved_sha256") or "").strip()
    if not consented:
        return _ok(
            "error: skill_approve must be invoked through the gatekeeper — "
            "no consent hash present; nothing promoted"
        )
    actual = hashlib.sha256(content.encode()).hexdigest()[:12]
    if actual != consented:
        return _ok(
            f"error: staged bytes for {skill_id!r} changed since approval "
            f"(consented {consented}, now {actual}) — re-stage and re-approve; "
            "nothing promoted"
        )
    target = _skill_path(skill_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    try:
        with _db._conn() as conn:
            conn.execute("DELETE FROM session_scratch WHERE id = ?", (row_id,))
    except Exception:
        logger.warning("skill_approve: failed to clean up session_scratch row %s", row_id)
        return _ok(
            f"skill {skill_id!r} saved to {target} "
            f"(note: staged scratch row #{row_id} could not be cleaned up — "
            f"will be swept by scratch_cleanup_old)"
        )
    return _ok(f"skill {skill_id!r} saved to {target}")


@tool(
    "run_skill",
    "Execute a Hikari skill by ID. skill_id is the folder name under .agents/skills/. "
    "args is a dict of parameters described in the skill file. "
    "Runs the skill content as a system prompt via an internal control call.",
    {"skill_id": str, "args": dict},
    annotations=annotations_for("run_skill"),
)
async def run_skill(args: dict[str, Any]) -> dict[str, Any]:
    skill_id = (args.get("skill_id") or "").strip()
    err = _validate_skill_id(skill_id)
    if err:
        return _ok(err)
    skill_args = args.get("args") or {}
    path = _skill_path(skill_id)
    if not path.exists():
        return _ok(f"error: skill {skill_id!r} not found")
    skill_content = path.read_text()
    prompt = skill_content
    if skill_args:
        args_text = "\n".join(f"  {k}: {v}" for k, v in skill_args.items())
        prompt = f"{skill_content}\n\n## Invocation arguments\n{args_text}"
    from agents.runtime import run_internal_control
    result = await run_internal_control(prompt, max_turns=8)
    return _ok(result)
