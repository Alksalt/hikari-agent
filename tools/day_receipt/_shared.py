"""Shared helpers + constants for the day_receipt tools.

Owns the DB-path resolver (env ``DAY_RECEIPT_DB`` wins, else
``~/.day-receipt/receipt.db``) and the tiny date-parsing helper that
accepts ISO ``YYYY-MM-DD``, ``today``, ``yesterday``, or a ``-N`` offset.
Both kept identical to the standalone ``day_receipt.config`` / ``dates``
modules so the in-process port and the standalone CLI resolve the same
file on the user's main device.

The four bands (``made`` / ``moved`` / ``learned`` / ``avoided``) live
here too — single source of truth shared by ``_db.py`` and ``_render.py``.
"""
from __future__ import annotations

import os
from datetime import date, timedelta
from pathlib import Path
from typing import Literal, get_args

# ---------- categories ----------

Category = Literal["made", "moved", "learned", "avoided"]
CATEGORIES: tuple[Category, ...] = get_args(Category)


def is_category(value: str) -> bool:
    return value in CATEGORIES


# ---------- paths ----------

_ENV_DB_PATH = "DAY_RECEIPT_DB"
_ENV_HOME = "DAY_RECEIPT_HOME"


def home_dir() -> Path:
    """User-level data dir for Day Receipt. Defaults to ~/.day-receipt.

    Override with ``DAY_RECEIPT_HOME`` for tests or alternate stores.
    """
    override = os.environ.get(_ENV_HOME)
    if override:
        return Path(override).expanduser()
    return Path.home() / ".day-receipt"


def db_path() -> Path:
    """SQLite database path. Env ``DAY_RECEIPT_DB`` wins, else
    ``<home>/receipt.db``. Resolved on every call so tests that
    ``monkeypatch.setenv`` after import still hit a fresh path.
    """
    override = os.environ.get(_ENV_DB_PATH)
    if override:
        return Path(override).expanduser()
    return home_dir() / "receipt.db"


# ---------- dates ----------

def parse_date(value: str | None) -> date:
    """Accept ISO ``YYYY-MM-DD``, ``today``, ``yesterday``, ``-N``.

    Matches ``day_receipt.dates.parse_date`` from the standalone repo
    byte-for-byte.
    """
    if value is None or value == "" or value.lower() == "today":
        return date.today()
    v = value.strip().lower()
    if v == "yesterday":
        return date.today() - timedelta(days=1)
    if v.startswith("-") and v[1:].isdigit():
        return date.today() - timedelta(days=int(v[1:]))
    return date.fromisoformat(value)
