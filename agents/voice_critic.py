"""T8.2 — voice_critic: bounded Haiku call that judges each outbound draft.

Silicon Mirror Generator-Critic pattern (arxiv 2604.00478): the Sonnet lead
drafts, a Haiku critic verdicts PASS or REWRITE, and the lead gets one
re-prompt if the critic rejects. The paper documented a Sonnet 4 sycophancy
drop from 9.6% to 1.4% with this configuration. Nature 2026 warm-training
paper showed training models warmer raises sycophancy, so a critic is
load-bearing for a persona that's engineered warm-under-the-mask.

The subagent prompt + role lives in ``agents.subagents.VOICE_CRITIC_AGENT``
(also registered in ``ALL_AGENTS`` so it's discoverable to the lead at agent
introspection time). The actual call here uses a bare ``ClaudeSDKClient``
mirroring ``post_filter.bounded_rewrite`` because outbound choreography runs
*outside* the agent loop — there's no Agent tool available at send time.

Latency: one Haiku call per outbound message, ~300ms p50 for short drafts.
Toggle via ``voice_critic.enabled`` in ``config/engagement.yaml``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from . import config as cfg
from .subagents import VOICE_CRITIC_AGENT

logger = logging.getLogger(__name__)


@dataclass
class CritiqueVerdict:
    """Result of one voice-critic round."""

    verdict: str  # "PASS" | "REWRITE" | "ERROR"
    reason: str | None  # populated for REWRITE; None for PASS
    raw: str  # exact text the critic returned (for debugging)


def _enabled() -> bool:
    return bool(cfg.get("voice_critic.enabled", True))


def _model() -> str:
    return str(cfg.get("voice_critic.model", "claude-haiku-4-5"))


def _max_budget() -> float:
    # One Haiku judgment call; ~300 input tokens + 30 output. Keep cap tight
    # so we fail fast if something goes off the rails.
    return float(cfg.get("voice_critic.max_budget_usd", 0.01))


def _parse_verdict(raw: str) -> CritiqueVerdict:
    """Coerce the critic's response into a typed verdict.

    Accepts case-insensitively. Unrecognized responses are treated as PASS
    rather than REWRITE — we err on the side of shipping the lead's draft if
    the critic itself goes off-script.
    """
    text = (raw or "").strip()
    if not text:
        return CritiqueVerdict(verdict="PASS", reason=None, raw="")
    head = text.split("\n", 1)[0].strip()
    upper = head.upper()
    if upper.startswith("PASS"):
        return CritiqueVerdict(verdict="PASS", reason=None, raw=text)
    if upper.startswith("REWRITE"):
        # "REWRITE: <reason>" — extract anything after the first colon.
        _, _, reason_part = head.partition(":")
        reason = reason_part.strip() or "no reason given"
        return CritiqueVerdict(verdict="REWRITE", reason=reason, raw=text)
    # Unknown shape — treat as PASS so we don't drop the draft on a critic
    # malfunction. Log it so the daily reflection can spot misbehavior.
    logger.info("voice_critic: unparseable verdict head=%r; defaulting PASS", head)
    return CritiqueVerdict(verdict="PASS", reason=None, raw=text)


async def critique_draft(draft: str) -> CritiqueVerdict:
    """Run one voice-critic round on ``draft`` and return the verdict.

    On any SDK failure, returns ``CritiqueVerdict(verdict="ERROR", ...)`` so
    callers can ship the original (the critic is best-effort, not a gate we
    want to drop messages on).
    """
    if not _enabled() or not draft or not draft.strip():
        return CritiqueVerdict(verdict="PASS", reason=None, raw="")

    try:
        from claude_agent_sdk import (
            AssistantMessage,
            ClaudeAgentOptions,
            ClaudeSDKClient,
            TextBlock,
        )
    except Exception:
        logger.exception("voice_critic: SDK import failed")
        return CritiqueVerdict(
            verdict="ERROR", reason="sdk_import_failed", raw="",
        )

    # Reuse the agent's prompt so the subagent definition stays the single
    # source of truth. We render it as a system prompt + the draft as the
    # user turn.
    options = ClaudeAgentOptions(
        model=_model(),
        max_turns=1,
        max_budget_usd=_max_budget(),
        allowed_tools=[],
        permission_mode="acceptEdits",
        system_prompt=VOICE_CRITIC_AGENT.prompt,
    )
    parts: list[str] = []
    try:
        async with ClaudeSDKClient(options=options) as client:
            await client.query(f"DRAFT:\n{draft}")
            async for msg in client.receive_response():
                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            parts.append(block.text)
    except Exception:
        logger.exception("voice_critic: SDK call failed")
        return CritiqueVerdict(
            verdict="ERROR", reason="sdk_call_failed", raw="",
        )

    raw = "".join(parts).strip()
    return _parse_verdict(raw)
