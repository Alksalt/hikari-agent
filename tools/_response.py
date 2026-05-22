"""Shared MCP tool response envelope.

All @tool functions in this package return the same shape:
{"content": [{"type": "text", "text": ...}], "data": optional}.
This module is the single source of truth so the envelope can evolve
in one place if the SDK requirements change.
"""
from __future__ import annotations

import json
from typing import Any


def ok(
    text: str,
    data: Any = None,
    *,
    sources: list[dict] | None = None,
    presentation_hint: str | None = None,
    notes: list[str] | None = None,
) -> dict[str, Any]:
    parts = [text]
    if presentation_hint:
        parts.append(f"\n### presentation_hint\n{presentation_hint}")
    if sources:
        parts.append("\n### sources")
        for s in sources:
            parts.append(f"- {s['name']} ({s.get('confidence', 1.0):.2f}) {s.get('url') or ''}")
    if notes:
        parts.append("\n### notes")
        for n in notes:
            parts.append(f"- {n}")
    if data is not None:
        parts.append("\n### data\n```json")
        parts.append(json.dumps(data, default=str, indent=2, ensure_ascii=False))
        parts.append("```")

    body: dict[str, Any] = {"content": [{"type": "text", "text": "\n".join(parts)}]}
    if data is not None:
        body["data"] = data
    return body
