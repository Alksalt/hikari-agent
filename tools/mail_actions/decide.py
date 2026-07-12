"""Chat-facing answer tool for ask-user mail-action questions (Task 6)."""

from __future__ import annotations

from typing import Any

from claude_agent_sdk import tool

from agents import mail_decisions, mail_handoff
from agents.injection_guard import wrap_untrusted
from tools._annotations import annotations_for
from tools._response import ok as _ok


@tool(
    "mail_action_decide",
    "Answer one ask-user email question by its numeric action ID (shown as "
    "[action #id]) and the 1-based option number as listed in that exact "
    "message/brief. Re-fetches the live option list itself before recording "
    "anything — never guess or reuse an option id from memory. Use only "
    "when the user has explicitly chosen one of the numbered options for "
    "that exact action id.",
    {"action_id": int, "option_number": int},
    annotations=annotations_for("mail_action_decide"),
)
async def mail_action_decide(args: dict[str, Any]) -> dict[str, Any]:
    action_id = int(args["action_id"])
    option_number = int(args["option_number"])

    row = mail_decisions.fetch_current_row(action_id)
    if row is None:
        return _ok(
            f"fant ikke et ventende spørsmål med handling-id {action_id} "
            "(kan være allerede besvart/løst, eller CLI-en er utilgjengelig)"
        )
    if str(row.get("kind") or "") != "ask-user":
        return _ok(f"handling {action_id} er ikke et ask-user-spørsmål")

    options = row.get("options") or []
    if option_number < 1 or option_number > len(options):
        return _ok(
            f"ugyldig alternativnummer {option_number} for handling {action_id} "
            f"(gyldig: 1-{len(options)})"
        )
    option_id = str(options[option_number - 1].get("id") or "")

    success, result = mail_handoff.decide(action_id, option_id)
    if not success:
        # `result` here is the owner CLI's raw stderr/stdout text — the CLI
        # boundary can echo attacker-influenced mail content back in a
        # rejection message, so it is untrusted the same as any other
        # external string reaching a composed prompt. Mirrors
        # tools/wiki/morning_brief.py's `safe_err` wrap of a parser exception.
        safe_result = wrap_untrusted("mail_actions_cli", str(result))
        return _ok(f"beslutning avvist for handling {action_id}: {safe_result}")
    return _ok(
        f"registrerte alternativ {option_number} for handling {action_id}",
        data={"action_id": action_id, "option_number": option_number, "option_id": option_id},
    )
