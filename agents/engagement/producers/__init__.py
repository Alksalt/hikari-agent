"""Producer registry for the engagement_tick.

Each module exposes a synchronous ``collect() -> list[TriggerCandidate]``
function. The unified _engagement_tick in scheduler.py calls all enabled
producers in parallel (via asyncio.gather wrapping sync callables) and
passes the merged candidate list to the selector.

All 15 producers:
  Default-on (5):
    gmail_unread_threshold, calendar_event_prep, reminder_fire,
    wiki_new_file, decision_resolve_due

  Opt-in (10):
    calendar_new_invite, callback_episode, drive_starred_new,
    notion_recent_edit, weather_alert, weirdly_good_mood_leak,
    reengage_silence, location_arrived_recurring, readwise_daily_review
    (stub — Readwise MCP removed 2026-05-21), gmail_important_thread
"""
from agents.engagement.producers import (  # noqa: F401
    calendar_event_prep,
    calendar_new_invite,
    callback_episode,
    decision_resolve_due,
    drive_starred_new,
    gmail_important_thread,
    gmail_unread_threshold,
    location_arrived_recurring,
    notion_recent_edit,
    readwise_daily_review,
    reengage_silence,
    reminder_fire,
    weather_alert,
    weirdly_good_mood_leak,
    wiki_new_file,
)

# Canonical set of all producer source IDs. Imported by the /proactive command
# and the engagement_tick scheduler.
ALL_PRODUCER_IDS: frozenset[str] = frozenset({
    "callback_episode",
    "calendar_event_prep",
    "calendar_new_invite",
    "decision_resolve_due",
    "drive_starred_new",
    "gmail_important_thread",
    "gmail_unread_threshold",
    "location_arrived_recurring",
    "notion_recent_edit",
    "readwise_daily_review",
    "reengage_silence",
    "reminder_fire",
    "weather_alert",
    "weirdly_good_mood_leak",
    "wiki_new_file",
})

DEFAULT_ENABLED_SOURCES: frozenset[str] = frozenset({
    "gmail_unread_threshold",
    "calendar_event_prep",
    "reminder_fire",
    "wiki_new_file",
    "decision_resolve_due",
})

# Map source id → module for dynamic dispatch by the scheduler.
_PRODUCER_MODULES = {
    "callback_episode": callback_episode,
    "calendar_event_prep": calendar_event_prep,
    "calendar_new_invite": calendar_new_invite,
    "decision_resolve_due": decision_resolve_due,
    "drive_starred_new": drive_starred_new,
    "gmail_important_thread": gmail_important_thread,
    "gmail_unread_threshold": gmail_unread_threshold,
    "location_arrived_recurring": location_arrived_recurring,
    "notion_recent_edit": notion_recent_edit,
    "readwise_daily_review": readwise_daily_review,
    "reengage_silence": reengage_silence,
    "reminder_fire": reminder_fire,
    "weather_alert": weather_alert,
    "weirdly_good_mood_leak": weirdly_good_mood_leak,
    "wiki_new_file": wiki_new_file,
}


def get_producer(source_id: str):
    """Return the producer module for the given source id, or None."""
    return _PRODUCER_MODULES.get(source_id)
