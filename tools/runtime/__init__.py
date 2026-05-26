"""runtime feature — in-turn progress signalling.

Exposes the ``progress`` @tool for chain-of-actions feedback:
  - typing-action (sendChatAction) for short/fast sub-steps
  - text message for longer steps or surprises/failures

Rate-limiter: max 4 text emissions per turn, 1.5 s gap, ContextVar-based
state keyed on the current turn_id. Single-step turns are skipped entirely.
"""
from __future__ import annotations

from tools.runtime.progress import progress

ALL_TOOLS = [progress]
