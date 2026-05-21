"""Per-turn ContextVar state shared between the SDK runtime and post-send
filters. Lives in its own module so ``importlib.reload(agents.runtime)``
(used by allowlist tests) does NOT replace the ContextVar object — that
would silently break the chat-path fabrication backstop because the runtime
would write into a different ContextVar than ``filter_outgoing`` reads from.
"""
from __future__ import annotations

from contextvars import ContextVar

# Set per ``_invoke_sdk`` call to the set of qualified tool names the model
# invoked during that turn. The chat path's post_filter reads this to detect
# the fabrication failure mode where the model claims to have checked
# gmail/calendar/drive but didn't actually call the corresponding tool.
LAST_TURN_TOOL_NAMES: ContextVar[set[str]] = ContextVar(
    "hikari_last_turn_tool_names", default=set(),
)
