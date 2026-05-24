"""Reminders feature — manifest.

One file per tool (``create.py`` / ``list.py`` / ``cancel.py`` /
``snooze.py``), with the ISO parser + repeat-keyword set shared
via ``_shared.py``.

Re-exports the four tool callables at package level so existing
``from tools import reminders`` / ``reminders.reminder_create.handler``
call sites keep working.
"""
from __future__ import annotations

from tools.reminders._shared import _VALID_REPEAT, _parse_iso  # noqa: F401 — re-exported helpers
from tools.reminders.cancel import reminder_cancel
from tools.reminders.create import reminder_create
from tools.reminders.list import reminder_list
from tools.reminders.snooze import reminder_snooze

# sync_apple_reminder and sync_gcal_reminder are scheduler-internal callers only.
# They are NOT LLM-reachable @tools and are NOT included in ALL_TOOLS.
# Import _sync_apple_reminder / _sync_gcal_reminder directly from their modules
# when needed by the scheduler (agents/proactive.py).

ALL_TOOLS = [
    reminder_create,
    reminder_list,
    reminder_cancel,
    reminder_snooze,
]
