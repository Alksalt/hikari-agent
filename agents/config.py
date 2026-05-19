"""Config loader. Single source of truth for all Hikari tunables.

All thresholds, regex patterns, banned phrases, cadence caps, etc. live in
``config/engagement.yaml`` — never hardcoded in code. Modules access them via
``config.get("section.key")`` or ``config.section("name")``.

Env override pattern: where a value is also exposed via an env var (e.g.
``HIKARI_DAILY_CAP_USD``), the env var name itself is stored in the yaml
(``daily_cap_usd_env``) and read via ``env_or(...)`` at the call site. This keeps
the env-vs-yaml split explicit and self-documenting.

Reload from disk via ``reload()`` — useful in tests or live tuning. The bot
process does not auto-watch the file.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).parent.parent


def _config_path() -> Path:
    """Resolved lazily so HIKARI_CONFIG_PATH set after import still wins."""
    return Path(
        os.environ.get("HIKARI_CONFIG_PATH")
        or REPO_ROOT / "config" / "engagement.yaml"
    )


@lru_cache(maxsize=1)
def _load() -> dict[str, Any]:
    path = _config_path()
    if not path.exists():
        raise FileNotFoundError(f"Hikari config not found at {path}")
    with path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise TypeError(f"Hikari config at {path} did not parse as a mapping")
    return data


def reload() -> None:
    """Clear the cached config; next access re-reads from disk."""
    _load.cache_clear()


def get(path: str, default: Any = None) -> Any:
    """Dot-path access. Returns ``default`` if any segment is missing.

    Example: ``get("typing.base_sec", 1.5)``.
    """
    node: Any = _load()
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def section(name: str) -> dict[str, Any]:
    """Return a whole top-level section as a dict. Raises if it isn't a mapping."""
    node = _load().get(name)
    if node is None:
        return {}
    if not isinstance(node, dict):
        raise TypeError(f"config section {name!r} is not a dict")
    return node


def env_or(env_key: str, fallback: Any) -> str:
    """Env override with fallback. Always returns a string."""
    val = os.environ.get(env_key)
    return val if val is not None else str(fallback)


def env_bool(env_key: str, fallback: bool = False) -> bool:
    """Env-driven bool with fallback. Truthy: 1/true/yes/on (case-insensitive)."""
    raw = os.environ.get(env_key)
    if raw is None:
        return fallback
    return raw.strip().lower() in ("1", "true", "yes", "on")


def env_float(env_key: str, fallback: float) -> float:
    raw = os.environ.get(env_key)
    if raw is None:
        return fallback
    try:
        return float(raw)
    except ValueError:
        return fallback
