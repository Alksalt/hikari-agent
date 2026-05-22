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
