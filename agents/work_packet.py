"""work_packet — typed CompoundTaskNode + WorkPacket / WorkStep models.

Sprint A Wave 3 (compound-turn-typed).

This module defines the *typed* compound-turn data model that replaces the
old raw ``list[dict]`` extractor output:

- ``CompoundTaskNode``: a single atomic task with intent, span, entities,
  time refs, risk class, approval policy, confidence and voice-uncertainty
  flag. Extracted by the LLM, validated deterministically.
- ``WorkStep``: runtime row tied to a ``work_packet_steps`` DB row — carries
  durable status (pending/running/done/waiting/failed/skipped/cancelled).
- ``WorkPacket``: a single user-turn's worth of nodes + steps, tied to a
  ``work_packets`` DB row.
- ``validate_nodes``: deterministic schema/consistency checks. Returns a
  list of human-readable error strings; empty list = OK.
- ``from_raw_dict``: tolerant constructor that accepts the JSON the LLM
  emits (with defaults / clamping).

No Pydantic dep — uses ``dataclasses`` to stay in line with the rest of
the agents/ tree.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field, asdict
from typing import Any, Literal

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Type aliases / vocabularies
# ---------------------------------------------------------------------------

IntentType = Literal["read", "write", "search", "compose", "dispatch", "calc"]
RiskClass = Literal["safe", "approve_required", "blocked"]
ApprovalPolicy = Literal["auto", "confirm_send", "block"]
StepStatus = Literal[
    "pending", "running", "done", "waiting", "failed", "skipped", "cancelled"
]

_INTENTS: tuple[str, ...] = ("read", "write", "search", "compose", "dispatch", "calc")
_RISKS: tuple[str, ...] = ("safe", "approve_required", "blocked")
_POLICIES: tuple[str, ...] = ("auto", "confirm_send", "block")

# Time-ref hint vocabulary — used only for deterministic parse-cleanness
# validation (not full NLP). Anything matching one of these patterns is
# considered "clean". Anything else passes through but emits a soft warning
# from ``validate_nodes`` (not a hard error).
_TIME_REF_RE = re.compile(
    r"^("
    r"(in|за)\s+\d+\s*(s|sec|seconds?|m|min|minutes?|h|hr|hours?|d|days?|w|weeks?|год|годин|хвилин|"
    r"мин|минут|час|часов|днів|днях|неділь|тижнів)"
    r"|"
    r"(today|tomorrow|tonight|yesterday|now|next\s+(week|month|monday|tuesday|wednesday|thursday|friday|saturday|sunday)"
    r"|tomorrow\s+(morning|afternoon|evening|night)"
    r"|(monday|tuesday|wednesday|thursday|friday|saturday|sunday)(\s+morning|\s+afternoon|\s+evening|\s+night)?"
    r"|\d{1,2}(:\d{2})?\s*(am|pm)?"
    r"|сегодня|завтра|вчера|сейчас|завтра\s+(утром|вечером|днем|ночью)"
    r"|сьогодні|завтра|вчора|зараз"
    r")"
    r")$",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# CompoundTaskNode
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class CompoundTaskNode:
    """A single typed task node from the planner.

    All fields are required, but ``from_raw_dict`` fills sane defaults for
    LLM output that's missing fields. ``validate_nodes`` enforces invariants
    at execution gate time.
    """

    intent_type: IntentType
    utterance_span: tuple[int, int]
    entities: list[str] = field(default_factory=list)
    time_refs: list[str] = field(default_factory=list)
    risk_class: RiskClass = "safe"
    approval_policy: ApprovalPolicy = "auto"
    confidence: float = 0.5
    voice_uncertainty: bool = False
    # Free-text body (the actual task instruction to dispatch).
    task: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        # Preserve tuple as 2-list for JSON round-trip
        d["utterance_span"] = [int(self.utterance_span[0]), int(self.utterance_span[1])]
        return d

    @classmethod
    def from_raw_dict(cls, raw: dict[str, Any], *, full_text: str | None = None) -> "CompoundTaskNode":
        """Construct from a tolerant dict (LLM output).

        Missing fields → defaults. Out-of-vocab enums → ``safe`` / ``auto``
        / ``read``. Spans clamped to ``[0, len(full_text)]`` if provided.
        ``confidence`` clamped to ``[0.0, 1.0]``. Raises ``ValueError`` only
        when ``raw`` is fundamentally wrong (not a dict, no task text).
        """
        if not isinstance(raw, dict):
            raise ValueError(f"CompoundTaskNode.from_raw_dict: expected dict, got {type(raw).__name__}")
        task_text = str(raw.get("task") or raw.get("body") or "").strip()
        if not task_text:
            raise ValueError("CompoundTaskNode.from_raw_dict: missing 'task' / 'body' text")

        intent_raw = str(raw.get("intent_type") or raw.get("intent") or "read").lower().strip()
        intent: IntentType = intent_raw if intent_raw in _INTENTS else "read"  # type: ignore[assignment]

        span_raw = raw.get("utterance_span") or raw.get("span") or [0, 0]
        try:
            s_lo = int(span_raw[0])
            s_hi = int(span_raw[1])
        except (TypeError, ValueError, IndexError):
            s_lo, s_hi = 0, 0
        if full_text is not None:
            n = len(full_text)
            s_lo = max(0, min(s_lo, n))
            s_hi = max(0, min(s_hi, n))
        if s_hi < s_lo:
            s_lo, s_hi = s_hi, s_lo

        ents_raw = raw.get("entities") or []
        entities = [str(e).strip() for e in ents_raw if str(e).strip()] if isinstance(ents_raw, list) else []

        trefs_raw = raw.get("time_refs") or raw.get("time") or []
        time_refs = [str(t).strip() for t in trefs_raw if str(t).strip()] if isinstance(trefs_raw, list) else []

        risk_raw = str(raw.get("risk_class") or raw.get("risk") or "safe").lower().strip()
        risk: RiskClass = risk_raw if risk_raw in _RISKS else "safe"  # type: ignore[assignment]

        policy_raw = str(raw.get("approval_policy") or raw.get("approval") or "auto").lower().strip()
        policy: ApprovalPolicy = policy_raw if policy_raw in _POLICIES else "auto"  # type: ignore[assignment]

        try:
            conf = float(raw.get("confidence", 0.5))
        except (TypeError, ValueError):
            conf = 0.5
        conf = max(0.0, min(1.0, conf))

        voice_unc = bool(raw.get("voice_uncertainty", False))

        return cls(
            intent_type=intent,
            utterance_span=(s_lo, s_hi),
            entities=entities,
            time_refs=time_refs,
            risk_class=risk,
            approval_policy=policy,
            confidence=conf,
            voice_uncertainty=voice_unc,
            task=task_text,
        )


# ---------------------------------------------------------------------------
# WorkStep / WorkPacket runtime models
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class WorkStep:
    """Runtime mirror of a ``work_packet_steps`` row."""

    step_id: int
    step_index: int
    tool_name: str
    input_json: str | None = None
    status: StepStatus = "pending"
    output_json: str | None = None
    error: str | None = None
    # Link back to the node that produced it (for receipt composition).
    node: CompoundTaskNode | None = None


@dataclass(slots=True)
class WorkPacket:
    """Runtime mirror of a ``work_packets`` row + its steps + source nodes."""

    packet_id: int
    user_turn_id: str
    task_nodes: list[CompoundTaskNode] = field(default_factory=list)
    steps: list[WorkStep] = field(default_factory=list)
    status: str = "planning"


# ---------------------------------------------------------------------------
# Deterministic validation
# ---------------------------------------------------------------------------

def validate_nodes(
    nodes: list[CompoundTaskNode], *, full_text: str | None = None
) -> list[str]:
    """Deterministic invariant checks. Returns list of error strings.

    Empty list = OK to execute. Any non-empty list → caller should escalate
    to single-LLM fallback (per Wave 3 scope).

    Checked invariants:
      1. At least one node.
      2. Each node's ``utterance_span`` is non-negative and ordered.
      3. Spans fit inside ``full_text`` if provided.
      4. Spans don't *overlap* (touching at a boundary is OK).
      5. ``intent_type`` in vocabulary.
      6. ``risk_class`` is consistent with ``approval_policy``:
            - ``safe`` ↔ ``auto``
            - ``approve_required`` ↔ ``confirm_send``
            - ``blocked``         ↔ ``block``
      7. ``confidence`` in [0, 1].
      8. ``time_refs`` parse cleanly via the regex hint (soft check —
         only flagged when at least one ref *clearly* doesn't look
         like a time expression at all, e.g. raw ``task`` body leaked
         into the slot).
    """
    errors: list[str] = []
    if not nodes:
        errors.append("no nodes — extractor returned empty list")
        return errors

    # 1. Per-node invariants
    spans: list[tuple[int, int, int]] = []  # (lo, hi, idx)
    for i, n in enumerate(nodes):
        if n.intent_type not in _INTENTS:
            errors.append(f"node[{i}]: intent_type={n.intent_type!r} not in {_INTENTS}")
        if n.risk_class not in _RISKS:
            errors.append(f"node[{i}]: risk_class={n.risk_class!r} not in {_RISKS}")
        if n.approval_policy not in _POLICIES:
            errors.append(f"node[{i}]: approval_policy={n.approval_policy!r} not in {_POLICIES}")
        if not 0.0 <= n.confidence <= 1.0:
            errors.append(f"node[{i}]: confidence={n.confidence} out of [0,1]")

        lo, hi = n.utterance_span
        if lo < 0 or hi < 0:
            errors.append(f"node[{i}]: utterance_span has negative offset {n.utterance_span}")
        if hi < lo:
            errors.append(f"node[{i}]: utterance_span end < start {n.utterance_span}")
        if full_text is not None and hi > len(full_text):
            errors.append(
                f"node[{i}]: utterance_span end {hi} exceeds text length {len(full_text)}"
            )

        # Risk ↔ policy consistency
        expected_policy: dict[str, str] = {
            "safe": "auto",
            "approve_required": "confirm_send",
            "blocked": "block",
        }
        if n.risk_class in expected_policy and n.approval_policy != expected_policy[n.risk_class]:
            errors.append(
                f"node[{i}]: risk_class={n.risk_class!r} requires "
                f"approval_policy={expected_policy[n.risk_class]!r}, "
                f"got {n.approval_policy!r}"
            )

        # Time-ref parse-cleanness (soft)
        for t in n.time_refs:
            if len(t) > 80:
                errors.append(f"node[{i}]: time_ref too long ({len(t)} chars): {t!r}")
                continue
            if not _TIME_REF_RE.match(t.strip()):
                # Only flag if it really doesn't look like time at all.
                # Many natural-language refs slip past the regex — only
                # flag the egregious cases (no digits, no time words).
                ts = t.strip().lower()
                time_words = ("today", "tomorrow", "tonight", "yesterday", "now",
                              "morning", "evening", "afternoon", "night",
                              "next", "сегодня", "завтра", "вчера", "сейчас",
                              "сьогодні", "завтра", "вчора", "зараз",
                              "min", "hour", "day", "week", "month", "год", "хвилин")
                has_digit = any(ch.isdigit() for ch in ts)
                has_time_word = any(w in ts for w in time_words)
                if not (has_digit or has_time_word):
                    errors.append(f"node[{i}]: time_ref doesn't parse: {t!r}")

        if n.task.strip() == "":
            errors.append(f"node[{i}]: empty task body")

        spans.append((lo, hi, i))

    # 2. Span overlap (only meaningful when both spans are non-zero-width)
    spans_sorted = sorted([s for s in spans if s[1] > s[0]], key=lambda x: (x[0], x[1]))
    for a, b in zip(spans_sorted, spans_sorted[1:]):
        # Touching boundary (a.hi == b.lo) is OK. True overlap = a.hi > b.lo.
        if a[1] > b[0]:
            errors.append(
                f"utterance_span overlap between node[{a[2]}]={a[:2]} "
                f"and node[{b[2]}]={b[:2]}"
            )

    return errors


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------

__all__ = [
    "CompoundTaskNode",
    "WorkPacket",
    "WorkStep",
    "validate_nodes",
    "IntentType",
    "RiskClass",
    "ApprovalPolicy",
    "StepStatus",
]
