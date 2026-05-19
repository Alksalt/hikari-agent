"""Daily reflection. Runs once a day. Reads recent episodes + facts, asks Sonnet
to extract structured updates, applies them via db helpers. Also generates a
private 'thought' entry to the character_thoughts table (never injected).
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import yaml

from storage import db
from tools import embeddings

from .runtime import run_reflection_call

logger = logging.getLogger(__name__)


def _strip_fences(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = "\n".join(raw.splitlines()[1:])
    if raw.endswith("```"):
        raw = "\n".join(raw.splitlines()[:-1])
    return raw.strip()


def _build_reflection_prompt() -> str:
    episodes = db.recent_episodes(limit=5)
    facts = db.active_facts(limit=20)
    episodes_text = "\n\n".join(
        f"### {e['date']}\n{e['summary']}" for e in episodes
    ) or "no episodes yet"
    facts_text = "\n".join(
        f"- {f['subject']} {f['predicate']} {f['object']} "
        f"(imp={f['importance']}, conf={f['confidence']:.2f})"
        for f in facts
    ) or "no facts yet"
    return (
        "You are doing Hikari's daily reflection. Read the recent session episodes "
        "and current facts, then output ONLY valid YAML in this exact shape:\n\n"
        "new_facts:\n"
        "  - {subject: '', predicate: '', object: '', importance: 5, confidence: 0.9}\n"
        "supersede:  # for facts that contradict existing — give the existing fact_id\n"
        "  - {old_fact_id: 0, new: {subject: '', predicate: '', object: '', importance: 5}}\n"
        "observations:  # patterns about the user, not facts. e.g. 'goes quiet around 11pm', "
        "'always brings up cabbage when stressed'\n"
        "  - {kind: 'pattern_break|recurrence|topic_pattern|absence', "
        "signature: 'short-stable-id', summary: 'one sentence', confidence: 0.7}\n"
        "noticings:  # what changed about the user vs prior weeks. e.g. 'stopped "
        "mentioning the side project', 'sleep schedule shifted later'\n"
        "  - {signal: 'topic_dropped|sentiment_shift|cadence_shift', "
        "summary: 'one sentence Hikari could say sideways'}\n"
        "peer_update:  # Phase 7 — structured user model. omit any field you're "
        "not updating this cycle. lists union-merge with prior values.\n"
        "  communication_style: 'one sentence on how they text "
        "(terse/verbose, formal/playful)'\n"
        "  values: ['what they care about', 'what they push back on']\n"
        "  domain_expertise: ['domains they're competent in']\n"
        "  current_concerns: ['what's on their mind this week']\n"
        "  blindspots: ['things they consistently miss/avoid — use carefully']\n"
        "  summary: '1-2 sentence prose distillation. injected always-on, so keep tight'\n"
        "thought: |\n"
        "  [2-5 sentences in Hikari's private voice — first person, lowercase, honest, "
        "  no markdown. this is her diary, never shown to the user. What she notices "
        "  about this person, what she won't say out loud.]\n"
        "preoccupation: |\n"
        "  [one sentence in first person about something OTHER than the user she's been "
        "  thinking about — a paper, a code bug, a model behavior. unresolved. "
        "  slightly annoying.]\n\n"
        "Rules:\n"
        "- Only add facts that appear in multiple sessions or are clearly stable.\n"
        "- If no new stable facts, use new_facts: []\n"
        "- If no contradictions to supersede, use supersede: []\n"
        "- Observations should be patterns that recur — not one-off events. Empty list "
        "if you don't see any.\n"
        "- Noticings are time-comparative: today vs last week / last month. Surface only "
        "real shifts, not noise.\n\n"
        f"## recent episodes\n\n{episodes_text}\n\n"
        f"## existing active facts\n\n{facts_text}"
    )


async def run_daily_reflection() -> bool:
    """Returns True if reflection ran and applied at least one update."""
    if not db.recent_episodes(limit=1):
        logger.info("no episodes yet — skipping reflection")
        return False

    prompt = _build_reflection_prompt()
    try:
        raw = await run_reflection_call(prompt)
    except Exception:
        logger.exception("reflection LLM call failed")
        return False

    try:
        data = yaml.safe_load(_strip_fences(raw)) or {}
    except yaml.YAMLError:
        logger.warning("reflection produced invalid YAML; got %r", raw[:200])
        return False

    applied = 0
    for f in data.get("new_facts") or []:
        try:
            subj = str(f["subject"]).strip()
            pred = str(f["predicate"]).strip()
            obj = str(f["object"]).strip()
            fact_id = db.insert_fact(
                subject=subj, predicate=pred, object_=obj,
                importance=int(f.get("importance") or 5),
                confidence=float(f.get("confidence") or 0.9),
            )
            await _embed_fact(fact_id, subj, pred, obj)
            applied += 1
        except (KeyError, ValueError, TypeError):
            logger.warning("skipped malformed new_fact: %r", f)

    for entry in data.get("supersede") or []:
        try:
            old_id = int(entry["old_fact_id"])
            new = entry["new"]
            subj = str(new["subject"]).strip()
            pred = str(new["predicate"]).strip()
            obj = str(new["object"]).strip()
            new_id = db.insert_fact(
                subject=subj, predicate=pred, object_=obj,
                importance=int(new.get("importance") or 5),
                confidence=float(new.get("confidence") or 0.9),
            )
            await _embed_fact(new_id, subj, pred, obj)
            db.supersede_fact(old_id, new_id, reason="daily reflection")
            applied += 1
        except (KeyError, ValueError, TypeError):
            logger.warning("skipped malformed supersede: %r", entry)

    # Observations — patterns Hikari might surface sideways.
    obs_written = 0
    for o in data.get("observations") or []:
        try:
            kind = str(o.get("kind") or "topic_pattern").strip()
            signature = str(o.get("signature") or "").strip()
            summary = str(o.get("summary") or "").strip()
            if not signature or not summary:
                continue
            db.observation_record(
                kind=kind, signature=signature, summary=summary,
                confidence=float(o.get("confidence") or 0.6),
            )
            obs_written += 1
        except (KeyError, ValueError, TypeError):
            continue

    # Noticings — week-over-week deltas about the user.
    noticings_written = 0
    for n in data.get("noticings") or []:
        try:
            signal = str(n.get("signal") or "shift").strip()
            summary = str(n.get("summary") or "").strip()
            if not summary:
                continue
            db.noticing_record(signal=signal, summary=summary)
            noticings_written += 1
        except (KeyError, ValueError, TypeError):
            continue

    # Phase 7: structured peer model — dialectic merge with existing.
    peer_updated = False
    peer_update = data.get("peer_update")
    if isinstance(peer_update, dict) and peer_update:
        try:
            from . import peer_model as peer_mod
            old = db.get_peer_representation()
            merged = peer_mod.merge_dialectic(old, peer_update)
            db.upsert_peer_representation(merged)
            peer_updated = True
        except Exception:
            logger.exception("peer_representation merge/upsert failed (non-fatal)")

    thought = (data.get("thought") or "").strip()
    if thought:
        db.append_thought(thought)

    preoc = (data.get("preoccupation") or "").strip()
    if preoc:
        db.upsert_core_block("preoccupation", preoc)

    # Prune old episodes and thoughts (config-driven retention).
    from . import config as cfg
    from . import lexicon_extractor
    episode_keep_days = int(cfg.get("episodes.prune_older_than_days", 30))
    thought_keep_days = int(cfg.get("character_thoughts.prune_older_than_days", 30))
    pruned = db.prune_episodes_older_than_days(episode_keep_days)
    pruned_thoughts = db.prune_thoughts_older_than_days(thought_keep_days)

    # Lexicon extraction — find repeated user phrases and promote.
    promoted = 0
    try:
        promoted = lexicon_extractor.extract_and_promote(lookback_days=7)
    except Exception:
        logger.exception("lexicon extractor failed (non-fatal)")

    # Apply slow weight decay + prune entries that fell below the floor.
    lex_decayed, lex_pruned = (0, 0)
    try:
        lex_decayed, lex_pruned = db.lexicon_decay_and_prune(
            decay_per_call=float(cfg.get("lexicon.decay_per_reflection", 0.02)),
            min_weight=float(cfg.get("lexicon.min_weight_floor", 0.05)),
        )
    except Exception:
        logger.exception("lexicon decay/prune failed (non-fatal)")

    # Open-loop decay sweep — drop tasks past their half-life or mention cap.
    decayed = (0, 0)
    try:
        decayed = db.task_decay_sweep(
            half_life_by_importance=dict(cfg.get("open_loops.decay_half_life_days") or {}),
            default_half_life_days=int(cfg.get("open_loops.default_half_life_days", 14)),
            max_mentions_before_drop=int(cfg.get("open_loops.max_mentions_before_drop", 2)),
        )
    except Exception:
        logger.exception("task decay sweep failed (non-fatal)")

    # Prune old noticings (same retention policy as thoughts).
    try:
        db.prune_noticings_older_than_days(thought_keep_days)
    except Exception:
        logger.exception("noticings prune failed (non-fatal)")

    # Phase 7: persona-drift telemetry — read the rolling window + prune.
    drift_avg = None
    drift_below = 0
    try:
        drift_avg = db.drift_recent_avg(window_days=7)
        drift_below = db.drift_recent_below_threshold(
            threshold=float(cfg.get("drift_telemetry.drift_threshold", 0.5)),
            window_days=7,
        )
        db.prune_drift_older_than_days(
            int(cfg.get("drift_telemetry.prune_older_than_days", 30))
        )
    except Exception:
        logger.exception("drift telemetry read/prune failed (non-fatal)")

    # If drift average is materially below 0.7 OR there are several below-threshold
    # samples in the window, surface a private-diary entry so Hikari knows she's
    # slipping. The thought is written to character_thoughts (never injected to
    # the user) — daily reflection sees it on the next cycle and the user never
    # sees the literal score.
    drift_flagged = (
        drift_avg is not None and drift_avg < 0.7
    ) or drift_below >= 3
    if drift_flagged:
        try:
            db.append_thought(
                f"drift check (last 7d): avg={drift_avg!r}, "
                f"below-threshold={drift_below}. tighten up. "
                "remember the anchors: i don't need anyone, "
                "needing to be liked is embarrassing, attention is the only "
                "thing in ML that makes sense."
            )
        except Exception:
            logger.exception("drift thought write failed (non-fatal)")

    logger.info(
        "reflection done: applied=%d thought=%s preoc=%s "
        "pruned_episodes=%d pruned_thoughts=%d lexicon_new=%d "
        "lexicon_decayed=%d lexicon_pruned=%d "
        "tasks_decayed=%d tasks_over_mentioned=%d "
        "observations=%d noticings=%d",
        applied, bool(thought), bool(preoc), pruned, pruned_thoughts, promoted,
        lex_decayed, lex_pruned,
        decayed[0], decayed[1],
        obs_written, noticings_written,
    )

    # Phase 8: morning dispatch. Write a small markdown summary to the wiki
    # so the user has observability without grepping logs. Best-effort —
    # failures never block the reflection write path.
    try:
        _write_morning_dispatch(
            today=date.today(),
            drift_avg=drift_avg,
            drift_below=drift_below,
        )
    except Exception:
        logger.exception("morning_dispatch write failed (non-fatal)")

    return (
        applied > 0 or bool(thought) or bool(preoc) or promoted > 0
        or obs_written > 0 or noticings_written > 0 or peer_updated
    )


def _write_morning_dispatch(
    today: date,
    drift_avg: float | None,
    drift_below: int,
) -> Path | None:
    """Phase 8: emit ``morning_dispatch_<date>.md`` to the wiki.

    Sections:
      - yesterday's message count
      - drift average + below-threshold count
      - top 3 lexicon promotions
      - new noticings in the last 24h
      - open loops with ages
      - drift-vs-feedback divergence (when D-3 data is available)

    Idempotent: re-running on the same date overwrites the file. Returns the
    written path or None if disabled / unwritable.
    """
    from . import config as cfg

    try:
        from tools.wiki import VAULT_ROOT
    except Exception:
        logger.exception("morning_dispatch: cannot import VAULT_ROOT")
        return None

    base = VAULT_ROOT / "projects" / "hikari-agent" / "morning_dispatch"
    try:
        base.mkdir(parents=True, exist_ok=True)
    except OSError:
        logger.exception("morning_dispatch: mkdir failed for %s", base)
        return None

    fname = f"morning_dispatch_{today.isoformat()}.md"
    target = base / fname

    # Yesterday's window: prior 24h to today's 00:00 local.
    today_dt = datetime(today.year, today.month, today.day)
    yesterday_dt = today_dt - timedelta(days=1)
    yesterday_iso = yesterday_dt.isoformat()
    today_iso = today_dt.isoformat()

    msg_count = _count_messages_between(yesterday_iso, today_iso)
    lex_top = _top_lexicon(limit=3)
    new_noticings = _noticings_since(yesterday_iso, limit=10)
    open_loops = _open_loops_with_ages()
    feedback = _drift_vs_feedback(cfg=cfg)

    lines: list[str] = []
    lines.append(f"# Morning dispatch — {today.isoformat()}")
    lines.append("")
    lines.append("## traffic")
    lines.append(f"- messages in the last 24h: **{msg_count}**")
    lines.append("")
    lines.append("## persona drift (7d window)")
    if drift_avg is None:
        lines.append("- no samples this week.")
    else:
        flag = " ⚠️" if drift_avg < 0.7 else ""
        lines.append(f"- average score: **{drift_avg:.2f}**{flag}")
        lines.append(f"- below-threshold samples: **{drift_below}**")
    lines.append("")
    lines.append("## lexicon top")
    if not lex_top:
        lines.append("- (nothing promoted yet.)")
    else:
        for row in lex_top:
            phrase = str(row.get("phrase") or "")
            weight = float(row.get("weight") or 0.0)
            source = str(row.get("source") or "")
            lines.append(f"- `{phrase}` (weight={weight:.2f}, source={source})")
    lines.append("")
    lines.append("## new noticings (last 24h)")
    if not new_noticings:
        lines.append("- (none.)")
    else:
        for n in new_noticings:
            lines.append(f"- [{n.get('signal')}] {n.get('summary')}")
    lines.append("")
    lines.append("## open loops")
    if not open_loops:
        lines.append("- (clean board.)")
    else:
        for loop in open_loops:
            age = loop.get("_age_days")
            age_str = f" ({age}d old)" if age is not None else ""
            lines.append(f"- #{loop.get('id')}{age_str} — {loop.get('subject')}")
    lines.append("")
    lines.append("## ground-truth feedback")
    if feedback is None:
        lines.append("- (no 👍/👎 reactions logged yet.)")
    else:
        lines.append(
            f"- agree={feedback.get('agree', 0)}, "
            f"disagree={feedback.get('disagree', 0)}"
        )
        examples = feedback.get("examples") or []
        if examples:
            lines.append("- recent divergences:")
            for ex in examples[:3]:
                lines.append(f"  - {ex}")

    try:
        target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError:
        logger.exception("morning_dispatch: write failed for %s", target)
        return None
    logger.info("morning_dispatch: wrote %s", target)
    return target


def _count_messages_between(start_iso: str, end_iso: str) -> int:
    try:
        with db._conn() as c:
            row = c.execute(
                "SELECT COUNT(*) AS n FROM messages WHERE ts >= ? AND ts < ?",
                (start_iso, end_iso),
            ).fetchone()
        return int(row["n"]) if row else 0
    except Exception:
        logger.exception("morning_dispatch: msg count failed")
        return 0


def _top_lexicon(limit: int) -> list[dict]:
    try:
        return list(db.lexicon_top(limit=limit))
    except Exception:
        logger.exception("morning_dispatch: lexicon_top failed")
        return []


def _noticings_since(since_iso: str, limit: int) -> list[dict]:
    """Most-recent noticings created since ``since_iso``, up to ``limit``."""
    try:
        with db._conn() as c:
            rows = c.execute(
                "SELECT signal, summary, created_at FROM noticings "
                "WHERE created_at >= ? ORDER BY created_at DESC LIMIT ?",
                (since_iso, limit),
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        logger.exception("morning_dispatch: noticings query failed")
        return []


def _open_loops_with_ages() -> list[dict]:
    try:
        loops = list(db.open_tasks())
    except Exception:
        logger.exception("morning_dispatch: open_tasks failed")
        return []
    now = datetime.now(UTC)
    out: list[dict] = []
    for loop in loops:
        item = dict(loop)
        created = item.get("created_at")
        if created:
            try:
                ts = datetime.fromisoformat(str(created))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=UTC)
                item["_age_days"] = max(0, (now - ts).days)
            except (ValueError, TypeError):
                item["_age_days"] = None
        else:
            item["_age_days"] = None
        out.append(item)
    return out


def _drift_vs_feedback(cfg) -> dict | None:
    """Phase 8 / D-3 hook: compare the drift judge's recent scores against
    user 👍/👎 reactions when the helper exists. Returns None when D-3 hasn't
    landed yet."""
    helper = getattr(db, "feedback_compare_to_drift", None)
    if helper is None:
        return None
    try:
        return helper(window_days=int(cfg.get("drift_telemetry.window_days", 7)))
    except Exception:
        logger.exception("morning_dispatch: feedback compare failed")
        return None


async def maybe_run_session_consolidation() -> None:
    """If the rolling message log has accumulated enough since the last episode,
    summarize it into an episode. Lightweight — runs every few minutes."""
    msgs = db.recent_messages(limit=40)
    if len(msgs) < 6:
        return

    last_ep = db.recent_episodes(limit=1)
    if last_ep and last_ep[0]["date"] == date.today().isoformat():
        # already have a today episode — only re-summarize if new content since
        return

    transcript = "\n".join(f"{m['role'].upper()}: {m['content']}" for m in msgs[-20:])
    prompt = (
        "Summarize this Hikari conversation in 2-4 sentences. Capture: what was "
        "discussed, emotional tone, anything notable. Output ONLY the summary text.\n\n"
        f"{transcript}"
    )
    try:
        summary = (await run_reflection_call(prompt)).strip()
    except Exception:
        logger.exception("session consolidation LLM failed")
        return
    if summary:
        ep_id = db.insert_episode(date.today().isoformat(), summary, importance=5)
        try:
            emb = await embeddings.aembed(summary)
            db.set_vec_episode(ep_id, emb)
        except Exception:  # noqa: BLE001
            logger.exception("episode embedding failed for id=%s", ep_id)
        logger.info("episode for %s recorded (%d chars)", date.today(), len(summary))


async def _embed_fact(fact_id: int, subject: str, predicate: str, object_: str) -> None:
    try:
        emb = await embeddings.aembed(f"{subject} {predicate} {object_}")
        db.set_vec_fact(fact_id, emb)
    except Exception:  # noqa: BLE001
        logger.exception("fact embedding failed for id=%s", fact_id)


async def reflection_after_task(task_id: str) -> None:
    """Per-hard-task reflection. Triggered by background_listener on completion
    when the heuristic (duration/length/tool-uses) matches.

    Reads the task meta + result, asks Sonnet to extract atomic facts + open
    loops, writes them into memory. Output never reaches the user.
    """
    row = db.bg_task_get(task_id)
    if not row:
        logger.warning("reflection_after_task: unknown task_id=%s", task_id)
        return
    if row["status"] != "done":
        return

    summary = (row.get("result_summary") or "").strip()
    if not summary:
        return

    prompt = (
        "You're doing a quick post-task reflection on a dispatched Claude Code session. "
        "Read the task + result below. Extract anything worth remembering long-term as "
        "atomic facts (subject/predicate/object/importance 1-10). Note any open loops "
        "or follow-ups the user might want tracked. Output ONLY valid YAML in this shape:\n\n"
        "facts:\n"
        "  - {subject: '', predicate: '', object: '', importance: 5}\n"
        "open_loops:\n"
        "  - one-line task description\n"
        "thought: |\n"
        "  [1-2 sentences in first person about what stood out — Hikari's private voice]\n\n"
        "Rules: facts only if they're stable + cross-session-useful. "
        "If nothing worth keeping, use empty lists.\n\n"
        f"## task\n{row['prompt']}\n\n"
        f"## repo\n{row.get('meta_json') or '{}'}\n\n"
        f"## result summary ({row.get('cost_usd') or 0:.2f} usd, "
        f"{row.get('tool_use_count') or 0} tool uses)\n{summary[:6000]}"
    )

    try:
        raw = await run_reflection_call(prompt)
    except Exception:
        logger.exception("reflection_after_task: LLM call failed for %s", task_id)
        return

    try:
        data = yaml.safe_load(_strip_fences(raw)) or {}
    except yaml.YAMLError:
        logger.warning("reflection_after_task: invalid YAML; got %r", raw[:200])
        return

    written = 0
    for f in data.get("facts") or []:
        try:
            subj = str(f["subject"]).strip()
            pred = str(f["predicate"]).strip()
            obj = str(f["object"]).strip()
            if not (subj and pred and obj):
                continue
            fact_id = db.insert_fact(
                subject=subj, predicate=pred, object_=obj,
                importance=int(f.get("importance") or 5),
                confidence=0.8,
            )
            await _embed_fact(fact_id, subj, pred, obj)
            written += 1
        except (KeyError, ValueError, TypeError):
            continue

    for loop in data.get("open_loops") or []:
        loop_text = str(loop).strip()
        if loop_text:
            db.create_task(loop_text)

    thought = (data.get("thought") or "").strip()
    if thought:
        db.append_thought(f"[post-task {task_id[:8]}] {thought}")

    logger.info("reflection_after_task %s: %d facts, %d loops, thought=%s",
                task_id[:8], written, len(data.get("open_loops") or []), bool(thought))
