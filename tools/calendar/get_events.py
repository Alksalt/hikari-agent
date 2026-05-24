"""Typed adapter for ``mcp__google_workspace__calendar_get_events``.

Calls the MCP tool directly via ``MANAGER.call``, parses the result into
``CalendarEvent`` Pydantic models, and returns a structured response.
No LLM / prompt plumbing involved.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from claude_agent_sdk import tool
from pydantic import BaseModel

from agents.mcp_manager import MANAGER, McpCallError
from tools._annotations import annotations_for
from tools._response import ok as _ok

logger = logging.getLogger(__name__)


class CalendarEvent(BaseModel):
    id: str
    title: str
    start_iso: str
    end_iso: str
    location: str = ""


def _fail(text: str) -> dict[str, Any]:
    """Return an error envelope matching the ``ok()`` shape."""
    return {
        "content": [{"type": "text", "text": f"error: {text}"}],
        "data": {"_error": text},
    }


def _extract_events(result: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract event list from a MANAGER.call result dict.

    The google_workspace MCP server returns either:
      - structured: {"events": [...]}  (structuredContent path)
      - text: JSON string in {"text": "..."}  (content[0].text path)
    Handle both plus nested shapes seen in the wild.
    """
    # structuredContent path — already a dict
    if "events" in result:
        raw = result["events"]
        if isinstance(raw, list):
            return raw

    # text path — JSON string
    text = result.get("text") or ""
    if text:
        try:
            parsed = json.loads(text)
        except (ValueError, TypeError):
            return []
        if isinstance(parsed, list):
            return parsed
        if isinstance(parsed, dict):
            raw = parsed.get("events") or parsed.get("items") or []
            if isinstance(raw, list):
                return raw

    # items alias used by some versions
    if "items" in result:
        raw = result["items"]
        if isinstance(raw, list):
            return raw

    return []


def _coerce_event(raw: dict[str, Any]) -> dict[str, Any]:
    """Normalise field names from the google_workspace MCP shape."""
    # The MCP server uses 'summary' for the title and nested start/end dicts.
    title = (
        str(raw.get("title") or raw.get("summary") or "").strip()
    )
    start = raw.get("start") or {}
    end = raw.get("end") or {}
    start_iso = str(
        raw.get("start_iso")
        or (start.get("dateTime") if isinstance(start, dict) else start)
        or ""
    ).strip()
    end_iso = str(
        raw.get("end_iso")
        or (end.get("dateTime") if isinstance(end, dict) else end)
        or ""
    ).strip()
    return {
        "id": str(raw.get("id") or "").strip(),
        "title": title,
        "start_iso": start_iso,
        "end_iso": end_iso,
        "location": str(raw.get("location") or "").strip(),
    }


async def _fetch_events(
    time_min: str,
    time_max: str,
    calendar_id: str = "primary",
) -> list[CalendarEvent]:
    """Call google_workspace/calendar_get_events directly and parse the result.

    Raises ``McpCallError`` on tool error so callers can decide how to handle it.
    """
    result = await MANAGER.call(
        "google_workspace",
        "calendar_get_events",
        {"time_min": time_min, "time_max": time_max, "calendar_id": calendar_id},
    )
    raw_events = _extract_events(result)
    out: list[CalendarEvent] = []
    for item in raw_events:
        if not isinstance(item, dict):
            continue
        coerced = _coerce_event(item)
        if not coerced["id"]:
            continue
        out.append(CalendarEvent(**coerced))
    return out


@tool(
    "calendar_get_events",
    "Fetch Google Calendar events between time_min (ISO-8601) and time_max (ISO-8601). "
    "calendar_id defaults to 'primary'. Returns structured event list.",
    {"time_min": str, "time_max": str, "calendar_id": str},
    annotations=annotations_for("calendar_get_events"),
)
async def calendar_get_events(args: dict[str, Any]) -> dict[str, Any]:
    time_min = str(args.get("time_min") or "").strip()
    time_max = str(args.get("time_max") or "").strip()
    if not time_min or not time_max:
        return _fail("time_min and time_max are required")
    calendar_id = str(args.get("calendar_id") or "primary").strip() or "primary"
    try:
        events = await _fetch_events(
            time_min=time_min,
            time_max=time_max,
            calendar_id=calendar_id,
        )
    except McpCallError as exc:
        logger.warning("calendar_get_events: MCP error: %s", exc)
        return _fail(f"calendar fetch failed: {exc.message}")
    return _ok(
        f"fetched {len(events)} events",
        data={"events": [e.model_dump() for e in events]},
    )
