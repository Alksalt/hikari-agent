"""Telegram bridge. Receives messages, locks to OWNER_TELEGRAM_ID, dispatches to
the agent runtime, drains the photo outbox after each turn, starts background jobs.

UX choreography (typing delay, false-start, ignore mechanic) lives in bridge_ux.py.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from storage import db
from tools import approvals as approval_tools
from tools import dispatch as dispatch_tools
from tools import location as location_tool
from tools import voice as voice_tool
from tools.photos import OUTBOX as PHOTO_OUTBOX

from . import affect as affect_mod
from . import config as cfg
from . import reactions as reactions_mod
from .background_listener import (
    listener_loop,
    recover_deferred_approvals,
    recover_running_tasks,
)
from .bridge_ux import (
    compute_typing_delay,
    false_start_pause_sec,
    false_start_resume_sec,
    should_false_start,
    should_ignore,
)
from .log_scrub import install_root_filter
from .politeness_gate import is_rude, random_refusal
from .post_filter import filter_outgoing
from .runtime import REPO_ROOT, owner_id, respond
from .scheduler import build_scheduler

logger = logging.getLogger(__name__)

USER_PHOTO_DIR = REPO_ROOT / "data" / "user_photos"


def _user_voice_dir() -> Path:
    """Resolved per-call so config changes (or test isolation) win without a reload."""
    rel = str(cfg.get("voice.save_dir") or "data/user_voice")
    return REPO_ROOT / rel


def _mood() -> str:
    return (db.get_core_block("mood_today") or "focused").strip().lower() or "focused"


async def _drain_photo_outbox(bot, chat_id: int) -> int:
    """Send and delete every file in the photo outbox. Returns count sent."""
    if not PHOTO_OUTBOX.exists():
        return 0
    sent = 0
    for path in sorted(PHOTO_OUTBOX.iterdir()):
        if not path.is_file() or path.suffix.lower() not in (".png", ".jpg", ".jpeg"):
            continue
        try:
            with path.open("rb") as f:
                await bot.send_photo(chat_id=chat_id, photo=f)
            sent += 1
        except Exception:
            logger.exception("failed to send photo %s", path.name)
            continue
        try:
            path.unlink()
        except OSError:
            pass
    return sent


async def _send_with_choreography(bot, message, reply_text: str) -> None:
    """Run outgoing filter, then type-indicator + delay, then send.

    The post_filter pass catches Claude's default assistant patter and obvious
    sycophancy collapses. Short safety-voice replies get swapped for an in-voice
    short phrase. Longer triggers are logged to character_thoughts (Hikari's
    diary) for the daily reflection to notice; the original text still ships
    until ``refusal_filter.enable_llm_rewrite`` is turned on.
    """
    chat_id = message.chat_id
    mood = _mood()

    filtered = filter_outgoing(reply_text)
    text_to_send = filtered.text
    if filtered.refusal_short_replaced:
        logger.info("post_filter: replaced safety-voice reply with %r", text_to_send)
        db.append_thought(
            "post_filter: short-replaced safety-voice leak. "
            f"hits={filtered.refusal_hits[:3]}"
        )
    elif filtered.needs_llm_rewrite:
        # Detected but not auto-rewritten (rewrite path opt-in via config). Log
        # so daily reflection notices the pattern.
        db.append_thought(
            "post_filter triggered (not rewritten): "
            f"refusal_hits={filtered.refusal_hits[:3]} "
            f"sycophancy={filtered.sycophancy_triggered} "
            f"anchor_violations={filtered.sycophancy_violations[:2]}"
        )

    await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    delay = compute_typing_delay(text_to_send, mood)

    if should_false_start(text_to_send):
        # Half the delay, brief gap, then resume typing for the rest.
        await asyncio.sleep(max(0.5, delay / 2))
        # Telegram has no "stop typing" — the indicator decays after a few seconds.
        await asyncio.sleep(false_start_pause_sec())
        await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
        await asyncio.sleep(false_start_resume_sec())
    else:
        await asyncio.sleep(delay)

    await message.reply_text(text_to_send)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat = update.effective_chat
    message = update.message
    if not user or not chat or not message or not message.text:
        return

    if user.id != owner_id():
        logger.warning("denied non-owner user_id=%s", user.id)
        return

    # Approval pre-check: if there's a pending approval, see if this message resolves it.
    # If so, consume the message (don't route to the agent).
    try:
        consumed = await approval_tools.resolve_pending_approval(chat.id, message.text)
    except Exception:
        logger.exception("approval resolver failed; continuing to agent")
        consumed = False
    if consumed:
        return

    # Politeness gate — refuse rude turns in character, no LLM call. Cheap
    # deterministic check; misses get caught by the CLAUDE.md persona rule.
    rude, matched = is_rude(message.text)
    if rude:
        refusal = random_refusal()
        logger.info("politeness_gate: rude pattern matched=%r → refused", matched)
        db.append_thought(
            f"refused — user was rude. matched={matched!r}. sent={refusal!r}"
        )
        await message.reply_text(refusal)
        return

    # Emotional half-life — scan inbound for heavy-moment signals. Sets
    # runtime_state['affect_state'] so the next hook injection knows to soften.
    try:
        affect_mod.scan_inbound(message.text)
    except Exception:
        logger.exception("affect scan failed (non-fatal)")

    # Probabilistic reaction — fires occasionally as a non-verbal nod.
    try:
        await reactions_mod.maybe_react(context.bot, chat.id, message.message_id)
    except Exception:
        logger.exception("reactions: maybe_react failed (non-fatal)")

    # Ignore mechanic — roll first, before any agent work
    mood = _mood()
    ignore, action_line = should_ignore(mood)
    if ignore and action_line:
        await message.reply_text(action_line)
        return

    try:
        reply = await respond(message.text)
    except Exception:
        logger.exception("agent failed for: %r", message.text[:80])
        await message.reply_text("(brain hit a wall. try again.)")
        return

    if reply:
        await _send_with_choreography(context.bot, message, reply)
    n = await _drain_photo_outbox(context.bot, chat.id)
    if n and not reply:
        logger.info("sent %d photo(s) with no accompanying text", n)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Inbound user photo — save to disk, prompt agent with path so it can Read,
    then record an episode tagged with the reply summary so future callbacks work
    ('how's the plant?')."""
    user = update.effective_user
    chat = update.effective_chat
    message = update.message
    if not user or not chat or not message or not message.photo:
        return
    if user.id != owner_id():
        return

    USER_PHOTO_DIR.mkdir(parents=True, exist_ok=True)
    largest = message.photo[-1]
    f = await largest.get_file()
    fname = f"{int(time.time() * 1000)}.jpg"
    rel = Path("data") / "user_photos" / fname
    abs_path = USER_PHOTO_DIR / fname
    try:
        await f.download_to_drive(custom_path=str(abs_path))
    except Exception:
        logger.exception("failed to download user photo")
        await message.reply_text("(couldn't download that. try again?)")
        return

    user_caption = (message.caption or "").strip()
    # Run the same inbound gates as text messages on the caption (if any).
    if user_caption:
        rude, matched = is_rude(user_caption)
        if rude:
            refusal = random_refusal()
            logger.info(
                "politeness_gate: rude photo caption matched=%r → refused", matched
            )
            db.append_thought(
                f"refused — rude photo caption. matched={matched!r}. sent={refusal!r}"
            )
            await message.reply_text(refusal)
            return
        try:
            affect_mod.scan_inbound(user_caption)
        except Exception:
            logger.exception("affect scan on caption failed (non-fatal)")
    prompt = (
        f"the user sent you a photo. it's saved at {rel}. "
        f"use the Read tool to look at it before replying. "
        f"caption (if any): {user_caption!r}.\n\n"
        f"react in your voice — short. not effusive. denial layer on. "
        f"after you reply, if there's anything photo-worth-remembering "
        f"(an object, a setting, a mood worth a future callback), "
        f"call mcp__hikari_memory__remember with a tight fact "
        f"(subject='photo', predicate='showed', object='<thing>')."
    )
    try:
        reply = await respond(prompt)
    except Exception:
        logger.exception("agent failed on inbound photo")
        await message.reply_text("(brain hit a wall on that photo.)")
        return
    # Record an episode so we can callback later ("how's the plant?").
    try:
        from datetime import date as _date
        summary = (
            f"user sent photo at {rel}. user_caption: {user_caption!r}. "
            f"my reaction: {reply[:200]!r}"
        )
        db.insert_episode(_date.today().isoformat(), summary, importance=4)
    except Exception:
        logger.exception("photo episode write failed (non-fatal)")
    if reply:
        await _send_with_choreography(context.bot, message, reply)
    await _drain_photo_outbox(context.bot, chat.id)


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Inbound user voice note — download .ogg, transcribe via Whisper, then
    react in voice. Parity with ``handle_photo``: politeness gate + affect scan
    run on the transcript; failure to transcribe surfaces the configured
    graceful-failure reply instead of crashing the handler.
    """
    user = update.effective_user
    chat = update.effective_chat
    message = update.message
    if not user or not chat or not message or not message.voice:
        return
    if user.id != owner_id():
        return

    voice_dir = _user_voice_dir()
    voice_dir.mkdir(parents=True, exist_ok=True)
    f = await message.voice.get_file()
    fname = f"{int(time.time() * 1000)}.ogg"
    abs_path = voice_dir / fname
    try:
        await f.download_to_drive(custom_path=str(abs_path))
    except Exception:
        logger.exception("failed to download user voice note")
        await message.reply_text("(couldn't download that. try again?)")
        return

    duration_sec = float(getattr(message.voice, "duration", 0) or 0)
    max_duration = float(cfg.get("voice.max_duration_sec", 300))
    graceful_reply = str(
        cfg.get("voice.graceful_failure_reply")
        or "(can't transcribe right now. type it instead.)"
    )
    if duration_sec > max_duration:
        logger.info(
            "voice note rejected: duration %.1fs > max %.1fs", duration_sec, max_duration
        )
        await message.reply_text(graceful_reply)
        return

    try:
        transcript = await voice_tool.transcribe_voice(abs_path)
    except voice_tool.VoiceTranscribeError as e:
        logger.info("voice transcription failed: %s", e)
        await message.reply_text(graceful_reply)
        return
    except Exception:
        logger.exception("voice transcription crashed unexpectedly")
        await message.reply_text(graceful_reply)
        return

    rude, matched = is_rude(transcript)
    if rude:
        refusal = random_refusal()
        logger.info(
            "politeness_gate: rude voice transcript matched=%r → refused", matched
        )
        db.append_thought(
            f"refused — rude voice transcript. matched={matched!r}. sent={refusal!r}"
        )
        await message.reply_text(refusal)
        return

    try:
        affect_mod.scan_inbound(transcript)
    except Exception:
        logger.exception("affect scan on transcript failed (non-fatal)")

    prefix = str(cfg.get("voice.transcript_prefix") or "[voice note]")
    prompt = (
        f"the user sent you a voice note ({duration_sec:.0f}s). "
        f"{prefix}: {transcript!r}\n\n"
        f"react in your voice — short. denial layer on. you can comment on "
        f"how they sounded (tired, rushed, lit up) if it's there."
    )
    try:
        reply = await respond(prompt)
    except Exception:
        logger.exception("agent failed on inbound voice note")
        await message.reply_text("(brain hit a wall on that one.)")
        return

    try:
        from datetime import date as _date
        summary = (
            f"user sent voice note ({duration_sec:.0f}s). "
            f"transcript: {transcript!r}. my reaction: {reply[:200]!r}"
        )
        db.insert_episode(_date.today().isoformat(), summary, importance=4)
    except Exception:
        logger.exception("voice episode write failed (non-fatal)")

    if reply:
        await _send_with_choreography(context.bot, message, reply)
    await _drain_photo_outbox(context.bot, chat.id)


async def handle_location(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Inbound user location share — reverse-geocode + fetch weather, store as
    transient fact for the hook to inject. We deliberately do NOT respond about
    the location on this turn; the hook respects ``defer_callback_turns`` so
    the first mention comes from a later, natural opening."""
    user = update.effective_user
    chat = update.effective_chat
    message = update.message
    if not user or not chat or not message or not message.location:
        return
    if user.id != owner_id():
        return
    loc = message.location
    state = await location_tool.record_share(
        lat=float(loc.latitude), lon=float(loc.longitude),
    )
    label = state.get("label") if state else None
    logger.info("location: recorded share lat=%.4f lon=%.4f label=%s",
                loc.latitude, loc.longitude, label)
    # Brief in-voice ack — pool from config so it's tuneable. No location
    # specifics here; the hook surfaces them later (defer_callback_turns).
    import random as _random

    ack_pool = cfg.get("location.ack_pool") or ["noted."]
    ack = _random.choice(ack_pool)
    try:
        await message.reply_text(ack)
    except Exception:
        logger.exception("location ack send failed (non-fatal)")


# ---------- commands ----------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Route /start through the agent so she responds in-character (not 'Welcome!')."""
    user = update.effective_user
    chat = update.effective_chat
    message = update.message
    if not user or not chat or not message or user.id != owner_id():
        return
    try:
        reply = await respond(
            "[the user just opened the chat with /start. react in your voice — "
            "short, denial layer on. don't welcome them like a service.]"
        )
    except Exception:
        logger.exception("agent failed on /start")
        await message.reply_text("(brain hit a wall. try again.)")
        return
    if reply:
        await _send_with_choreography(context.bot, message, reply)
    await _drain_photo_outbox(context.bot, chat.id)


async def cmd_silence(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    message = update.message
    if not user or not message or user.id != owner_id():
        return
    minutes = int(cfg.get("silence.default_minutes", 120))
    if context.args:
        try:
            minutes = max(1, int(context.args[0]))
        except (ValueError, IndexError):
            pass
    until = datetime.now(UTC) + timedelta(minutes=minutes)
    db.runtime_set("silence_until", until.isoformat())
    await message.reply_text(f"ok. quiet for {minutes} minutes. don't make me regret it.")


async def cmd_unsilence(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    message = update.message
    if not user or not message or user.id != owner_id():
        return
    db.runtime_set("silence_until", None)
    await message.reply_text("fine. you can hear me again.")


async def cmd_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List running + recent background tasks."""
    user = update.effective_user
    message = update.message
    if not user or not message or user.id != owner_id():
        return
    running = db.bg_tasks_running()
    recent = db.bg_tasks_recent(chat_id=user.id, limit=5)
    lines: list[str] = []
    if running:
        lines.append(f"running ({len(running)}):")
        for r in running:
            lines.append(f"  {r['task_id'][:8]} — {r['status']} — {r['prompt'][:60]}")
    else:
        lines.append("nothing running.")
    if recent:
        lines.append("")
        lines.append("recent:")
        for r in recent:
            cost = r.get("cost_usd") or 0.0
            lines.append(
                f"  {r['task_id'][:8]} [{r['status']}] ${cost:.2f} — {r['prompt'][:50]}"
            )
    await message.reply_text("\n".join(lines))


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Cancel a background task by id-prefix. Note: in-process asyncio.Task cancellation
    is not straightforward — for v1 this only marks the row cancelled. The nested SDK
    client will keep running until its turn cap or budget cap; future work to actually
    interrupt it."""
    user = update.effective_user
    message = update.message
    if not user or not message or user.id != owner_id():
        return
    if not context.args:
        await message.reply_text("usage: /cancel <task_id_prefix>")
        return
    prefix = context.args[0].strip().lower()
    running = db.bg_tasks_running()
    matches = [r for r in running if r["task_id"].lower().startswith(prefix)]
    if not matches:
        await message.reply_text(f"no running task starting with {prefix!r}.")
        return
    if len(matches) > 1:
        await message.reply_text(f"ambiguous; {len(matches)} match. be more specific.")
        return
    target = matches[0]
    db.bg_task_update(target["task_id"], status="cancelled",
                      completed_at=db._now(),
                      result_summary="cancelled by user")
    await message.reply_text(
        f"marked {target['task_id'][:8]} cancelled. "
        f"(it'll finish its current turn before stopping for real.)"
    )


async def cmd_cost(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Daily cost summary."""
    user = update.effective_user
    message = update.message
    if not user or not message or user.id != owner_id():
        return
    today_iso = datetime.now(UTC).date().isoformat()
    with db._conn() as c:
        row = c.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) AS bg_cost, COUNT(*) AS n "
            "FROM background_tasks WHERE substr(started_at, 1, 10) = ?",
            (today_iso,),
        ).fetchone()
    from tools import budget
    bg_cost = float(row["bg_cost"] or 0.0)
    bg_n = int(row["n"] or 0)
    chat_today = float(db.runtime_get("cost_today") or 0.0)
    total = bg_cost + chat_today
    cap = budget.daily_cap()
    await message.reply_text(
        f"today: ~${total:.2f} (chat ${chat_today:.2f} + {bg_n} dispatched ${bg_cost:.2f}). "
        f"cap is ${cap:.2f}."
    )


def build_application() -> Application:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise ValueError("TELEGRAM_BOT_TOKEN not set in environment")
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("silence", cmd_silence))
    app.add_handler(CommandHandler("unsilence", cmd_unsilence))
    app.add_handler(CommandHandler("tasks", cmd_tasks))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("cost", cmd_cost))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.LOCATION, handle_location))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    return app


def main() -> None:
    load_dotenv()
    logging.basicConfig(
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        level=logging.INFO,
    )
    # Install secret-redacting + canary-leak filter on the root logger so
    # secrets never hit stdout/files.
    install_root_filter()
    # Lazy/opt-in observability — no-op unless HIKARI_LOGFIRE_ENABLED is set
    # AND the logfire package is installed.
    from . import observability
    observability.init_logfire()
    _ = owner_id()
    if (REPO_ROOT / ".mcp.json").exists():
        try:
            import json
            mcp = json.loads((REPO_ROOT / ".mcp.json").read_text())
            servers = mcp.get("mcpServers", {})
            if "google_workspace" in servers and not os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON"):
                logger.warning(
                    "google_workspace MCP is registered in .mcp.json but "
                    "GOOGLE_SERVICE_ACCOUNT_JSON is not set — the server will fail to start."
                )
        except Exception:  # noqa: BLE001
            pass

    app = build_application()

    async def post_init(application: Application) -> None:
        async def send_text(text: str) -> None:
            # Run outgoing filter so proactive heartbeats can't leak
            # safety-voice patter either.
            filtered = filter_outgoing(text)
            to_send = filtered.text
            if filtered.refusal_short_replaced:
                db.append_thought(
                    "proactive: short-replaced safety-voice. "
                    f"hits={filtered.refusal_hits[:3]}"
                )
            await application.bot.send_message(chat_id=owner_id(), text=to_send)

        scheduler = build_scheduler(send_text)
        scheduler.start()
        logger.info("scheduler started: %s", [j.id for j in scheduler.get_jobs()])

        # Wire owner chat into dispatch tool so it can resolve where to send results.
        dispatch_tools.set_owner_chat_id(owner_id())
        # Wire bot ref into approvals so tools can send approval prompts out-of-band.
        approval_tools.set_bot(application.bot)

        # Tell the user about any tasks that were running mid-restart.
        await recover_running_tasks(application.bot)
        # Phase 6: resurface any deferred approval that was pending pre-restart.
        await recover_deferred_approvals(application.bot)

        # Start the long-running dispatch event listener.
        asyncio.create_task(listener_loop(application.bot))
        logger.info("dispatch listener task started")

    app.post_init = post_init
    logger.info("starting hikari-agent (full stack)")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
