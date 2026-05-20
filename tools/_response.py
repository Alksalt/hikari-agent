"""Shared MCP tool response envelope.

All @tool functions in this package return the same shape:
{"content": [{"type": "text", "text": ...}], "data": optional}.
This module is the single source of truth so the envelope can evolve
in one place if the SDK requirements change.
"""
from __future__ import annotations

from typing import Any


def ok(text: str, data: Any = None) -> dict[str, Any]:
    body: dict[str, Any] = {"content": [{"type": "text", "text": text}]}
    if data is not None:
        body["data"] = data
    return body
