"""Telegram bridge. Receives messages, locks to OWNER_TELEGRAM_ID, dispatches to
the agent runtime, drains the photo outbox after each turn, starts background jobs.

UX choreography (typing delay, false-start, ignore mechanic) lives in bridge_ux.py.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv
from telegram import MessageReactionUpdated, ReactionTypeEmoji, Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    MessageReactionHandler,
    filters,
)

from storage import db
from tools import approvals as approval_tools
from tools import dispatch as dispatch_tools
from tools import location as location_tool
from tools import voice as voice_tool
from tools.photos import OUTBOX as PHOTO_OUTBOX

from . import affect as affect_mod
from . import belief_frame as belief_mod
from . import config as cfg
from . import drift_judge as drift_mod
from . import nonverbal as nonverbal_mod
from . import reactions as reactions_mod
from . import stickers as stickers_mod
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
from . import post_filter
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


def _typing_refresh_sec() -> float:
    return float(cfg.get("typing.refresh_sec", 4.0))


class TypingHeartbeat:
    """Phase 8 — keep the Telegram typing indicator alive while the agent is
    working. Starts immediately on entry (before any LLM/STT call), refreshes
    every ``typing.refresh_sec`` seconds, and stops cleanly on exit. Used as
    an async context manager so the typing state always cleans up even when
    the inner block raises.

    Pattern:
        async with TypingHeartbeat(bot, chat_id) as hb:
            reply = await respond(user_text)
        # hb tells _send_with_choreography how long the user already waited.
    """

    def __init__(self, bot, chat_id: int):
        self._bot = bot
        self._chat_id = chat_id
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._started_at: float = 0.0

    @property
    def elapsed(self) -> float:
        return max(0.0, time.monotonic() - self._started_at) if self._started_at else 0.0

    async def __aenter__(self) -> "TypingHeartbeat":
        self._started_at = time.monotonic()
        # Fire one ChatAction.TYPING immediately so the user sees it within
        # ~100ms of sending — no waiting for the agent to start.
        try:
            await self._bot.send_chat_action(
                chat_id=self._chat_id, action=ChatAction.TYPING,
            )
        except Exception:
            logger.exception("typing heartbeat: initial send_chat_action failed")
        self._task = asyncio.create_task(self._loop())
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self._stop.set()
        if self._task is not None:
            # Cancel proactively so we don't wait up to refresh_sec for the
            # loop to notice the stop event on its own timer tick. The loop
            # tolerates CancelledError silently — see _loop body.
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                # CancelledError is the expected path; everything else gets
                # logged inside the loop itself.
                pass
        return False

    async def _loop(self) -> None:
        refresh = _typing_refresh_sec()
        try:
            while not self._stop.is_set():
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=refresh)
                    return  # stop was set during the wait
                except asyncio.TimeoutError:
                    pass
                try:
                    await self._bot.send_chat_action(
                        chat_id=self._chat_id, action=ChatAction.TYPING,
                    )
                except Exception:
                    logger.exception("typing heartbeat: refresh send failed")
        except asyncio.CancelledError:
            # __aexit__ cancelled us during cleanup. Exit silently — caller
            # already set the stop event and is shutting down.
            return


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


async def _send_with_choreography(
    bot, message, reply_text: str, elapsed_real: float = 0.0,
) -> None:
    """Run outgoing filter, then type-indicator + delay, then send.

    The post_filter pass catches Claude's default assistant patter and obvious
    sycophancy collapses. Short safety-voice replies get swapped for an in-voice
    short phrase. Longer drift triggers go through the Phase 8 bounded rewrite
    path before falling back to a deterministic short reply.

    Phase 8: the typing indicator is now held alive by ``TypingHeartbeat`` from
    the moment the user message arrives. ``elapsed_real`` is the time we've
    already spent in the agent path; if it exceeds the synthesized typing
    delay, the artificial delay is skipped so we don't stack real latency on
    top of fake latency.
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
        # Phase 8: bounded LLM rewrite. One Haiku turn, no tools. If the
        # rewrite still trips the filter, fall back to a deterministic short
        # in-voice phrase rather than shipping the drift.
        text_to_send = await post_filter.rewrite_or_fallback(
            reply_text, filtered, mood, where="bridge",
        )

    delay = compute_typing_delay(text_to_send, mood)
    remaining = max(0.0, delay - elapsed_real)

    if should_false_start(text_to_send) and remaining > 0:
        # Half the delay, brief gap, then resume typing for the rest.
        await asyncio.sleep(max(0.5, remaining / 2))
        # Telegram has no "stop typing" — the indicator decays after a few seconds.
        await asyncio.sleep(false_start_pause_sec())
        await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
        await asyncio.sleep(false_start_resume_sec())
    elif remaining > 0:
        await asyncio.sleep(remaining)

    sent_msg = await message.reply_text(text_to_send)

    # Phase 8: stamp the assistant row in `messages` with the Telegram
    # outbound message_id so 👍/👎 reactions can be joined back to the reply.
    try:
        if sent_msg is not None and getattr(sent_msg, "message_id", None):
            db.update_last_assistant_telegram_msg_id(int(sent_msg.message_id))
    except Exception:
        logger.exception(
            "post_send: failed to stamp telegram_message_id (non-fatal)"
        )

    # Outbound-counter bump + sticker gate. Bump first so the value passed in
    # reflects this just-sent reply; the sticker module reads the same shared
    # counter via storage.db.OUTBOUND_MSG_COUNTER_KEY.
    try:
        stickers_mod._bump_outbound_counter()
        outbound_counter = db.runtime_get_int(db.OUTBOUND_MSG_COUNTER_KEY, 0)
        await stickers_mod.maybe_send_sticker(bot, chat_id, outbound_counter)
    except Exception:
        logger.exception("stickers: maybe_send_sticker failed (non-fatal)")
        outbound_counter = db.runtime_get_int(db.OUTBOUND_MSG_COUNTER_KEY, 0)

    # Phase 7: drift judge — fire-and-forget Haiku sampler. Runs in a separate
    # ClaudeSDKClient (no session resume, no _RUN_LOCK) so user-send latency
    # stays zero. Sampled probabilistically + daily-capped in config.
    try:
        asyncio.create_task(
            drift_mod.maybe_judge_and_log(text_to_send, outbound_counter)
        )
    except Exception:
        logger.exception("drift_judge: maybe_judge_and_log scheduling failed")


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

    # Phase 9: non-verbal reply modes (sticker-only / reaction-only) roll
    # BEFORE respond() AND BEFORE belief-frame priming so we save both the
    # LLM cost and avoid stamping a wasted-priming thought when we short-
    # circuit (review-F5). Heuristics inside ``nonverbal.maybe_nonverbal_reply``
    # skip on questions / common substantive openers / long messages / daily
    # cap so substantive turns always reach the real reply path.
    nonverbal_kind = nonverbal_mod.maybe_nonverbal_reply(message.text, mood)
    if nonverbal_kind == "sticker":
        await nonverbal_mod.send_sticker_only(context.bot, chat.id)
        return
    if nonverbal_kind == "reaction":
        await nonverbal_mod.send_reaction_only(
            context.bot, chat.id, message.message_id,
        )
        return

    # Belief-frame guard — if the user is asserting a factual claim as their
    # belief ("i think X", "i'm pretty sure X"), prepend an adversarial
    # instruction so the recall subagent looks for contradictions instead of
    # confirmations. Mitigates Stanford-AI-Index 2026 sycophancy-under-belief.
    # Moved AFTER the non-verbal gate (Phase 9 review-F5): when a non-verbal
    # reply short-circuits the turn, this priming is never used, so don't
    # bother stamping a misleading character_thoughts entry.
    user_text = message.text
    try:
        bm_hit, bm_fragment = belief_mod.is_belief_assertion(user_text)
    except Exception:
        logger.exception("belief_frame scan failed (non-fatal)")
        bm_hit, bm_fragment = False, None
    if bm_hit and bm_fragment:
        user_text = (
            belief_mod.adversarial_prompt_suffix(bm_fragment) + "\n\n" + user_text
        )
        db.append_thought(
            f"belief-frame detected: {bm_fragment!r}. recall adversarial mode primed."
        )

    # Phase 8: start the typing heartbeat IMMEDIATELY so the user sees the
    # indicator while the agent is actually working, not after the reply is
    # already in hand.
    async with TypingHeartbeat(context.bot, chat.id) as hb:
        try:
            reply = await respond(user_text)
        except Exception:
            logger.exception("agent failed for: %r", message.text[:80])
            await message.reply_text("(brain hit a wall. try again.)")
            return

        elapsed = hb.elapsed
    if reply:
        await _send_with_choreography(
            context.bot, message, reply, elapsed_real=elapsed,
        )
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

    # Phase 8: start typing heartbeat before the download — photo handling has
    # the longest pipeline (download → Read → respond) so user feedback is
    # critical here.
    async with TypingHeartbeat(context.bot, chat.id) as hb:
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

        elapsed = hb.elapsed
    if reply:
        await _send_with_choreography(
            context.bot, message, reply, elapsed_real=elapsed,
        )
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

    # Phase 8: typing heartbeat starts before the download. Voice note path:
    # download → Whisper transcribe → respond → reply, all under one heartbeat.
    async with TypingHeartbeat(context.bot, chat.id) as hb:
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
                "voice note rejected: duration %.1fs > max %.1fs",
                duration_sec, max_duration,
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

        elapsed = hb.elapsed

    if reply:
        await _send_with_choreography(
            context.bot, message, reply, elapsed_real=elapsed,
        )
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


_STICKER_CAPTURE_MODE_KEY = "sticker_capture_mode"
_STICKER_CAPTURE_POOL_KEY = "sticker_capture_pool"


async def cmd_grab_stickers(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Phase 9 — capture file_ids of every sticker the owner sends, then
    print a YAML snippet ready to paste into config/engagement.yaml.

    Subcommands:
      /grab_stickers          — show status
      /grab_stickers start    — enter capture mode (every inbound sticker captured)
      /grab_stickers stop     — exit capture mode + print the YAML snippet
      /grab_stickers reset    — exit + drop the accumulated pool
    """
    import json

    user = update.effective_user
    message = update.message
    if not user or not message or user.id != owner_id():
        return

    arg = (context.args[0].strip().lower() if context.args else "").strip()
    on = db.runtime_get(_STICKER_CAPTURE_MODE_KEY) == "1"
    pool_json = db.runtime_get(_STICKER_CAPTURE_POOL_KEY) or "[]"
    try:
        pool = json.loads(pool_json)
        if not isinstance(pool, list):
            pool = []
    except (ValueError, TypeError):
        pool = []

    if arg == "start":
        db.runtime_set(_STICKER_CAPTURE_MODE_KEY, "1")
        # Don't clobber an existing partial pool — let user append across sessions.
        await message.reply_text(
            f"sticker capture ON. send me stickers; i'll log them. "
            f"({len(pool)} already queued.) /grab_stickers stop to finish."
        )
        return

    if arg in ("stop", "done", "finish"):
        db.runtime_set(_STICKER_CAPTURE_MODE_KEY, None)
        if not pool:
            await message.reply_text(
                "captured nothing. send stickers while capture is on first."
            )
            return
        snippet_lines = ["stickers:", "  pool:"]
        for fid in pool:
            # Telegram file_ids today are alphanumeric + _ + -, but escape
            # double quotes + backslashes defensively in case a future
            # source emits anything weirder (review-F6).
            fid_safe = str(fid).replace("\\", "\\\\").replace('"', '\\"')
            snippet_lines.append(f'    - "{fid_safe}"')
        snippet = "\n".join(snippet_lines)
        await message.reply_text(
            f"captured {len(pool)} sticker(s). paste this into "
            f"config/engagement.yaml (replace the existing `stickers.pool:`):\n\n"
            f"```\n{snippet}\n```"
        )
        # Leave the pool intact in case they want to capture more later.
        return

    if arg == "reset":
        db.runtime_set(_STICKER_CAPTURE_MODE_KEY, None)
        db.runtime_set(_STICKER_CAPTURE_POOL_KEY, None)
        await message.reply_text("sticker capture cleared.")
        return

    # No arg → status.
    state = "ON" if on else "off"
    await message.reply_text(
        f"sticker capture is {state}. {len(pool)} file_id(s) queued.\n"
        f"/grab_stickers start | stop | reset"
    )


async def handle_inbound_sticker(
    update: Update, context: ContextTypes.DEFAULT_TYPE,  # noqa: ARG001
) -> None:
    """Phase 9 — when sticker-capture mode is on, log inbound owner stickers.

    Outside capture mode, owner stickers are silently ignored (we don't have
    a conversational handler for them yet)."""
    import json

    user = update.effective_user
    message = update.message
    if not user or not message or not message.sticker:
        return
    if user.id != owner_id():
        return
    if db.runtime_get(_STICKER_CAPTURE_MODE_KEY) != "1":
        return

    pool_json = db.runtime_get(_STICKER_CAPTURE_POOL_KEY) or "[]"
    try:
        pool = json.loads(pool_json)
        if not isinstance(pool, list):
            pool = []
    except (ValueError, TypeError):
        pool = []

    file_id = message.sticker.file_id
    if file_id in pool:
        await message.reply_text(f"already have that one ({len(pool)} total).")
        return
    pool.append(file_id)
    db.runtime_set(_STICKER_CAPTURE_POOL_KEY, json.dumps(pool))
    await message.reply_text(f"captured ({len(pool)}). send more or /grab_stickers stop.")


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


_REACTION_TURN_COOLDOWN_KEY = "reaction_turn_last_at"
_REACTION_TURN_DAILY_KEY = "reaction_turn_count_day"
_REACTION_TURN_DAY_KEY = "reaction_turn_count_date"


def _reaction_turns_enabled() -> bool:
    return bool(cfg.get("reactions_as_turns.enabled", True))


def _reaction_feedback_also_replies() -> bool:
    return bool(cfg.get("reactions_as_turns.feedback_emojis_also_reply", False))


def _reaction_cooldown_sec() -> int:
    return int(cfg.get("reactions_as_turns.cooldown_sec", 90))


def _reaction_max_per_day() -> int:
    return int(cfg.get("reactions_as_turns.max_per_day", 20))


def _reaction_irritable_skip_prob() -> float:
    return float(cfg.get("reactions_as_turns.irritable_skip_probability", 0.5))


def _reaction_turn_within_cooldown() -> bool:
    last = db.runtime_get(_REACTION_TURN_COOLDOWN_KEY)
    if not last:
        return False
    try:
        last_dt = datetime.fromisoformat(last)
        if last_dt.tzinfo is None:
            last_dt = last_dt.replace(tzinfo=UTC)
    except (ValueError, TypeError):
        return False
    return (datetime.now(UTC) - last_dt).total_seconds() < _reaction_cooldown_sec()


def _reaction_turn_daily_count_bump() -> int:
    """Returns the new count after the bump. Resets to 1 on a new UTC date.

    Phase 9 review-F3: uses ``db.runtime_increment`` (atomic +1 via SQL) so
    concurrent calls don't lose a bump."""
    today = datetime.now(UTC).date().isoformat()
    last_day = db.runtime_get(_REACTION_TURN_DAY_KEY)
    if last_day != today:
        db.runtime_set(_REACTION_TURN_DAY_KEY, today)
        db.runtime_set(_REACTION_TURN_DAILY_KEY, "0")
    return db.runtime_increment(_REACTION_TURN_DAILY_KEY, 1)


def _reaction_turn_daily_count_peek() -> int:
    """Read without bumping. Returns the current day's count (or 0 if new day)."""
    today = datetime.now(UTC).date().isoformat()
    last_day = db.runtime_get(_REACTION_TURN_DAY_KEY)
    if last_day != today:
        return 0
    return db.runtime_get_int(_REACTION_TURN_DAILY_KEY, 0)


def _lookup_assistant_text_by_telegram_msg_id(telegram_msg_id: int) -> str | None:
    """Find the assistant message Hikari sent that the user just reacted to."""
    try:
        with db._conn() as c:
            row = c.execute(
                "SELECT content FROM messages WHERE telegram_message_id = ? "
                "AND role = 'assistant' ORDER BY id DESC LIMIT 1",
                (int(telegram_msg_id),),
            ).fetchone()
        return str(row["content"]) if row else None
    except Exception:
        logger.exception("lookup_assistant_text_by_telegram_msg_id failed")
        return None


async def _send_text_with_choreography(
    bot, chat_id: int, text_to_send_in: str, *, elapsed_real: float = 0.0,
) -> None:
    """Phase 9 — same choreography as ``_send_with_choreography`` but without
    threading the reply to a specific message. Used by reaction-triggered
    turns (no user text to reply to)."""
    mood = _mood()
    filtered = filter_outgoing(text_to_send_in)
    text_to_send = filtered.text
    if filtered.refusal_short_replaced:
        db.append_thought(
            "reaction-turn: short-replaced safety-voice leak. "
            f"hits={filtered.refusal_hits[:3]}"
        )
    elif filtered.needs_llm_rewrite:
        text_to_send = await post_filter.rewrite_or_fallback(
            text_to_send_in, filtered, mood, where="reaction-turn",
        )

    delay = compute_typing_delay(text_to_send, mood)
    remaining = max(0.0, delay - elapsed_real)

    # Phase 9 review-F2: match the false-start choreography from
    # ``_send_with_choreography`` so reaction-triggered replies have the same
    # human-typing rhythm as user-triggered ones. Reaction turns are usually
    # short impulsive replies — exactly where false-starts feel natural.
    if should_false_start(text_to_send) and remaining > 0:
        await asyncio.sleep(max(0.5, remaining / 2))
        await asyncio.sleep(false_start_pause_sec())
        try:
            await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
        except Exception:
            logger.exception(
                "reaction-turn: typing refresh failed (non-fatal)"
            )
        await asyncio.sleep(false_start_resume_sec())
    elif remaining > 0:
        await asyncio.sleep(remaining)

    try:
        sent = await bot.send_message(chat_id=chat_id, text=text_to_send)
    except Exception:
        logger.exception("reaction-turn: send_message failed")
        return

    try:
        if sent is not None and getattr(sent, "message_id", None):
            db.update_last_assistant_telegram_msg_id(int(sent.message_id))
    except Exception:
        logger.exception(
            "reaction-turn: telegram_message_id stamp failed (non-fatal)"
        )

    try:
        stickers_mod._bump_outbound_counter()
        outbound_counter = db.runtime_get_int(db.OUTBOUND_MSG_COUNTER_KEY, 0)
        await stickers_mod.maybe_send_sticker(bot, chat_id, outbound_counter)
    except Exception:
        logger.exception("reaction-turn: maybe_send_sticker failed (non-fatal)")
        outbound_counter = db.runtime_get_int(db.OUTBOUND_MSG_COUNTER_KEY, 0)

    try:
        asyncio.create_task(
            drift_mod.maybe_judge_and_log(text_to_send, outbound_counter)
        )
    except Exception:
        logger.exception("reaction-turn: drift_judge scheduling failed")


async def handle_message_reaction(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Phase 8/9 — owner reacted to one of Hikari's outbound messages.

    Two channels, kept separate:
      - 👍 / 👎 → recorded in ``user_feedback`` for the drift-judge
        ground-truth comparison. Silent unless
        ``reactions_as_turns.feedback_emojis_also_reply`` is set.
      - Any other emoji → triggers a Hikari turn (no typing needed). Gated by
        cooldown + daily cap + mood. The synthetic prompt includes the
        previous message text + the emoji so she can respond contextually.
    """
    rxn: MessageReactionUpdated | None = update.message_reaction
    if rxn is None:
        return
    user = rxn.user
    if user is None or user.id != owner_id():
        return

    # Telegram sends the NEW reaction set. Removal (empty new_reaction) is a
    # no-op for both channels — we don't undo previous feedback rows.
    chosen_emoji: str | None = None
    for r in rxn.new_reaction or []:
        if isinstance(r, ReactionTypeEmoji):
            chosen_emoji = r.emoji
            break
    if chosen_emoji is None:
        return

    is_feedback = chosen_emoji in ("👍", "👎")
    if is_feedback:
        rating = 1 if chosen_emoji == "👍" else -1
        try:
            db.feedback_record(int(rxn.message_id), rating)
        except Exception:
            logger.exception(
                "feedback_record failed for msg_id=%s rating=%s",
                rxn.message_id, rating,
            )
        # If feedback-also-replies is off (default), stop here.
        if not _reaction_feedback_also_replies():
            return

    if not _reaction_turns_enabled():
        return

    # Gates: cooldown / daily cap / mood.
    if _reaction_turn_within_cooldown():
        logger.info("reaction-turn: cooldown active; skipping")
        return
    if _reaction_turn_daily_count_peek() >= _reaction_max_per_day():
        logger.info("reaction-turn: daily cap reached; skipping")
        return
    mood = _mood()
    if mood == "irritable" and random.random() < _reaction_irritable_skip_prob():
        logger.info("reaction-turn: irritable mood skip")
        return

    # Look up the original message text Hikari sent. If the row is missing
    # (e.g. user reacted to an old message pre-D-3), fall back to a generic
    # prompt — better than dropping the turn entirely.
    chat = rxn.chat
    if chat is None or chat.id != owner_id():
        # Shouldn't happen for a single-user bot but defends against shared use.
        return
    prev_text = _lookup_assistant_text_by_telegram_msg_id(int(rxn.message_id))

    # Phase 9 review-F1: stamp cooldown + bump count IMMEDIATELY after the
    # gate checks pass — before the mood roll, message lookup, prompt build,
    # or respond() call. Two reactions arriving in the same asyncio tick
    # would otherwise both pass the (still-empty) cooldown/cap window.
    db.runtime_set(_REACTION_TURN_COOLDOWN_KEY, datetime.now(UTC).isoformat())
    _reaction_turn_daily_count_bump()

    if prev_text:
        # Escape braces so the synthetic prompt doesn't trip str.format
        # somewhere downstream, and truncate to keep the prompt tight.
        snippet = prev_text[:500].replace("{", "{{").replace("}", "}}")
        synthetic_prompt = (
            f"[the user reacted to your previous message with {chosen_emoji}. "
            f"they did not type anything — just the reaction. "
            f"previous message text: {snippet!r}.\n\n"
            f"reply in voice, short, as if they nudged you. it's a "
            f"non-verbal poke — respond like one. one or two lines tops.]"
        )
    else:
        synthetic_prompt = (
            f"[the user reacted with {chosen_emoji} to one of your messages "
            f"but the original text isn't in memory. react back briefly in "
            f"voice — one line.]"
        )

    bot = context.bot if context is not None else None
    if bot is None:
        logger.warning("reaction-turn: bot is None on context; cannot send")
        return

    started = time.monotonic()
    try:
        reply = await respond(synthetic_prompt)
    except Exception:
        logger.exception("reaction-turn: respond() failed")
        return
    if not reply:
        return
    elapsed = max(0.0, time.monotonic() - started)
    await _send_text_with_choreography(
        bot, chat.id, reply, elapsed_real=elapsed,
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
    # Phase 9: sticker-pack install — owner sends stickers while capture mode
    # is on; bot logs file_ids and emits a YAML snippet on /grab_stickers stop.
    app.add_handler(CommandHandler("grab_stickers", cmd_grab_stickers))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.LOCATION, handle_location))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(MessageHandler(filters.Sticker.ALL, handle_inbound_sticker))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    # Phase 8: 👍/👎 ground-truth for the drift judge. Owner-only handler;
    # writes a +1 / -1 row into user_feedback keyed by the outbound message_id.
    app.add_handler(MessageReactionHandler(handle_message_reaction))
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
            # safety-voice patter either. Phase 8: when the filter flags a
            # rewrite-worthy hit, attempt one bounded Haiku rewrite before
            # falling back to a deterministic short reply.
            filtered = filter_outgoing(text)
            to_send = filtered.text
            if filtered.refusal_short_replaced:
                db.append_thought(
                    "proactive: short-replaced safety-voice. "
                    f"hits={filtered.refusal_hits[:3]}"
                )
            elif filtered.needs_llm_rewrite:
                to_send = await post_filter.rewrite_or_fallback(
                    text, filtered, mood=_mood(), where="proactive",
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
    # Phase 8: include MESSAGE_REACTION so 👍/👎 ground-truth gets delivered.
    # Telegram excludes reactions from the default allowed_updates set.
    app.run_polling(
        drop_pending_updates=True,
        allowed_updates=[
            *Update.ALL_TYPES,
        ],
    )


if __name__ == "__main__":
    main()
