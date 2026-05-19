"""Structured peer (user) representation — Honcho-inspired.

Replaces the flat ``core_blocks.user_profile`` string with a structured JSON
shape that captures how Hikari understands her one person across multiple
dimensions. The daily reflection writes here via dialectic merge (update
existing values by reasoning, don't blindly overwrite).

Fields:
  - ``communication_style``: how the user texts — terse/verbose, formal/casual,
    playful, contrarian, tends to use voice notes, etc.
  - ``values``: what they push back on, what they protect, what they care about.
  - ``domain_expertise``: where they're competent (and Hikari should not condescend).
  - ``current_concerns``: rolling list of what's on their mind lately
    (week-scale, not snapshot).
  - ``blindspots``: things Hikari has noticed they consistently miss/avoid.
    Used carefully — surface obliquely, never as diagnosis.
  - ``summary``: 1-2 sentence prose distillation. Used in always-on injection.

The ``mood_today`` core_block stays separate (rotates daily; three fast-path
readers depend on the existing key/value table for low-latency access).
"""

from __future__ import annotations

import logging
from typing import Any, TypedDict

logger = logging.getLogger(__name__)


class PeerRepresentation(TypedDict, total=False):
    communication_style: str
    values: list[str]
    domain_expertise: list[str]
    current_concerns: list[str]
    blindspots: list[str]
    summary: str


_EMPTY_MODEL: PeerRepresentation = {
    "communication_style": "",
    "values": [],
    "domain_expertise": [],
    "current_concerns": [],
    "blindspots": [],
    "summary": "",
}


def empty() -> PeerRepresentation:
    """Return an empty (but well-shaped) representation. Used at first-run."""
    return {
        "communication_style": "",
        "values": [],
        "domain_expertise": [],
        "current_concerns": [],
        "blindspots": [],
        "summary": "",
    }


def _normalize(model: Any) -> PeerRepresentation:
    """Coerce arbitrary dict-like input to the canonical shape, dropping
    unknown keys and fixing types. Defensive — never raises."""
    if not isinstance(model, dict):
        return empty()
    out = empty()
    cs = model.get("communication_style")
    if isinstance(cs, str):
        out["communication_style"] = cs.strip()
    for list_key in ("values", "domain_expertise", "current_concerns", "blindspots"):
        raw = model.get(list_key) or []
        if isinstance(raw, list):
            out[list_key] = [str(x).strip() for x in raw if str(x).strip()]
        elif isinstance(raw, str):
            # Single string → wrap in list for forgiveness.
            out[list_key] = [raw.strip()] if raw.strip() else []
    s = model.get("summary")
    if isinstance(s, str):
        out["summary"] = s.strip()
    return out


def format_for_injection(model: PeerRepresentation | None) -> str:
    """Render the structured form as a prompt block. Returns empty string if
    the model is empty / missing — the caller can skip the section entirely."""
    if not model:
        return ""
    model = _normalize(model)
    parts: list[str] = []
    if model["summary"]:
        parts.append(f"who they are: {model['summary']}")
    if model["communication_style"]:
        parts.append(f"how they text: {model['communication_style']}")
    if model["values"]:
        parts.append("things they care about: " + "; ".join(model["values"][:5]))
    if model["domain_expertise"]:
        parts.append(
            "they're competent at: " + ", ".join(model["domain_expertise"][:5])
            + ". don't condescend on these."
        )
    if model["current_concerns"]:
        parts.append("what's on their mind lately: "
                     + "; ".join(model["current_concerns"][:5]))
    if model["blindspots"]:
        parts.append(
            "blindspots you've noticed (surface obliquely, never as diagnosis): "
            + "; ".join(model["blindspots"][:3])
        )
    if not parts:
        return ""
    return "# memory: who they are (structured user model)\n" + "\n".join(
        f"- {p}" for p in parts
    )


def merge_dialectic(
    old: PeerRepresentation | None,
    new_observations: dict[str, Any] | None,
) -> PeerRepresentation:
    """Update the representation from a fresh observation pass.

    Dialectic merge rules (not blind overwrite):
      - ``communication_style`` and ``summary`` overwrite when the new value is
        non-empty (these are prose distillations; reflection knows best).
      - List fields union-merge: keep old entries, append new ones not already
        present, cap at 10 entries (oldest dropped on overflow).

    Returns the merged representation. Never raises.
    """
    base = _normalize(old) if old else empty()
    new = _normalize(new_observations) if new_observations else empty()

    if new["communication_style"]:
        base["communication_style"] = new["communication_style"]
    if new["summary"]:
        base["summary"] = new["summary"]
    for list_key in ("values", "domain_expertise", "current_concerns", "blindspots"):
        seen = {x.lower() for x in base[list_key]}
        merged = list(base[list_key])
        for item in new[list_key]:
            if item.lower() not in seen:
                merged.append(item)
                seen.add(item.lower())
        # Cap at 10 — oldest first out.
        if len(merged) > 10:
            merged = merged[-10:]
        base[list_key] = merged
    return base
