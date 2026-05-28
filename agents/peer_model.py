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
    their_model_of_me: dict


def empty() -> PeerRepresentation:
    """Return an empty (but well-shaped) representation. Used at first-run."""
    return {
        "communication_style": "",
        "values": [],
        "domain_expertise": [],
        "current_concerns": [],
        "blindspots": [],
        "summary": "",
        "their_model_of_me": {},
    }


class SelfRepresentation(TypedDict, total=False):
    current_voice_register: str
    recent_deflection_rate: float
    mood_prediction_accuracy: float
    drift_vectors: list[str]
    last_updated_iso: str


def empty_self() -> SelfRepresentation:
    return {
        "current_voice_register": "",
        "recent_deflection_rate": 0.0,
        "mood_prediction_accuracy": 0.0,
        "drift_vectors": [],
        "last_updated_iso": "",
    }


def merge_self_dialectic(
    old: SelfRepresentation | dict | None,
    new: dict | None,
) -> SelfRepresentation:
    """Prose overwrite on string fields, list cap 5 on drift_vectors, floats
    overwrite when non-zero. Returns dict for db storage."""
    if not old:
        old = empty_self()
    if not new:
        return dict(old)  # type: ignore[return-value]
    out = dict(old)
    for k in ("current_voice_register", "last_updated_iso"):
        v = new.get(k)
        if v:
            out[k] = str(v)
    for k in ("recent_deflection_rate", "mood_prediction_accuracy"):
        v = new.get(k)
        if v is not None and float(v) > 0:
            out[k] = float(v)
    drift = new.get("drift_vectors") or []
    if drift:
        combined = (out.get("drift_vectors") or []) + [str(x) for x in drift]
        out["drift_vectors"] = combined[-5:]  # cap last 5
    return out  # type: ignore[return-value]


def format_self_for_injection(model: SelfRepresentation | dict | None) -> str:
    """Render the # self-model block (<=100 tokens). Returns empty if all empty."""
    if not model:
        return ""
    voice = (model.get("current_voice_register") or "").strip()
    rate = model.get("recent_deflection_rate") or 0.0
    drift = model.get("drift_vectors") or []
    if not voice and not rate and not drift:
        return ""
    lines = ["# self-model"]
    if voice:
        lines.append(f"- voice: {voice}")
    if rate:
        lines.append(f"- recent deflection rate: {rate:.2f}")
    if drift:
        recent = drift[-3:] if len(drift) > 3 else drift
        lines.append("- recent drifts: " + "; ".join(recent))
    return "\n".join(lines)


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
    tmom = model.get("their_model_of_me")
    if isinstance(tmom, dict):
        out["their_model_of_me"] = tmom
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
    # their_model_of_me: shallow merge — new keys overwrite, old keys preserved.
    new_tmom = new.get("their_model_of_me") or {}
    if new_tmom and isinstance(new_tmom, dict):
        existing = dict(base.get("their_model_of_me") or {})
        existing.update(new_tmom)
        base["their_model_of_me"] = existing
    return base
