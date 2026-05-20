"""Cross-file contract: reminder_create description references the `# now`
block that agents/hooks.py:_format_now renders. If either side drifts
without the other, reminders silently start failing on relative times.
"""
from __future__ import annotations

from agents import hooks
from tools import reminders


def test_format_now_starts_with_now_header():
    block = hooks._format_now()
    assert block.startswith("# now\n"), f"unexpected prefix: {block[:32]!r}"


def test_reminder_create_description_references_now_block():
    desc = getattr(reminders.reminder_create, "description", None)
    assert desc, "reminder_create.description should be a non-empty string"
    assert "# now" in desc, "reminder_create description must reference the `# now` block"
