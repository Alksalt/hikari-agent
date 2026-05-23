"""Per-tool telemetry. Wraps an async tool handler with start/end timing,
success bool, error class capture, and untrusted-output size. Writes a row
to the tool_calls table on every invocation."""
from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from functools import wraps
from typing import Any

from storage import db


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
