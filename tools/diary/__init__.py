"""Diary feature — read Hikari's recent diary entries.

One tool: ``diary_read``. Entries are written by the evening diary
composer (``agents/evening_diary.py``) once per day.
"""
from __future__ import annotations

from tools.diary.read import diary_read

ALL_TOOLS = [diary_read]
