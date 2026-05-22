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
from datetime import datetime, timedelta, timezone
from typing import Literal

from storage import db
from agents import config as cfg

logger = logging.getLogger(__name__)

Outcome = Literal["approved", "rejected", "expired"]

# Map in-process Outcome values to db-valid status strings.
# The approvals schema CHECK only allows ('pending','approved','rejected','timeout').
_OUTCOME_TO_DB_STATUS: dict[str, str] = {
    "approved": "approved",
    "rejected": "rejected",
    "expired": "timeout",
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
    ) -> Outcome:
        """Block until the user resolves the approval or the deadline expires.

        Idempotent on tool_use_id — a second call with the same id returns
        immediately by joining the existing in-flight event.
        """
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
                await self._send_text(
                    chat_id,
                    self._format_prompt(tool_name, summary),
                )
            except Exception:
                logger.exception("gatekeeper: send_text failed for tool_use_id=%s", tool_use_id)

        # Await resolution or deadline.
        now = datetime.now(timezone.utc)
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=timezone.utc)
        timeout_s = max(1.0, (deadline - now).total_seconds())
        try:
            await asyncio.wait_for(pending_obj.event.wait(), timeout=timeout_s)
        except asyncio.TimeoutError:
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
            datetime.now(timezone.utc) - timedelta(hours=max_age_h)
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

    def _write_audit_row(self, p: "_Pending") -> None:
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
            from tools.approvals import _redact  # noqa: PLC0415
            import json as _json  # noqa: PLC0415
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
