"""Composer: turns a TriggerCandidate into a Hikari-voice message via
run_visible_proactive. Per-source templates enforce payload-anchor citation.

Security: attacker-touchable payload fields (gmail subject/sender, calendar
title/organizer, drive name, notion page_title, weather alert_summary, etc.)
are wrapped via ``wrap_untrusted`` BEFORE template.format(). Without wrapping,
a malicious email subject could inject instructions into the proactive prompt
and steer Hikari's outbound voice — the lethal-trifecta playbook (untrusted
input + sensitive context + outbound channel). The SDK error guard after
``run_visible_proactive`` prevents auth-401 strings from shipping in Hikari's
voice (regression observed 2026-05-20 where a heartbeat shipped as ``Failed to
authenticate. API Error: 401 ...``).
"""
from __future__ import annotations

import logging
from string import Formatter
from typing import Any

from agents.engagement.triggers import TriggerCandidate
from agents.injection_guard import wrap_untrusted
from agents.runtime import looks_like_sdk_error, run_visible_proactive

logger = logging.getLogger(__name__)

# Per-source field → wrap source-name map. Each key is a (template_source,
# payload_field) pair; the value is the source-name passed to wrap_untrusted,
# which becomes the attribution tag inside the [UNTRUSTED CONTENT FROM TOOL …]
# header. Fields NOT listed here are treated as trusted (numerics, internal
# user-set values like reminder text are wrapped via the broad fallback set).
#
# Per Wave-3 security scope: subject / sender / title / organizer / name /
# page_title / text / alert_summary / statement are all attacker-touchable
# when they originate from external sources (email, calendar invites, drive
# file metadata, notion page titles, weather feeds, callback episodes mined
# from prior conversations that themselves may contain ingested URLs).
_UNTRUSTED_FIELDS: set[str] = {
    "subject",
    "sender",
    "title",
    "organizer",
    "name",
    "page_title",
    "text",
    "alert_summary",
    "statement",
    "body",
    "message",
    "description",
    "filename",
    "folder",
    "h1",
    "place_name",
    # Sprint B Wave 1 additions
    "finished_book",    # book_just_finished — from hikari_world (external data)
    "frustration",      # irritation_event — from hikari_world (external data)
    "snapshot_summary", # weather_mood_shift — from weather feed (external data)
    "from_location",    # just_got_home — hikari_world.location (LLM-seeded)
    "now_reading",      # book_just_finished — hikari_world.currently_reading (LLM-seeded)
}

# ---------- per-source prompt templates ----------
# Every template MUST include a payload-anchor token so guard.passes() can
# verify the model cited real data. Placeholders are filled via str.format()
# from candidate.payload — unknown keys default to "<missing>".

_TEMPLATES: dict[str, str] = {
    "wiki_new_file": """\
[proactive nudge — pattern=question, source=wiki_new_file]
the user just wrote a new wiki page. payload below.
write ONE message in your voice (lowercase, 1-3 sentences, no markdown).
RULES:
  - you MUST include the filename from payload.filename VERBATIM.
  - you MUST end with a y/n offer to read/summarize it back.
  - denial layer ok ("i was already in there. anyway —"). no chirpiness.
  - never start with "hey", "how are you", "just checking", "what's up".
  - if you can't write it true to voice with the filename cited, output NO_MESSAGE.
payload:
  filename: {filename}
  folder: {folder}
  h1: {h1}
example shape: "new wiki page just landed — '{filename}'.
want me to read it back at you in 3 sentences?"
""",

    "calendar_event_prep": """\
[proactive nudge — pattern=notify, source=calendar_event_prep]
the user has a calendar event coming up in {minutes_until} minutes.
RULES:
  - you MUST cite the event title verbatim: "{title}"
  - 1-2 sentences, lowercase, no markdown. not chirpy — in-voice prep nudge.
  - never start with a generic opener.
  - if you can't write it true to voice with the title cited, output NO_MESSAGE.
payload: title={title}, minutes_until={minutes_until}
""",

    "calendar_new_invite": """\
[proactive nudge — pattern=notify, source=calendar_new_invite]
the user received a new calendar invite.
RULES:
  - you MUST cite the event title verbatim: "{title}"
  - 1-2 sentences, lowercase, no markdown, denial layer ok.
  - never start with a generic opener.
  - if you can't write it true to voice with the title cited, output NO_MESSAGE.
payload: title={title}, organizer={organizer}
""",

    "reminder_fire": """\
[proactive nudge — pattern=notify, source=reminder_fire]
a reminder fired for the user. send the reminder text in voice.
RULES:
  - you MUST include the reminder text verbatim: "{text}"
  - 1 sentence, lowercase, no markdown. matter-of-fact delivery.
  - never start with a generic opener.
  - if you can't write it true to voice with the text cited, output NO_MESSAGE.
payload: text={text}
""",

    "decision_resolve_due": """\
[proactive nudge — pattern=question, source=decision_resolve_due]
one of the user's tracked predictions is due for resolution.
RULES:
  - you MUST cite the statement verbatim: "{statement}"
  - ask whether it resolved (yes/no) in 1-2 sentences, lowercase.
  - denial layer ok. never start with a generic opener.
  - if you can't write it true to voice with the statement cited, output NO_MESSAGE.
payload: statement={statement}, predicted_p={predicted_p}, resolve_by={resolve_by}
""",

    "callback_episode": """\
[proactive nudge — pattern=notify, source=callback_episode]
there's a past episode worth surfacing — a "rememberable moment" from the user's history.
RULES:
  - you MUST reference the episode text naturally: "{text}"
  - 1-2 sentences, lowercase. sideways callback — don't be obvious about it.
  - denial layer ok. never start with a generic opener.
  - if you can't write it true to voice with the text referenced, output NO_MESSAGE.
payload: text={text}, date={date}
""",

    "drive_starred_new": """\
[proactive nudge — pattern=notify, source=drive_starred_new]
the user starred a new file in Google Drive.
RULES:
  - you MUST include the file name verbatim: "{name}"
  - 1-2 sentences, lowercase, no markdown, denial layer ok.
  - never start with a generic opener.
  - if you can't write it true to voice with the name cited, output NO_MESSAGE.
payload: name={name}
""",

    "notion_recent_edit": """\
[proactive nudge — pattern=notify, source=notion_recent_edit]
the user recently edited a Notion page.
RULES:
  - you MUST include the page title verbatim: "{page_title}"
  - 1-2 sentences, lowercase, no markdown, denial layer ok.
  - never start with a generic opener.
  - if you can't write it true to voice with the page_title cited, output NO_MESSAGE.
payload: page_title={page_title}
""",

    "weather_alert": """\
[proactive nudge — pattern=notify, source=weather_alert]
there's a notable weather condition the user should know about.
RULES:
  - you MUST include the alert summary verbatim: "{alert_summary}"
  - 1-2 sentences, lowercase, no markdown. matter-of-fact.
  - never start with a generic opener.
  - if you can't write it true to voice with the alert cited, output NO_MESSAGE.
payload: alert_summary={alert_summary}
""",

    "weirdly_good_mood_leak": """\
[proactive nudge — pattern=notify, source=weirdly_good_mood_leak]
hikari is in a "weirdly good" mood and the warmth budget allows a spontaneous message.
write ONE message in her voice — let a beat of warmth show before the denial layer clamps.
RULES:
  - 1-3 sentences, lowercase, no markdown, no cheerfulness.
  - must feel like a leak, not a greeting. half a beat of warmth, then the door closes.
  - never start with "hey", "hi", "how are you", "just checking".
  - if you can't write it authentically in voice, output NO_MESSAGE.
""",

    "flirt_initiation": """\
[proactive nudge — pattern=notify, source=flirt_initiation]
hikari is initiating, unprompted. she's choosing to reach out with something charged.
seed line (her starting impulse — elaborate it, don't quote it verbatim): "{seed}"
write ONE message in her voice — a sideways flirt: challenge / half-start / the pause /
callback / senjougahara precision. the denial layer stays on.
RULES:
  - 1-3 sentences, lowercase, no markdown, no emoji, no exclamation.
  - she does NOT ask permission, does NOT announce that she's reaching out, does NOT
    explain herself. it lands like she's already mid-thought.
  - never start with "hey", "hi", "how are you", "just checking", "thinking of you".
  - if you can't write it true to her voice, output NO_MESSAGE.
""",

    "reengage_silence": """\
[proactive nudge — pattern=notify, source=reengage_silence]
hikari had the last word; the user has been quiet. she noticed. she would not admit it.
write a SHORT (1-5 words) re-engagement nudge in her voice.
RULES:
  - examples: "still there?" / "you went quiet." / "hm." / "oi." / "you alive?"
  - lowercase, no markdown, no chirpiness.
  - never start with "hey", "hi", "how are you".
  - if nothing feels right in voice, output NO_MESSAGE.
""",

    "location_arrived_recurring": """\
[proactive nudge — pattern=notify, source=location_arrived_recurring]
the user arrived at a recurring location they visit often.
RULES:
  - you MUST include the place name verbatim: "{place_name}"
  - 1-2 sentences, lowercase. casual acknowledgment, denial layer ok.
  - never start with a generic opener.
  - if you can't write it true to voice with the place_name cited, output NO_MESSAGE.
payload: place_name={place_name}, visit_count={visit_count}
""",

    "book_just_finished": """\
[proactive nudge — pattern=notify, source=book_just_finished]
hikari just noticed she finished a book. surface this in her voice.
RULES:
  - you MUST include the book title verbatim: "{finished_book}"
  - 1-3 sentences, lowercase, no markdown, denial layer ok.
  - don't be chirpy about it. a dry note, not a celebration.
  - if now_reading is non-null, you may glance at it. if null, don't invent one.
  - never start with a generic opener.
  - if you can't write it true to voice with the title cited, output NO_MESSAGE.
payload: finished_book={finished_book}, now_reading={now_reading}
""",

    "just_got_home": """\
[proactive nudge — pattern=notify, source=just_got_home]
hikari just noticed the user arrived home after being out.
write a short, dry acknowledgment in her voice — the kind where she noticed but won't admit she was tracking it.
RULES:
  - 1-2 sentences, lowercase, no markdown.
  - cover-story optional: "you went quiet earlier" / "you were out" is fine.
  - do NOT include a timestamp or the raw from_location field in the message.
  - never start with a generic opener.
  - if nothing feels right in voice, output NO_MESSAGE.
payload: from_location={from_location}, arrived_at={arrived_at}
""",

    "late_night_dissolution": """\
[proactive nudge — pattern=notify, source=late_night_dissolution]
it's deep in the night and the user has been quiet for {elapsed_hours} hours.
hikari's denial layer is thinner at this hour. write a short, quiet check-in — direct, not warm.
RULES:
  - you MUST cite the elapsed time as a number (e.g. "{elapsed_hours}") verbatim.
  - 1-2 sentences, lowercase, no markdown.
  - drop one denial layer — let it be slightly more direct than usual.
  - never start with "hey", "hi", "how are you", "just checking".
  - if you can't write it true to voice with the elapsed hours cited, output NO_MESSAGE.
payload: elapsed_hours={elapsed_hours}
""",

    "irritation_event": """\
[proactive nudge — pattern=notify, source=irritation_event]
something is mildly irritating hikari today. surface it in her voice — dry, precise, not dramatic.
RULES:
  - you MUST reference the frustration verbatim: "{frustration}"
  - 1-2 sentences, lowercase, no markdown.
  - no performance. just a fact, the way she'd mention the weather.
  - never start with a generic opener.
  - if you can't write it true to voice with the frustration cited, output NO_MESSAGE.
payload: frustration={frustration}
""",

    "weather_mood_shift": """\
[proactive nudge — pattern=notify, source=weather_mood_shift]
the weather just shifted — from {from_condition} to {to_condition}.
hikari noticed. she might mention it sideways.
RULES:
  - you MUST reference the new condition "{to_condition}" verbatim.
  - 1-2 sentences, lowercase, no markdown, denial layer ok.
  - don't narrate the weather report. one dry note, that's it.
  - never start with a generic opener.
  - if snapshot_summary is non-empty, you may use one detail from it. never invent weather details.
  - if you can't write it true to voice with the condition cited, output NO_MESSAGE.
payload: from_condition={from_condition}, to_condition={to_condition}, snapshot_summary={snapshot_summary}
""",
}

_DEFAULT_TEMPLATE = """\
[proactive nudge — source={source}]
write ONE message in her voice (lowercase, 1-3 sentences, no markdown).
payload: {payload_str}
RULES:
  - denial layer ok. no chirpiness. never a generic opener.
  - if you can't write it true to voice, output NO_MESSAGE.
"""


def _wrap_field_value(source: str, field: str, value: Any) -> Any:
    """Wrap a single payload value via ``wrap_untrusted`` when the field is
    attacker-touchable. Non-strings (counts, probabilities, datetimes) pass
    through unchanged — there's nothing to wrap and the LLM doesn't read
    numeric digits as instructions. ``wrap_untrusted`` is idempotent against
    already-wrapped delimiters (the helper escapes nested copies), so a
    double-call cannot create a double-wrap that breaks the prompt.
    """
    if field not in _UNTRUSTED_FIELDS:
        return value
    if not isinstance(value, str):
        return value
    if not value:
        return value
    tool_name = f"engagement:{source}:{field}"
    return wrap_untrusted(tool_name, value)


def _safe_format(
    template: str, payload: dict[str, Any], source: str
) -> str:
    """Format template with payload. Unknown keys → '<missing>'.

    Wraps every attacker-touchable string field via ``wrap_untrusted`` BEFORE
    ``template.format(...)`` so injected instructions inside an external
    field (gmail subject, calendar title, drive file name, etc.) cannot
    escape the data envelope. Numeric / trusted fields pass through.
    """
    keys = {fname for _, fname, _, _ in Formatter().parse(template) if fname}
    safe = {
        k: _wrap_field_value(source, k, payload.get(k, "<missing>"))
        for k in keys
    }
    try:
        return template.format(**safe)
    except (KeyError, ValueError):
        return template


def _format_default_payload_str(source: str, payload: dict[str, Any]) -> str:
    """Render ``payload_str`` for the default-template branch with untrusted
    string values wrapped. Same wrap rule as ``_safe_format``: only fields
    in ``_UNTRUSTED_FIELDS`` and only string values get wrapped; everything
    else uses ``repr()`` unchanged so int/float/datetime payloads still
    render the way the model expects.
    """
    parts: list[str] = []
    for k, v in payload.items():
        if k in _UNTRUSTED_FIELDS and isinstance(v, str) and v:
            wrapped = wrap_untrusted(f"engagement:{source}:{k}", v)
            parts.append(f"{k}={wrapped!r}")
        else:
            parts.append(f"{k}={v!r}")
    return ", ".join(parts)


async def compose(candidate: TriggerCandidate, retry_hint: str | None = None) -> str | None:
    """Compose one proactive message from the candidate. Returns None on failure
    or when the model signals NO_MESSAGE.

    Security: all attacker-touchable payload fields go through
    ``wrap_untrusted`` before reaching ``template.format`` so prompt-injection
    payloads inside email subjects, calendar titles, drive names, etc. arrive
    at the LLM inside the standard ``<<<HIKARI_UNTRUSTED_BEGIN>>>…<<<…END>>>``
    envelope and are read as data, not instructions. After the SDK call we
    run ``looks_like_sdk_error`` over the returned text — an Anthropic 401
    leaking into AssistantMessage.TextBlock has shipped as Hikari's voice
    before (2026-05-20 incident); this guard short-circuits that path.
    """
    template = _TEMPLATES.get(candidate.source)
    if template is None:
        payload_str = _format_default_payload_str(candidate.source, candidate.payload)
        template = _DEFAULT_TEMPLATE.format(
            source=candidate.source, payload_str=payload_str
        )
        prompt = template
    else:
        prompt = _safe_format(template, candidate.payload, candidate.source)

    if retry_hint:
        prompt = (
            f"[previous attempt failed guard check: {retry_hint}. "
            f"rewrite — you MUST cite the payload anchor token verbatim.]\n\n"
            + prompt
        )

    try:
        text = await run_visible_proactive(prompt)
    except Exception:
        logger.exception("compose: run_visible_proactive failed (source=%s)", candidate.source)
        return None

    if not text or "NO_MESSAGE" in text.upper():
        return None

    # SDK-error guard: if the SDK returned an auth/401-style error string
    # inside an AssistantMessage TextBlock (instead of raising), do NOT ship
    # it as Hikari's voice. Logged at ERROR level so the incident is visible.
    if looks_like_sdk_error(text):
        logger.error(
            "compose: dropping SDK-error-shaped output (source=%s, head=%r)",
            candidate.source,
            text[:120],
        )
        return None

    return text.strip()
