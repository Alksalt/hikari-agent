"""Shared helpers + constants for the codex reports tools.

The codex feature exposes a small read-only surface over the repo's
``codex/`` directory — LLM-generated markdown audits that Hikari can
surface when the user asks "what did codex find". Reports are treated
as untrusted data (wrapped via ``injection_guard.wrap_untrusted`` at
read time).

Constants (``_MAX_LIMIT``, ``_MAX_READ_BYTES``) and the private path
helpers (``_reports_dir``, ``_safe_name``) are re-exported from
``tools/codex/__init__.py`` so tests that pull them via the package
namespace (e.g. ``codex_tools._MAX_READ_BYTES``) keep working.
"""
from __future__ import annotations

from pathlib import Path

from agents import config as cfg

# Hard limits — prevent runaway scans or oversized reads.
_MAX_LIMIT = 50
_MAX_READ_BYTES = 200_000


def _reports_dir() -> Path:
    """Resolve the configured reports directory. Relative paths resolve
    against the current working directory."""
    raw = str(cfg.get("codex.reports_dir", "./codex"))
    p = Path(raw).expanduser()
    if not p.is_absolute():
        p = (Path.cwd() / p).resolve()
    return p


def _safe_name(name: str) -> str:
    """Strip directory components — codex reads are flat, no nested paths."""
    return Path(name).name
