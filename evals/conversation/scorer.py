"""LLM-as-judge scoring for Layer B.

NOT WIRED in Sprint 5C MVP — placeholder for the Sonnet judge call.
Layer B runner currently raises NotImplementedError; this module will be
filled in when Layer B execution lands.
"""
from __future__ import annotations


async def score_response(
    user_prompt: str, response: str, rubrics: dict[str, float]
) -> dict:
    raise NotImplementedError(
        "scorer.score_response: Layer B not yet implemented — Sprint 5C-MVP"
    )
