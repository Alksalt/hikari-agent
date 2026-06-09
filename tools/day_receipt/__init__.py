"""Day Receipt feature — manifest.

End-of-day Made / Moved / Learned / Avoided log + free-form note.
Ported from the standalone ``day-receipt`` MCP server so the agent
binary is self-contained — no separate subprocess to launch. The DB
path defaults to ``~/.day-receipt/receipt.db`` (override via
``DAY_RECEIPT_DB``) so the standalone CLI and these in-process tools
share data on the user's main device.

One file per tool. Shared schema + CRUD lives in ``_db.py``, the
46-col ASCII renderer in ``_render.py``, paths / date helpers /
category constants in ``_shared.py``.
"""
from __future__ import annotations

from tools.day_receipt.add import receipt_add
from tools.day_receipt.delete import receipt_delete
from tools.day_receipt.get import receipt_get
from tools.day_receipt.print import receipt_print
from tools.day_receipt.read import receipt_read
from tools.day_receipt.search import receipt_search
from tools.day_receipt.set_note import receipt_set_note
from tools.day_receipt.today import receipt_today
from tools.day_receipt.week import receipt_week

ALL_TOOLS = [
    receipt_add,
    receipt_read,
    receipt_today,
    receipt_get,
    receipt_print,
    receipt_week,
    receipt_search,
    receipt_set_note,
    receipt_delete,
]
