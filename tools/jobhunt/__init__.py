"""Job-hunt copilot (Sprint 2) — read-only radar over the owner's three
job-hunt repos (outreach, job-search, get_hired_prep).

This package currently ships only the data layer (``tools/jobhunt/readers.py``
— typed, read-only SQLite/markdown adapters, no LLM in the data path). The
MCP tool-facing surface lands in a later Sprint 2 task, which will populate
``ALL_TOOLS`` below. Kept as an empty list (rather than omitting the module)
so the tool registry sees a valid, importable package in the meantime.
"""
from __future__ import annotations

ALL_TOOLS: list = []
