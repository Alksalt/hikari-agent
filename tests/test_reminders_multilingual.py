"""Phase 11 — multilingual reminders regression net.

Asserts that the reminder_create tool description tells the model to use
the ``# now`` block and shows UK / RU examples — that's what lets the
model produce ISO timestamps from relative phrasing.
The model decides whether to invoke reminder_create via the tool description;
no regex substantive-opener list is involved.
"""
from __future__ import annotations

from pathlib import Path

from tools import reminders


def test_reminder_create_description_mentions_now_and_multilingual():
    """The model-facing description must (a) point at the `# now` block
    and (b) carry at least one UK and one RU example."""
    desc = getattr(reminders.reminder_create, "description", None)
    if not desc:
        # Fall back to source if the SDK hides .description.
        src = Path(__file__).resolve().parent.parent / "tools" / "reminders.py"
        desc = src.read_text(encoding="utf-8")
    assert "# now" in desc, "description must point at the `# now` block"
    assert "нагадай" in desc, "description must contain a UK example"
    assert "напомни" in desc, "description must contain an RU example"
    assert "ISO" in desc or "iso" in desc.lower()
