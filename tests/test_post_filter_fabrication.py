"""Bug 2 (live 2026-05-21): post_filter backstop for inbox/calendar
fabrication. The model claimed "5 unread, all from Google..." without ever
calling gmail (tool_uses: 0). filter_outgoing reads the LAST_TURN_TOOL_NAMES
contextvar from agents.runtime and ships a redirect line if the reply has
inbox/calendar shape but no relevant fetch tool was invoked this turn."""
from __future__ import annotations

import pytest

from agents.post_filter import (
    _FABRICATED_CALENDAR_RE,
    _FABRICATED_INBOX_RE,
    _FABRICATION_REPLACEMENT,
    _strip_fabricated_external_data,
    filter_outgoing,
)
from agents.runtime import LAST_TURN_TOOL_NAMES

# The live failure mode + variants. Cyrillic cases cover the 2026-06-01
# incident: a Ukrainian digest ("п'ять листів" — a SPELLED-OUT number) sailed
# past the English-only regex.
INBOX_FABRICATIONS = [
    "5 unread, all from Google",
    "you have 3 new emails",
    "12 unread messages in your inbox",
    "your inbox has 7 new messages",
    "nothing new in your inbox",
    "inbox is empty",
    "in your inbox: a few drafts and noise",
    # uk/ru
    "п'ять листів від гугла",
    "3 непрочитаних листи",
    "у скриньці 7 нових",
    "в інбоксі порожньо",
    "5 новых писем",
]

CALENDAR_FABRICATIONS = [
    "you have 2 meetings today",
    "you have 3 events on the calendar",
    "today's calendar: standup, then deep work",
    "tomorrow's schedule looks lighter",
    "next up at 14:00 — your dentist",
    "nothing on your calendar",
    "calendar is empty today",
    # uk/ru
    "2 зустрічі сьогодні",
    "три події завтра",
    "календар порожній",
    "нічого в календарі",
]

# Things that LOOK adjacent but should NOT trip the backstop.
LEGITIMATE = [
    "i sent the email already.",
    "your point about emails being a sink — yeah.",
    "remember that one email from your mom?",
    "calendar app is bad design.",
    "you said tomorrow would be quieter.",
    "no idea what's on your schedule, ask me to check.",
    # uk/ru — casual mentions without a count must NOT trip
    "лист від мами пам'ятаєш?",
    "надішли мені листівку колись",
    "я люблю тебе, на добраніч",
]


@pytest.mark.parametrize("bad", INBOX_FABRICATIONS)
def test_inbox_regex_catches(bad):
    assert _FABRICATED_INBOX_RE.search(bad), f"should have matched: {bad!r}"


@pytest.mark.parametrize("bad", CALENDAR_FABRICATIONS)
def test_calendar_regex_catches(bad):
    assert _FABRICATED_CALENDAR_RE.search(bad), f"should have matched: {bad!r}"


@pytest.mark.parametrize("ok", LEGITIMATE)
def test_legitimate_does_not_match(ok):
    assert not _FABRICATED_INBOX_RE.search(ok), (
        f"inbox re falsely matched: {ok!r}"
    )
    assert not _FABRICATED_CALENDAR_RE.search(ok), (
        f"calendar re falsely matched: {ok!r}"
    )


def test_fabrication_backstop_fires_with_no_tools():
    """Inbox-shape text + empty tool set → backstop fires."""
    token = LAST_TURN_TOOL_NAMES.set(set())
    try:
        text, fired, reason = _strip_fabricated_external_data(
            "5 unread, all from Google",
        )
        assert fired is True
        assert reason == "inbox_no_fetch"
        assert text == _FABRICATION_REPLACEMENT
    finally:
        LAST_TURN_TOOL_NAMES.reset(token)


def test_fabrication_backstop_passes_when_gmail_tool_was_called():
    """Inbox-shape text + real fetch tool → ship as-is."""
    token = LAST_TURN_TOOL_NAMES.set(
        {"mcp__google_workspace__query_gmail_emails"},
    )
    try:
        text, fired, _ = _strip_fabricated_external_data(
            "5 unread, all from Google",
        )
        assert fired is False
        assert text == "5 unread, all from Google"
    finally:
        LAST_TURN_TOOL_NAMES.reset(token)


def test_fabrication_backstop_passes_when_subagent_was_dispatched():
    """Generic delegation (Agent / Task) gets a free pass — subagent tool
    calls happen out-of-stream and don't surface in the parent's message loop,
    so we can't prove no fetch happened."""
    token = LAST_TURN_TOOL_NAMES.set({"Agent"})
    try:
        text, fired, _ = _strip_fabricated_external_data(
            "you have 3 new emails",
        )
        assert fired is False
        assert text == "you have 3 new emails"
    finally:
        LAST_TURN_TOOL_NAMES.reset(token)


def test_fabrication_backstop_fires_on_calendar_without_fetch():
    """Calendar-shape text + a tool was called but NOT a calendar tool →
    fires. The token here is recall — irrelevant to calendar data."""
    token = LAST_TURN_TOOL_NAMES.set({"mcp__hikari_memory__recall"})
    try:
        text, fired, reason = _strip_fabricated_external_data(
            "you have 2 meetings today",
        )
        assert fired is True
        assert reason == "calendar_no_fetch"
        assert text == _FABRICATION_REPLACEMENT
    finally:
        LAST_TURN_TOOL_NAMES.reset(token)


def test_fabrication_backstop_passes_when_calendar_tool_was_called():
    token = LAST_TURN_TOOL_NAMES.set(
        {"mcp__google_workspace__calendar_get_events"},
    )
    try:
        text, fired, _ = _strip_fabricated_external_data(
            "you have 2 meetings today",
        )
        assert fired is False
    finally:
        LAST_TURN_TOOL_NAMES.reset(token)


def test_fabrication_backstop_disabled_via_config(monkeypatch):
    """When the config flag is false, the detector returns the text untouched
    even on a clear fabrication."""
    from agents import config as cfg
    monkeypatch.setattr(
        cfg, "get",
        lambda key, default=None: (
            False if key == "post_filter.fabrication_backstop_enabled"
            else default
        ),
    )
    token = LAST_TURN_TOOL_NAMES.set(set())
    try:
        text, fired, _ = _strip_fabricated_external_data(
            "5 unread, all from Google",
        )
        assert fired is False
        assert text == "5 unread, all from Google"
    finally:
        LAST_TURN_TOOL_NAMES.reset(token)


def test_filter_outgoing_integrates_fabrication_backstop():
    """End-to-end: filter_outgoing routes a fabrication through the backstop
    and returns a FilterResult with short_replaced=True."""
    token = LAST_TURN_TOOL_NAMES.set(set())
    try:
        result = filter_outgoing("5 unread, all from Google")
        assert result.refusal_short_replaced is True
        assert any(
            h.startswith("fabrication_backstop:") for h in result.refusal_hits
        )
        assert result.text == _FABRICATION_REPLACEMENT
    finally:
        LAST_TURN_TOOL_NAMES.reset(token)


def test_filter_outgoing_passes_normal_reply():
    """Normal Hikari chat reply has neither inbox nor calendar shape → passes."""
    token = LAST_TURN_TOOL_NAMES.set(set())
    try:
        result = filter_outgoing("ugh. fine. give me a minute.")
        assert result.refusal_short_replaced is False
        assert not any(
            h.startswith("fabrication_backstop:") for h in result.refusal_hits
        )
    finally:
        LAST_TURN_TOOL_NAMES.reset(token)


def test_fabrication_backstop_fires_on_cyrillic_digest():
    """The 2026-06-01 incident: a Ukrainian digest with an empty tool set must
    now fire (it sailed past the English-only regex before)."""
    token = LAST_TURN_TOOL_NAMES.set(set())
    try:
        text, fired, reason = _strip_fabricated_external_data("п'ять листів від гугла")
        assert fired is True
        assert reason == "inbox_no_fetch"
        assert text == _FABRICATION_REPLACEMENT
    finally:
        LAST_TURN_TOOL_NAMES.reset(token)


def test_fabrication_backstop_exempt_source_skips():
    """daily_checkin builds its digest from the typed adapter (provenance at
    the data layer), so it is exempt even with an empty tool set — otherwise
    the composer turn (no tool) would false-positive."""
    token = LAST_TURN_TOOL_NAMES.set(set())
    try:
        text, fired, _ = _strip_fabricated_external_data(
            "п'ять листів. решта — коли будеш готовий.", source="daily_checkin",
        )
        assert fired is False
        # A non-exempt source with the same text + empty tools DOES fire.
        _, fired_chat, _ = _strip_fabricated_external_data(
            "п'ять листів. решта — коли будеш готовий.", source="chat",
        )
        assert fired_chat is True
    finally:
        LAST_TURN_TOOL_NAMES.reset(token)


def test_fabrication_backstop_passes_with_direct_query_inbox_tool():
    """A direct query_inbox call IS visible in LAST_TURN_TOOL_NAMES, so an
    inbox-shape reply ships unmodified — the main-turn provenance path."""
    token = LAST_TURN_TOOL_NAMES.set({"mcp__hikari_utility__query_inbox"})
    try:
        text, fired, _ = _strip_fabricated_external_data("you have 3 new emails")
        assert fired is False
        assert text == "you have 3 new emails"
    finally:
        LAST_TURN_TOOL_NAMES.reset(token)


def test_fabrication_backstop_strict_mode_blocks_delegation(monkeypatch):
    """With fabrication_delegation_inbox_strict=true, inbox-shape under
    delegation-only (Agent/Task, no observable gmail tool) fires."""
    from agents import config as cfg

    real_get = cfg.get
    monkeypatch.setattr(
        cfg, "get",
        lambda key, default=None: (
            True if key == "post_filter.fabrication_delegation_inbox_strict"
            else real_get(key, default)
        ),
    )
    token = LAST_TURN_TOOL_NAMES.set({"Agent"})
    try:
        _, fired, reason = _strip_fabricated_external_data("you have 3 new emails")
        assert fired is True
        assert reason == "inbox_no_fetch"
    finally:
        LAST_TURN_TOOL_NAMES.reset(token)
