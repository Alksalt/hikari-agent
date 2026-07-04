"""Per-tool telemetry. Wraps an async tool handler with start/end timing,
success bool, error class capture, and untrusted-output size. Writes a row
to the tool_calls table on every invocation."""
from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from functools import wraps
from typing import Any

from storage import db

logger = logging.getLogger(__name__)

# The SDK silently replaces any MCP tool result past its 25k-token output cap
# with an error the model sees INSTEAD of the data, while this wrapper still
# records success=1 (the handler itself returned fine) — that's how the
# 2026-07-04 jobhunt_radar 174KB result became invisible. Warn well before
# the cap (25k tokens ≈ 100KB of text) so oversized tools show up in the log
# next to their success row.
_OUTPUT_WARN_CHARS = 80_000


def instrumented(tool_id: str):
    """Decorator factory. Apply to async MCP tool handlers."""
    def decorator(fn: Callable[[dict[str, Any]], Awaitable[Any]]):
        @wraps(fn)
        async def wrapper(args: dict[str, Any]) -> Any:
            started = time.monotonic()
            success = True
            error_class: str | None = None
            output_size = 0
            try:
                result = await fn(args)
                try:
                    content = result.get("content") if isinstance(result, dict) else None
                    if isinstance(content, list):
                        for blk in content:
                            text = blk.get("text") if isinstance(blk, dict) else None
                            if isinstance(text, str):
                                output_size += len(text)
                except Exception:
                    pass
                if output_size > _OUTPUT_WARN_CHARS:
                    logger.warning(
                        "tool %s returned %d chars — likely past the SDK's "
                        "25k-token MCP output cap; the model may see a "
                        "size-limit error instead of this result",
                        tool_id, output_size,
                    )
                return result
            except Exception as exc:
                success = False
                error_class = type(exc).__name__
                raise
            finally:
                duration_ms = int((time.monotonic() - started) * 1000)
                try:
                    db.tool_calls_insert(
                        tool_id=tool_id,
                        duration_ms=duration_ms,
                        success=success,
                        error_class=error_class,
                        output_size=output_size,
                    )
                except Exception:
                    pass  # never fail a tool call because telemetry broke
        return wrapper
    return decorator
