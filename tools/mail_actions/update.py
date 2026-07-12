"""Explicit controls for email actions surfaced in Hikari briefs."""

from __future__ import annotations

from typing import Any

from claude_agent_sdk import tool

from agents import mail_handoff
from tools._annotations import annotations_for
from tools._response import ok as _ok


@tool(
    "mail_action_acknowledge",
    "Acknowledge one email action by the numeric action ID shown in the brief. "
    "Use only when the user explicitly says they saw/acknowledge that exact action.",
    {"action_id": int},
    annotations=annotations_for("mail_action_acknowledge"),
)
async def mail_action_acknowledge(args: dict[str, Any]) -> dict[str, Any]:
    action_id = int(args["action_id"])
    if not mail_handoff.acknowledge(action_id):
        return _ok(f"could not acknowledge email action {action_id}")
    return _ok(f"acknowledged email action {action_id}", data={"action_id": action_id})


@tool(
    "mail_action_resolve",
    "Resolve one email action by the numeric action ID shown in the brief. "
    "Use only when the user explicitly says that exact action is complete.",
    {"action_id": int, "note": str},
    annotations=annotations_for("mail_action_resolve"),
)
async def mail_action_resolve(args: dict[str, Any]) -> dict[str, Any]:
    action_id = int(args["action_id"])
    note = str(args.get("note") or "resolved by user through Hikari")
    if not mail_handoff.resolve(action_id, note):
        return _ok(f"could not resolve email action {action_id}")
    return _ok(f"resolved email action {action_id}", data={"action_id": action_id})


@tool(
    "mail_action_snooze",
    "Snooze one email action until an explicit ISO-8601 timestamp. Use only "
    "when the user names that exact action and asks to postpone it.",
    {"action_id": int, "until_iso": str},
    annotations=annotations_for("mail_action_snooze"),
)
async def mail_action_snooze(args: dict[str, Any]) -> dict[str, Any]:
    action_id = int(args["action_id"])
    until_iso = str(args["until_iso"]).strip()
    if not mail_handoff.snooze(action_id, until_iso):
        return _ok(f"could not snooze email action {action_id}")
    return _ok(
        f"snoozed email action {action_id} until {until_iso}",
        data={"action_id": action_id, "until_iso": until_iso},
    )
