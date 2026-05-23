from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal


@dataclass(frozen=True)
class TriggerCandidate:
    source: str
    pattern: Literal["notify", "question", "review"]
    payload: dict[str, Any]
    dedup_key: str
    decay_at: datetime
    pool: str = "user_anchored"       # "user_anchored" / "agent_spontaneous" / "scheduled_ceremony"
    novelty: float = 0.5              # 0..1
    actionability: float = 0.5        # 0..1
    confidence: float = 0.8           # 0..1
