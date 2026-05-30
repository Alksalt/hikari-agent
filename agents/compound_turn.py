"""compound_turn — run multiple tasks extracted from one message.

This module has TWO entry points:

1. **Legacy dict path** (kept for existing tests + non-typed callers):
   ``run_compound_turn(tasks: list[dict])`` — dependency-wave topological
   sort, parallel within each wave, sequential between. No work_packets
   row, no risk gating.

2. **Typed packet path** (Sprint A Wave 3):
   ``run_compound_turn_typed(user_text, user_turn_id, ...)`` — extracts
   typed CompoundTaskNodes, validates deterministically, creates a
   durable ``work_packets`` row, runs read steps in parallel via
   ``asyncio.gather`` with per-step ``asyncio.wait_for`` timeout, runs
   write steps sequentially with approval conversion for
   ``approve_required`` risk, composes a final terse Hikari-voice
   receipt, and updates the packet status (done / waiting / failed).

Voice transcript path: when the input is a voice note, the bridge
pre-prefixes ``[voice note]``. The typed extractor sets
``voice_uncertainty=True`` for those — same planner code path, no fork.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from agents import config as cfg
from agents.work_packet import (
    CompoundTaskNode,
    WorkPacket,
    WorkStep,
    validate_nodes,
)

logger = logging.getLogger(__name__)

# Per-step read timeout (seconds). Configurable via engagement.yaml
# compound_turn.step_timeout_s; falls back to 12.0 if unset.
_DEFAULT_STEP_TIMEOUT = float(cfg.get("compound_turn.step_timeout_s", 12.0))


# ---------------------------------------------------------------------------
# Legacy dict-based dispatcher (kept verbatim for existing call-sites)
# ---------------------------------------------------------------------------

def _build_waves(tasks: list[dict]) -> list[list[int]]:
    """Topological sort into execution waves (each wave runs in parallel).

    Raises ValueError on dependency cycles.
    """
    n = len(tasks)
    remaining = set(range(n))
    completed: set[int] = set()
    waves: list[list[int]] = []
    while remaining:
        wave = [
            i for i in remaining
            if all(d in completed for d in tasks[i].get("depends_on", []))
        ]
        if not wave:
            raise ValueError(f"dependency cycle in tasks {remaining}")
        waves.append(wave)
        for i in wave:
            remaining.discard(i)
            completed.add(i)
    return waves


async def run_compound_turn(tasks: list[dict]) -> str:
    """Execute dict-shaped tasks in dependency order and return combined results.

    Legacy entry point. Kept verbatim for existing callers and tests.
    Single-task list: direct run_internal_control, no overhead (no sink — the
    lone child's _invoke_sdk sets LAST_TURN_TOOL_NAMES directly for that call).
    Multi-task: topological waves, parallel within each wave. A shared sink
    collects child tool names across waves and is merged into
    LAST_TURN_TOOL_NAMES via aggregate_compound_tool_calls before returning.
    """
    from agents.runtime import run_internal_control

    if len(tasks) == 1:
        return await run_internal_control(tasks[0]["task"])

    # Shared sink for multi-task path: collects all child tool names.
    sink: set[str] = set()

    try:
        waves = _build_waves(tasks)
    except ValueError:
        logger.warning("compound_turn: cycle detected — falling back to sequential")
        waves = [[i] for i in range(len(tasks))]

    results: dict[int, str] = {}
    total_successes = 0
    first_exc: Exception | None = None
    for wave in waves:
        if len(wave) == 1:
            idx = wave[0]
            results[idx] = await run_internal_control(tasks[idx]["task"], tool_names_sink=sink)
            total_successes += 1
        else:
            wave_results = await asyncio.gather(
                *[run_internal_control(tasks[idx]["task"], tool_names_sink=sink) for idx in wave],
                return_exceptions=True,
            )
            for idx, res in zip(wave, wave_results):
                if isinstance(res, Exception):
                    logger.warning("compound_turn: task %d raised: %s", idx, res)
                    if first_exc is None:
                        first_exc = res
                    results[idx] = ""
                else:
                    results[idx] = str(res)
                    total_successes += 1

    if total_successes == 0 and first_exc is not None:
        raise first_exc

    from agents.post_filter import aggregate_compound_tool_calls
    aggregate_compound_tool_calls(sink)

    parts = [results[i].strip() for i in range(len(tasks)) if results.get(i, "").strip()]
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Sprint A Wave 3 — typed packet dispatcher
# ---------------------------------------------------------------------------

# Which intents are read-shaped (parallelisable, idempotent).
_READ_INTENTS: frozenset[str] = frozenset({"read", "search", "calc"})


def _partition_steps(steps: list[WorkStep]) -> tuple[list[WorkStep], list[WorkStep]]:
    """Split steps into (reads, writes). Reads run in parallel; writes serial."""
    reads: list[WorkStep] = []
    writes: list[WorkStep] = []
    for s in steps:
        node = s.node
        if node is None or node.intent_type in _READ_INTENTS:
            reads.append(s)
        else:
            writes.append(s)
    return reads, writes


async def _run_read_step(
    step: WorkStep,
    *,
    step_timeout: float,
    tool_names_sink: set[str] | None = None,
    internal_belief_context: str | None = None,
) -> tuple[WorkStep, str | None, Exception | None]:
    """Execute one read step under a per-step timeout.

    Returns ``(step, output_or_None, exception_or_None)``. Caller updates DB.
    ``tool_names_sink``: when provided, merged with child tool names via
    ``run_internal_control``'s forwarding to ``_invoke_sdk``.
    ``internal_belief_context``: when provided, prepended to the task prompt so
    the belief-frame adversarial suffix is preserved on compound child turns.
    """
    from agents.runtime import run_internal_control
    assert step.node is not None
    task_prompt = step.node.task
    if internal_belief_context:
        task_prompt = internal_belief_context + "\n\n" + task_prompt
    try:
        coro = run_internal_control(task_prompt, tool_names_sink=tool_names_sink)
        out = await asyncio.wait_for(coro, timeout=step_timeout)
        return step, str(out), None
    except asyncio.TimeoutError as exc:
        return step, None, exc
    except Exception as exc:  # noqa: BLE001 — surface up the chain
        return step, None, exc


def _confirm_send_prompt(node: CompoundTaskNode) -> str:
    """Generate a terse CONFIRM-SEND prompt for an ``approve_required`` write."""
    tail = ""
    if node.entities:
        tail = f" ({', '.join(node.entities[:3])})"
    return f"CONFIRM-SEND: {node.task}{tail} — reply 'yes' to send, 'no' to skip."


def _compose_receipt(packet: WorkPacket) -> str:
    """Terse Hikari-voice receipt: N done / M waiting / K failed.

    Body: one short line per step. Successful read results are surfaced
    only when they're short enough to be useful; long outputs collapse
    into "done." so the user gets the structure, not a wall of text.
    """
    done: list[WorkStep] = []
    waiting: list[WorkStep] = []
    failed: list[WorkStep] = []
    skipped: list[WorkStep] = []
    for s in packet.steps:
        if s.status == "done":
            done.append(s)
        elif s.status == "waiting":
            waiting.append(s)
        elif s.status == "failed":
            failed.append(s)
        elif s.status == "skipped":
            skipped.append(s)

    # Header — only show if there's actually something multi-step worth
    # reporting. For a single done step, just return its output.
    if len(packet.steps) == 1 and done and not waiting and not failed:
        out = done[0].output_json or ""
        try:
            payload = json.loads(out)
            if isinstance(payload, dict) and "text" in payload:
                return str(payload["text"]).strip()
        except (json.JSONDecodeError, TypeError):
            pass
        return out.strip()

    parts: list[str] = []
    counts = []
    if done:
        counts.append(f"{len(done)} done")
    if waiting:
        counts.append(f"{len(waiting)} waiting")
    if failed:
        counts.append(f"{len(failed)} failed")
    if skipped:
        counts.append(f"{len(skipped)} skipped")
    if counts:
        parts.append(" / ".join(counts) + ".")

    # One line per outcome — keep it terse.
    for s in done:
        body = (s.output_json or "").strip()
        if not body:
            continue
        first_line = body.splitlines()[0][:140]
        parts.append(f"- {first_line}")
    for s in waiting:
        if s.node is not None:
            parts.append(f"- waiting: {_confirm_send_prompt(s.node)}")
        else:
            parts.append("- waiting: needs your confirm.")
    for s in failed:
        why = (s.error or "unknown").strip().splitlines()[0][:100]
        parts.append(f"- failed: {why}")
    for s in skipped:
        parts.append("- skipped.")

    return "\n".join(parts).strip()


async def _fallback_single_llm(user_text: str) -> str:
    """Escalate to a single LLM turn when validation fails or extractor errors."""
    from agents.runtime import run_internal_control
    logger.info("compound_turn_typed: falling back to single-LLM turn")
    return await run_internal_control(user_text)


async def run_compound_turn_typed(
    user_text: str,
    *,
    user_turn_id: str,
    step_timeout: float = _DEFAULT_STEP_TIMEOUT,
    is_voice: bool = False,
    internal_belief_context: str | None = None,
    internal_reply_context: str | None = None,
) -> str:
    """Sprint A Wave 3 typed planner.

    Steps:
      1. Call task_extractor → list[CompoundTaskNode].
      2. validate_nodes → on error, fall back to single-LLM.
      3. Create work_packets row + work_packet_steps rows.
      4. Partition: reads in parallel via asyncio.gather + asyncio.wait_for
         timeout; writes sequential with approval conversion for
         ``approve_required`` (mark waiting + CONFIRM-SEND prompt).
      5. Update step status after each completion.
      6. Compose terse Hikari-voice receipt.
      7. Mark work_packet done / failed / waiting.

    Voice path: ``is_voice=True`` is informational — the extractor inspects
    the message body itself and sets ``voice_uncertainty`` on each node.

    ``internal_belief_context``: when provided (belief-frame adversarial suffix
    computed by the bridge), prepended to each child step's task prompt so the
    belief-frame guard is active on compound turns as well as normal turns.
    ``internal_reply_context``: Telegram reply-quote context (built by the
    bridge); folded into the same prefix channel so a quoted message frames
    every child step too.

    Returns the final user-facing reply text.
    """
    from storage import db
    from tools.dispatch.task_extractor import extract_typed_nodes
    from tools.runtime.progress import _PROGRESS_STATE
    from tools.runtime.progress import progress as _progress
    from agents.runtime import current_turn_id as _ctv

    # Fold the Telegram reply-quote context (if any) into the same prompt-prefix
    # channel the belief-frame guard uses, so both ride along on every child
    # step without threading a second param through reads/writes. Reply context
    # leads so the quoted message frames the belief suffix that follows.
    if internal_reply_context:
        internal_belief_context = (
            internal_reply_context
            if not internal_belief_context
            else internal_reply_context + "\n\n" + internal_belief_context
        )

    # Initialize rate-limit state for this turn so _progress can gate correctly.
    _PROGRESS_STATE.set({
        "turn_id": _ctv() or user_turn_id,
        "count": 0,
        "last_ts": 0.0,
        "single_step": False,
    })

    # Shared sink that collects every tool name invoked by child
    # run_internal_control calls. Passed into _run_read_step (parallel reads)
    # and the sequential write dispatch so aggregation catches both. Merged
    # into LAST_TURN_TOOL_NAMES before the final receipt via
    # aggregate_compound_tool_calls — prevents the fabrication backstop from
    # clobbering real inbox/calendar receipts produced in child turns.
    # set.update / |= holds the GIL and there is no await between a child's
    # SDK loop returning and its sink update, so no lock is needed.
    tool_names_sink: set[str] = set()

    # 1. Extract typed nodes
    try:
        nodes = await extract_typed_nodes(user_text)
    except (ValueError, json.JSONDecodeError) as exc:
        logger.warning("compound_turn_typed: extractor failed (%s) — single-LLM fallback", exc)
        return await _fallback_single_llm(user_text)
    except Exception as exc:  # noqa: BLE001 — transport/runtime issues
        logger.warning("compound_turn_typed: extractor exception (%s) — single-LLM fallback", exc)
        return await _fallback_single_llm(user_text)

    # 2. Validate deterministically
    errors = validate_nodes(nodes, full_text=user_text)
    if errors:
        logger.warning("compound_turn_typed: validation errors %s — single-LLM fallback", errors)
        return await _fallback_single_llm(user_text)

    # Single-step packets skip all progress beats — mark state and continue.
    if len(nodes) <= 1:
        state = _PROGRESS_STATE.get()
        state["single_step"] = True
        _PROGRESS_STATE.set(state)
    else:
        # Multi-step: emit first beat so the user sees activity immediately.
        await _progress.handler({"message": "...looking that up.", "mode": "auto"})

    # If voice note flag came from the bridge but no node carries it, lift it.
    if is_voice:
        for n in nodes:
            n.voice_uncertainty = True

    # 3. Create durable packet + steps
    packet_id = db.work_packet_create(user_turn_id, summary=user_text[:140])
    db.work_packet_update_status(packet_id, "running")

    steps: list[WorkStep] = []
    for i, node in enumerate(nodes):
        tool_name = f"{node.intent_type}:{(node.entities[0] if node.entities else 'auto')}"
        input_json = json.dumps(node.to_dict(), ensure_ascii=False)
        step_id = db.work_packet_step_insert(
            packet_id, i, tool_name, input_json=input_json
        )
        steps.append(WorkStep(
            step_id=step_id,
            step_index=i,
            tool_name=tool_name,
            input_json=input_json,
            node=node,
        ))

    packet = WorkPacket(
        packet_id=packet_id,
        user_turn_id=user_turn_id,
        task_nodes=nodes,
        steps=steps,
        status="running",
    )

    # 4. Partition + execute
    reads, writes = _partition_steps(steps)

    # 4a. Reads — parallel with per-step timeout
    if reads:
        # Mark all as running first (best-effort).
        for s in reads:
            db.work_packet_step_update(s.step_id, status="running")
        results = await asyncio.gather(
            *[_run_read_step(
                s,
                step_timeout=step_timeout,
                tool_names_sink=tool_names_sink,
                internal_belief_context=internal_belief_context,
            ) for s in reads],
            return_exceptions=False,  # _run_read_step never re-raises
        )
        for step, output, exc in results:
            if exc is not None:
                step.status = "failed"
                step.error = f"{type(exc).__name__}: {exc}"[:500]
                db.work_packet_step_update(
                    step.step_id,
                    status="failed",
                    error=step.error,
                    finished=True,
                )
            else:
                raw_output = output or ""
                from agents.runtime import looks_like_sdk_error
                if looks_like_sdk_error(raw_output):
                    step.status = "failed"
                    step.error = "sdk_error_leak"
                    db.work_packet_step_update(
                        step.step_id,
                        status="failed",
                        error=step.error,
                        finished=True,
                    )
                else:
                    step.status = "done"
                    step.output_json = raw_output
                    # Wrap plain text in a JSON object for consistent receipt parsing.
                    payload = json.dumps({"text": step.output_json}, ensure_ascii=False)
                    db.work_packet_step_update(
                        step.step_id,
                        status="done",
                        output_json=payload,
                        finished=True,
                    )

    # 4b. Writes — sequential, with approval conversion for approve_required.
    if writes:
        await _progress.handler({"message": "ok. now the writes.", "mode": "auto"})
    for s in writes:
        node = s.node
        assert node is not None
        # Risk gates
        if node.risk_class == "blocked":
            s.status = "skipped"
            db.work_packet_step_update(
                s.step_id, status="skipped",
                output_json=json.dumps({"reason": "risk_class=blocked"}, ensure_ascii=False),
                finished=True,
            )
            continue
        if node.risk_class == "approve_required":
            # Approval conversion: do NOT execute. Generate prompt + mark waiting.
            await _progress.handler({"message": "...one needs your ok.", "surprise": True})
            prompt = _confirm_send_prompt(node)
            s.status = "waiting"
            db.work_packet_step_update(
                s.step_id, status="waiting",
                output_json=json.dumps({"confirm_prompt": prompt}, ensure_ascii=False),
            )
            continue
        # safe write — run it
        db.work_packet_step_update(s.step_id, status="running")
        try:
            from agents.runtime import looks_like_sdk_error, run_internal_control
            write_task = node.task
            if internal_belief_context:
                write_task = internal_belief_context + "\n\n" + write_task
            out = await asyncio.wait_for(
                run_internal_control(write_task, tool_names_sink=tool_names_sink),
                timeout=step_timeout * 2,  # writes get a bigger budget
            )
            raw_out = str(out)
            if looks_like_sdk_error(raw_out):
                s.status = "failed"
                s.error = "sdk_error_leak"
                db.work_packet_step_update(
                    s.step_id, status="failed", error=s.error, finished=True,
                )
            else:
                s.status = "done"
                s.output_json = raw_out
                db.work_packet_step_update(
                    s.step_id, status="done",
                    output_json=json.dumps({"text": s.output_json}, ensure_ascii=False),
                    finished=True,
                )
        except asyncio.TimeoutError as exc:
            s.status = "failed"
            s.error = f"TimeoutError: write step timed out after {step_timeout * 2}s"
            db.work_packet_step_update(
                s.step_id, status="failed", error=s.error, finished=True,
            )
        except Exception as exc:  # noqa: BLE001
            s.status = "failed"
            s.error = f"{type(exc).__name__}: {exc}"[:500]
            db.work_packet_step_update(
                s.step_id, status="failed", error=s.error, finished=True,
            )

    # 5. Final packet status
    statuses = {s.status for s in steps}
    if "waiting" in statuses:
        final_status = "waiting"
        finished = False
    elif statuses <= {"done", "skipped"}:
        final_status = "done"
        finished = True
    elif "done" in statuses or "skipped" in statuses:
        # Mixed done + failed — packet is "done" (we landed something) but
        # report failures in the receipt.
        final_status = "done"
        finished = True
    else:
        final_status = "failed"
        finished = True
    packet.status = final_status
    db.work_packet_update_status(packet_id, final_status, finished=finished)

    # 6. Aggregate child tool names into parent LAST_TURN_TOOL_NAMES so the
    #    fabrication backstop (filter_outgoing) sees real fetches from child turns.
    from agents.post_filter import aggregate_compound_tool_calls
    aggregate_compound_tool_calls(tool_names_sink)

    # 7. Receipt
    return _compose_receipt(packet)


__all__ = [
    "run_compound_turn",
    "run_compound_turn_typed",
    "_build_waves",
    "_compose_receipt",
    "_partition_steps",
]
