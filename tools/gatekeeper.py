"""Gatekeeper — single durable state machine for can_use_tool approvals.

Phase E (Sprint 2). Unlike the legacy PreToolUse defer path, this gate holds
the SDK's tool call paused INSIDE can_use_tool.request() (an asyncio await)
until the user resolves it, then returns Allow or Deny to the SDK.

The Gatekeeper is a module-level singleton (``GATEKEEPER``). The bridge calls
``GATEKEEPER.set_send_text(fn)`` at boot and ``GATEKEEPER.restart_recovery``
after sdk_pool starts.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Literal

from agents import config as cfg
from storage import db

logger = logging.getLogger(__name__)

Outcome = Literal["approved", "rejected", "expired", "admin_cancel"]

# Map in-process Outcome values to db-valid status strings.
# The approvals schema CHECK only allows ('pending','approved','rejected','timeout').
_OUTCOME_TO_DB_STATUS: dict[str, str] = {
    "approved": "approved",
    "rejected": "rejected",
    "expired": "timeout",
    "admin_cancel": "rejected",
}


@dataclass
class _Pending:
    aid: int
    chat_id: int
    tool_use_id: str
    tool_name: str
    event: asyncio.Event = field(default_factory=asyncio.Event)
    outcome: Outcome | None = None


class Gatekeeper:
    """Durable async state machine for SDK can_use_tool approvals.

    Lifecycle of a gate request
    ---------------------------
    1. ``can_use_tool`` calls ``request()``.
    2. ``request()`` writes a row to ``approvals`` (gate_kind='gatekeeper'),
       sends a Telegram prompt, then awaits ``asyncio.Event.wait()`` with a
       deadline timeout.
    3. When the user sends CONFIRM-SEND (or a reject phrase), the bridge calls
       ``tools.approvals.resolve_pending_approval`` which calls
       ``GATEKEEPER.resolve(tool_use_id, outcome)``.
    4. ``resolve()`` sets ``_Pending.outcome`` and fires the event, unblocking
       the awaiting ``request()`` call, which returns the outcome to the SDK.
    5. On restart: ``restart_recovery()`` expires stale rows and nudges the user.
    """

    def __init__(self) -> None:
        self._by_use_id: dict[str, _Pending] = {}
        self._lock = asyncio.Lock()
        self._send_text = None  # set via set_send_text() at bridge boot

    def set_send_text(self, send_text_fn) -> None:
        """Bridge calls this at startup. send_text_fn(chat_id, text) -> awaitable."""
        self._send_text = send_text_fn

    async def request(
        self,
        *,
        tool_use_id: str,
        tool_name: str,
        chat_id: int,
        args: dict,
        summary: str,
        deadline: datetime,
        gate_kind: str = "gatekeeper",
    ) -> Outcome:
        """Block until the user resolves the approval or the deadline expires.

        Idempotent on tool_use_id — a second call with the same id returns
        immediately by joining the existing in-flight event.
        """
        # per-session per-tool allowlist (Phase F Feature 1)
        from tools.approvals import _check_always_approve
        if _check_always_approve(chat_id, tool_name):
            logger.info(
                "gatekeeper: always_approve hit for %s (chat_id=%s)",
                tool_name, chat_id,
            )
            return "approved"

        is_new = False
        async with self._lock:
            existing = self._by_use_id.get(tool_use_id)
            if existing:
                pending_obj = existing
            else:
                aid = db.approval_create_gatekeeper(
                    chat_id=chat_id,
                    tool_name=tool_name,
                    tool_use_id=tool_use_id,
                    args_json=json.dumps(args, ensure_ascii=False, default=str),
                    summary=summary,
                    deadline_iso=deadline.isoformat(),
                    gate_kind=gate_kind,
                )
                pending_obj = _Pending(
                    aid=aid,
                    chat_id=chat_id,
                    tool_use_id=tool_use_id,
                    tool_name=tool_name,
                )
                self._by_use_id[tool_use_id] = pending_obj
                is_new = True

        # Send Telegram prompt outside the lock so we don't block other requesters.
        # Only the first caller sends — subsequent idempotent calls skip it.
        if is_new and self._send_text:
            try:
                reply_markup = None
                try:
                    from agents.telegram_bridge import _kb_approval  # noqa: PLC0415
                    reply_markup = _kb_approval(pending_obj.aid)
                except Exception:
                    pass
                try:
                    if reply_markup is not None:
                        await self._send_text(
                            chat_id,
                            self._format_prompt(tool_name, summary),
                            reply_markup=reply_markup,
                        )
                    else:
                        await self._send_text(
                            chat_id,
                            self._format_prompt(tool_name, summary),
                        )
                except TypeError:
                    # Fallback: send_text doesn't accept reply_markup (e.g. tests).
                    await self._send_text(
                        chat_id,
                        self._format_prompt(tool_name, summary),
                    )
            except Exception:
                logger.exception("gatekeeper: send_text failed for tool_use_id=%s", tool_use_id)

        # Await resolution or deadline.
        now = datetime.now(UTC)
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=UTC)
        timeout_s = max(1.0, (deadline - now).total_seconds())
        try:
            await asyncio.wait_for(pending_obj.event.wait(), timeout=timeout_s)
        except TimeoutError:
            logger.info("gatekeeper: timeout for tool_use_id=%s", tool_use_id)
            # Only expire if not already resolved by a concurrent approve/reject.
            async with self._lock:
                _p = self._by_use_id.get(tool_use_id)
                already_resolved = _p is None or _p.outcome is not None
            if not already_resolved:
                await self._resolve_internal(tool_use_id, "expired")

        # Clean up the in-memory slot (only the last waiter needs to do it;
        # use discard rather than pop so concurrent callers don't race).
        async with self._lock:
            self._by_use_id.pop(tool_use_id, None)

        # pending_obj is captured before the await, so its outcome is safe to read
        # even after _by_use_id was cleaned up by a concurrent caller.
        return pending_obj.outcome if pending_obj.outcome else "expired"

    async def resolve(self, tool_use_id: str, outcome: Outcome) -> bool:
        """Resolve an in-flight or DB-only pending gatekeeper approval.

        Returns True if a row was found and resolved, False otherwise.
        """
        return await self._resolve_internal(tool_use_id, outcome)

    async def _resolve_internal(self, tool_use_id: str, outcome: Outcome) -> bool:
        db_status = _OUTCOME_TO_DB_STATUS.get(outcome, "rejected")
        async with self._lock:
            p = self._by_use_id.get(tool_use_id)
            if p:
                if p.outcome is not None:
                    return True  # already resolved — idempotent, don't overwrite
                p.outcome = outcome
                db.approval_resolve(p.aid, db_status)
                if outcome == "approved":
                    db.approval_mark_executed(p.aid, result_summary="(handed back to SDK)")
                    self._write_audit_row(p)
                p.event.set()
                return True

        # Not in-memory — try the DB (e.g. called from restart_recovery).
        row = db.approval_pending_by_use_id(tool_use_id)
        if not row:
            return False
        db.approval_resolve(int(row["id"]), db_status)
        return True

    async def restart_recovery(self, bot=None) -> int:
        """On boot: expire stale rows then nudge survivors and expire them too.

        The matching SDK tool_use_id is gone after a process restart, so there
        is no way to resume the paused can_use_tool await. We notify the user
        and mark all pending gatekeeper rows as timed out.

        Returns the number of rows handled.
        """
        max_age_h = float(cfg.get("gatekeeper.recovery_max_age_h", 1.0))
        cutoff = (
            datetime.now(UTC) - timedelta(hours=max_age_h)
        ).isoformat()
        expired_count = db.approval_expire_stale(cutoff)
        logger.info(
            "gatekeeper.restart_recovery: expired %d stale rows (older than %gh)",
            expired_count, max_age_h,
        )

        # Nudge any rows that survived the cutoff (very recent ones).
        survivors = db.approvals_list_pending_gatekeeper()
        for row in survivors:
            chat_id = int(row["chat_id"])
            tool_name = str(row.get("tool_name") or "")
            if self._send_text:
                try:
                    await self._send_text(
                        chat_id,
                        f"...the {tool_name} approval got dropped on restart. "
                        "ask again if it still matters.",
                    )
                except Exception:
                    logger.exception(
                        "gatekeeper: recovery send failed for approval %s",
                        row.get("id"),
                    )
            db.approval_resolve(int(row["id"]), "timeout")
            logger.info(
                "gatekeeper.restart_recovery: nudged + expired approval %s (tool=%s)",
                row.get("id"), tool_name,
            )

        return expired_count + len(survivors)

    def _write_audit_row(self, p: _Pending) -> None:
        """Write a hash-chained audit row for an approved gatekeeper call.

        Called inside _resolve_internal (under lock) after approval_mark_executed.
        Non-fatal: any exception is logged and swallowed so the in-memory state
        (event.set) still unblocks the awaiting request() caller.
        """
        try:
            with db._conn() as _c:
                row = _c.execute(
                    "SELECT args_json FROM approvals WHERE id=?", (p.aid,)
                ).fetchone()
            import json as _json  # noqa: PLC0415

            from tools.approvals import _redact  # noqa: PLC0415
            args_redacted = _redact(row["args_json"] or "")[:500] if row else ""
            try:
                from agents.injection_guard import flag_args_with_untrusted_content  # noqa: PLC0415
                args_dict = _json.loads(row["args_json"] or "{}") if row else {}
                taint_flag, taint_reason = flag_args_with_untrusted_content(args_dict)
                summary_str = "gatekeeper approved"
                if taint_flag:
                    summary_str = f"[UNTRUSTED:{taint_reason}] {summary_str}"
            except Exception:
                summary_str = "gatekeeper approved"
            db.audit_append(
                tool=p.tool_name,
                args_json_redacted=args_redacted,
                result_summary=summary_str,
                approved_by="owner",
            )
        except Exception:
            logger.exception("gatekeeper: audit_append failed for aid=%s", p.aid)

    @staticmethod
    def _format_prompt(tool_name: str, summary: str) -> str:
        return (
            f"⏸️ {summary}\n\n"
            "type CONFIRM-SEND exactly to send. timeout will drop it."
        )


GATEKEEPER = Gatekeeper()


def summarize(tool_name: str, tool_input: dict) -> str:
    """Return a one-line human-readable description of a gated tool call.

    Used by can_use_tool handlers to build the approval prompt shown to the
    owner. Handlers are ordered most-specific first; the fallback at the end
    catches any unregistered tool and fails loud so coverage gaps surface
    immediately.
    """
    if tool_name == "mcp__google_workspace__gmail_bulk_delete_messages":
        query = tool_input.get("query", "?")
        return f"bulk-delete gmail messages matching {query!r}"

    if tool_name == "mcp__google_workspace__gmail_send_email":
        to = tool_input.get("to", "?")
        subject = tool_input.get("subject") or ""
        body = tool_input.get("body") or tool_input.get("html", "") or ""
        cc = tool_input.get("cc") or ""
        bcc = tool_input.get("bcc") or ""
        parts = [f"send email to {to}\nsubject: {subject}\nbody: {body}"]
        if cc:
            parts.append(f"cc: {cc}")
        if bcc:
            parts.append(f"bcc: {bcc}")
        html = tool_input.get("html") or ""
        if html and html != body:
            parts.append(f"html: {html}")
        return "\n".join(parts)

    if tool_name == "mcp__google_workspace__gmail_reply_to_email":
        msg_id = tool_input.get("message_id", "?")
        body = tool_input.get("body") or ""
        html = tool_input.get("html") or ""
        cc = tool_input.get("cc") or ""
        bcc = tool_input.get("bcc") or ""
        parts = [f"reply to gmail thread {msg_id}\nbody: {body}"]
        if html and html != body:
            parts.append(f"html: {html}")
        if cc:
            parts.append(f"cc: {cc}")
        if bcc:
            parts.append(f"bcc: {bcc}")
        return "\n".join(parts)

    if tool_name == "mcp__google_workspace__delete_calendar_event":
        event_id = tool_input.get("event_id", "?")
        cal_id = tool_input.get("calendar_id", "primary")
        return f"delete calendar event {event_id} from {cal_id}"

    if tool_name == "mcp__google_workspace__drive_delete_file":
        file_id = tool_input.get("file_id", "?")
        return f"delete drive file {file_id}"

    if tool_name == "mcp__google_workspace__create_calendar_event":
        title = tool_input.get("summary") or tool_input.get("title") or "?"
        start = tool_input.get("start_time") or tool_input.get("start", "?")
        end = tool_input.get("end_time") or tool_input.get("end") or ""
        location = tool_input.get("location") or ""
        attendees = tool_input.get("attendees") or []
        parts = [f"create calendar event {title!r} at {start}"]
        if end:
            parts.append(f"end: {end}")
        if location:
            parts.append(f"location: {location}")
        if attendees:
            parts.append(f"attendees: {attendees!r}")
        return "\n".join(parts)

    if tool_name == "mcp__google_workspace__drive_delete_folder":
        folder_id = tool_input.get("folder_id", "?")
        return f"delete drive folder {folder_id}"

    if tool_name == "mcp__google_workspace__drive_upload_file":
        name = tool_input.get("file_name") or tool_input.get("name") or "?"
        source = tool_input.get("source_path") or tool_input.get("local_path") or tool_input.get("path") or ""
        parts = [f"upload to drive: {name!r}"]
        if source:
            parts.append(f"source: {source}")
        return "\n".join(parts)

    if tool_name == "mcp__notion__API-patch-page":
        page_id = tool_input.get("page_id", "?")
        content = tool_input.get("properties") or tool_input.get("content") or ""
        return f"patch notion page {page_id}\ncontent: {content!r}"

    if tool_name == "mcp__notion__API-post-page":
        parent = (tool_input.get("parent") or {}).get("page_id") or "?"
        content = tool_input.get("children") or tool_input.get("content") or ""
        return f"create notion page under {parent}\ncontent: {content!r}"

    if tool_name == "mcp__notion__API-patch-block-children":
        block_id = tool_input.get("block_id", "?")
        content = tool_input.get("children") or tool_input.get("content") or ""
        return f"add children to notion block {block_id}\ncontent: {content!r}"

    if tool_name == "mcp__notion__API-update-a-block":
        block_id = tool_input.get("block_id", "?")
        content = tool_input.get("block") or tool_input.get("content") or ""
        return f"update notion block {block_id}\ncontent: {content!r}"

    if tool_name == "mcp__notion__API-delete-a-block":
        block_id = tool_input.get("block_id", "?")
        return f"delete notion block {block_id}"

    if tool_name == "mcp__github__create_issue":
        repo = f"{tool_input.get('owner', '?')}/{tool_input.get('repo', '?')}"
        title = tool_input.get("title") or "?"
        body = tool_input.get("body") or ""
        parts = [f"create issue in {repo}: {title!r}"]
        if body:
            parts.append(f"body: {body}")
        return "\n".join(parts)

    if tool_name == "mcp__github__create_pull_request":
        repo = f"{tool_input.get('owner', '?')}/{tool_input.get('repo', '?')}"
        title = tool_input.get("title") or "?"
        body = tool_input.get("body") or ""
        base = tool_input.get("base") or ""
        head = tool_input.get("head") or ""
        parts = [f"open PR in {repo}: {title!r}"]
        if base:
            parts.append(f"base: {base}")
        if head:
            parts.append(f"head: {head}")
        if body:
            parts.append(f"body: {body}")
        return "\n".join(parts)

    if tool_name == "mcp__github__merge_pull_request":
        repo = f"{tool_input.get('owner', '?')}/{tool_input.get('repo', '?')}"
        pull = tool_input.get("pullNumber") or tool_input.get("pull_number", "?")
        return f"merge PR #{pull} in {repo}"

    if tool_name == "mcp__github__delete_file":
        repo = f"{tool_input.get('owner', '?')}/{tool_input.get('repo', '?')}"
        path = tool_input.get("path", "?")
        return f"delete {path} from {repo}"

    if tool_name == "mcp__github__delete_repository":
        repo = f"{tool_input.get('owner', '?')}/{tool_input.get('repo', '?')}"
        return f"DELETE REPO {repo}"

    if tool_name == "mcp__hikari_dispatch__dispatch_claude_session":
        task = tool_input.get("task") or tool_input.get("prompt") or ""
        repo_path = tool_input.get("repo_path") or ""
        allowed_tools = tool_input.get("allowed_tools") or []
        write_mode = bool(tool_input.get("write_mode") or False)
        header = (
            "⚠ WRITE-MODE dispatch — worker can run Bash/Edit/Write autonomously"
            if write_mode
            else "read-only dispatch (write tools filtered out)"
        )
        parts = [f"dispatch claude session [{header}]:", f"  write_mode: {write_mode}", f"  task: {task!r}"]
        if repo_path:
            parts.append(f"  repo_path: {repo_path}")
        if allowed_tools:
            parts.append(f"  allowed_tools: {allowed_tools!r}")
        return "\n".join(parts)

    if tool_name == "mcp__hikari_utility__python_run":
        code_preview = (tool_input.get("code") or "")[:120]
        return f"run python: {code_preview!r}"

    # Server-prefix fallbacks: avoid raising NotImplementedError for the
    # large surface of Google Workspace / GitHub / Notion write tools that
    # don't have a dedicated case above. The summary is generic but better
    # than the catch-all "tool {name} (no summary available)" the caller
    # would otherwise render.
    if tool_name.startswith("mcp__google_workspace__"):
        op = tool_name.removeprefix("mcp__google_workspace__")
        if "gmail" in op:
            subj = tool_input.get("subject") or tool_input.get("to") or ""
            return f"google_workspace gmail op: {op}" + (f" — {subj!r}" if subj else "")
        if "calendar" in op:
            return f"google_workspace calendar op: {op}"
        if "drive" in op:
            name = tool_input.get("file_name") or tool_input.get("file_id") or ""
            return f"google_workspace drive op: {op}" + (f" — {name!r}" if name else "")
        if "sheets" in op or "docs" in op or "slides" in op or "presentation" in op:
            return f"google_workspace doc op: {op}"
        return f"google_workspace op: {op}"

    if tool_name.startswith("mcp__github__"):
        op = tool_name.removeprefix("mcp__github__")
        repo = f"{tool_input.get('owner', '?')}/{tool_input.get('repo', '?')}"
        return f"github op: {op} on {repo}"

    if tool_name.startswith("mcp__notion__") or tool_name.startswith("mcp__claude_ai_Notion__"):
        op = tool_name.split("__")[-1]
        title = tool_input.get("title") or tool_input.get("page_id") or ""
        return f"notion op: {op}" + (f" — {title!r}" if title else "")

    raise NotImplementedError(
        f"summarize: no handler for gated tool {tool_name!r} — "
        "add a case to tools/gatekeeper.py:summarize()"
    )
