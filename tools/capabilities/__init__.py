"""Capabilities feature — the user-facing "what can you do" answer.

One tool, ``capabilities_overview``: reads the tool catalog (single source
of truth: config/tools.yaml) + the curated command menu and returns a
structured overview the model renders in voice. No LLM, no network.
"""
from __future__ import annotations

from tools.capabilities.overview import capabilities_overview

ALL_TOOLS = [capabilities_overview]
