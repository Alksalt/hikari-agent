"""Producer registry for the engagement_tick.

Each module exposes a synchronous ``collect() -> list[TriggerCandidate]``
function. The unified _engagement_tick in scheduler.py calls all enabled
producers in parallel (via asyncio.gather wrapping sync callables) and
passes the merged candidate list to the selector.

All 20 producers:
  Default-on (3):
    calendar_event_prep, wiki_new_file, decision_resolve_due

  Default-on world-delta producers (5):
    book_just_finished, just_got_home, late_night_dissolution,
    irritation_event, weather_mood_shift

  Opt-in (12):
    anniversary_callback, belief_resurface, calendar_new_invite, callback_episode,
    drive_starred_new, notion_recent_edit, weather_alert,
    weirdly_good_mood_leak, reengage_silence, location_arrived_recurring,
    research_callback
"""
from agents.engagement.producers import (  # noqa: F401
    anniversary_callback,
    belief_resurface,
    book_just_finished,
    calendar_event_prep,
    calendar_new_invite,
    callback_episode,
    decision_resolve_due,
    drive_starred_new,
    flirt_initiation,
    irritation_event,
    just_got_home,
    late_night_dissolution,
    location_arrived_recurring,
    notion_recent_edit,
    reengage_silence,
    reminder_fire,
    research_callback,
    weather_alert,
    weather_mood_shift,
    weirdly_good_mood_leak,
    wiki_new_file,
)

# Canonical set of all producer source IDs. Imported by the /proactive command
# and the engagement_tick scheduler.
ALL_PRODUCER_IDS: frozenset[str] = frozenset({
    "anniversary_callback",
    "belief_resurface",
    "book_just_finished",
    "callback_episode",
    "calendar_event_prep",
    "calendar_new_invite",
    "decision_resolve_due",
    "drive_starred_new",
    "flirt_initiation",
    "irritation_event",
    "just_got_home",
    "late_night_dissolution",
    "location_arrived_recurring",
    "notion_recent_edit",
    "reengage_silence",
    "reminder_fire",
    "research_callback",
    "weather_alert",
    "weather_mood_shift",
    "weirdly_good_mood_leak",
    "wiki_new_file",
})

DEFAULT_ENABLED_SOURCES: frozenset[str] = frozenset({
    "calendar_event_prep",
    "wiki_new_file",
    "decision_resolve_due",
    "reengage_silence",
    "book_just_finished",
    "just_got_home",
    "late_night_dissolution",
    "irritation_event",
    "weather_mood_shift",
    # Warmth/intimacy producers enabled 2026-06-03 (kept in sync with
    # config/engagement.yaml proactive.default_enabled_sources — the yaml list
    # is the live source of truth; this set is the fallback + /proactive display).
    "weirdly_good_mood_leak",
    "anniversary_callback",
    "belief_resurface",
    "research_callback",
    "callback_episode",
    "flirt_initiation",
})

# Map source id → module for dynamic dispatch by the scheduler.
_PRODUCER_MODULES = {
    "anniversary_callback": anniversary_callback,
    "belief_resurface": belief_resurface,
    "book_just_finished": book_just_finished,
    "callback_episode": callback_episode,
    "calendar_event_prep": calendar_event_prep,
    "calendar_new_invite": calendar_new_invite,
    "decision_resolve_due": decision_resolve_due,
    "drive_starred_new": drive_starred_new,
    "flirt_initiation": flirt_initiation,
    "irritation_event": irritation_event,
    "just_got_home": just_got_home,
    "late_night_dissolution": late_night_dissolution,
    "location_arrived_recurring": location_arrived_recurring,
    "notion_recent_edit": notion_recent_edit,
    "reengage_silence": reengage_silence,
    "reminder_fire": reminder_fire,
    "research_callback": research_callback,
    "weather_alert": weather_alert,
    "weather_mood_shift": weather_mood_shift,
    "weirdly_good_mood_leak": weirdly_good_mood_leak,
    "wiki_new_file": wiki_new_file,
}


def get_producer(source_id: str):
    """Return the producer module for the given source id, or None."""
    return _PRODUCER_MODULES.get(source_id)
