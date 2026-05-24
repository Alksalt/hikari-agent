"""Centralized AUTH_PRECHECK mode resolver.

Single source of truth for the priority chain:
  AUTH_PRECHECK_OVERRIDE env  (escape hatch, highest priority)
  > AUTH_PRECHECK env
  > auth.precheck in engagement.yaml config
  > "shadow" default (lowest priority)

Both agents/hooks.py (_precheck_scopes) and agents/cockpit.py
(_read_auth_precheck) delegate here so they can never drift apart.
"""

from __future__ import annotations

import logging
import os
from typing import Literal

logger = logging.getLogger(__name__)

_VALID_MODES: frozenset[str] = frozenset({"off", "shadow", "enforce"})

AuthPrecheckMode = Literal["off", "shadow", "enforce"]


def resolve_mode() -> AuthPrecheckMode:
    """Return the effective AUTH_PRECHECK mode.

    Priority (highest to lowest):
      1. AUTH_PRECHECK_OVERRIDE env var — runtime escape hatch
      2. AUTH_PRECHECK env var — set by /settings set AUTH_PRECHECK <mode>
      3. auth.precheck in engagement.yaml config
      4. "shadow" — safe default
    """
    override_env = os.environ.get("AUTH_PRECHECK_OVERRIDE", "").strip().lower()
    if override_env:
        if override_env not in _VALID_MODES:
            logger.warning(
                "auth: unknown AUTH_PRECHECK_OVERRIDE=%r — falling back to shadow",
                override_env,
            )
            return "shadow"
        return override_env  # type: ignore[return-value]

    direct_env = os.environ.get("AUTH_PRECHECK", "").strip().lower()
    if direct_env:
        if direct_env not in _VALID_MODES:
            logger.warning(
                "auth: unknown AUTH_PRECHECK=%r — falling back to shadow",
                direct_env,
            )
            return "shadow"
        return direct_env  # type: ignore[return-value]

    try:
        from agents import config as _cfg
        cfg_val = str(_cfg.get("auth.precheck") or "").strip().lower()
        if cfg_val in _VALID_MODES:
            return cfg_val  # type: ignore[return-value]
    except Exception:
        pass

    return "shadow"
