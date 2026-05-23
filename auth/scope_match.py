"""Google OAuth scope superset matching.

Notion public OAuth returns a non-rotating long-lived access_token.
No refresh endpoint, no rotation, no mutex needed.

Usage::

    from auth.scope_match import scope_satisfies
    if scope_satisfies(required_scope, granted_set):
        ...
"""
from __future__ import annotations

_GOOGLE_SUPERSETS: dict[str, frozenset[str]] = {
    # superset_scope → frozenset of scopes it covers
    "https://mail.google.com/": frozenset({
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/gmail.send",
        "https://www.googleapis.com/auth/gmail.modify",
        "https://www.googleapis.com/auth/gmail.compose",
        "https://www.googleapis.com/auth/gmail.metadata",
        "https://www.googleapis.com/auth/gmail.labels",
        "https://www.googleapis.com/auth/gmail.settings.basic",
        "https://www.googleapis.com/auth/gmail.settings.sharing",
    }),
    "https://www.googleapis.com/auth/calendar": frozenset({
        "https://www.googleapis.com/auth/calendar.events",
        "https://www.googleapis.com/auth/calendar.readonly",
        "https://www.googleapis.com/auth/calendar.events.readonly",
        "https://www.googleapis.com/auth/calendar.settings.readonly",
        "https://www.googleapis.com/auth/calendar.acls",
        "https://www.googleapis.com/auth/calendar.acls.readonly",
    }),
    "https://www.googleapis.com/auth/drive": frozenset({
        "https://www.googleapis.com/auth/drive.file",
        "https://www.googleapis.com/auth/drive.readonly",
        "https://www.googleapis.com/auth/drive.metadata",
        "https://www.googleapis.com/auth/drive.metadata.readonly",
        "https://www.googleapis.com/auth/drive.appdata",
    }),
}


def scope_satisfies(required: str, granted: set[str]) -> bool:
    """Return True if ``required`` is satisfied by the ``granted`` set.

    Handles:
    - Exact match.
    - Wildcard ``"*"`` in granted (fine-grained PATs).
    - Google documented superset table (e.g. ``https://mail.google.com/``
      covers ``gmail.modify``).
    """
    if required in granted:
        return True
    # Wildcard: any provider with '*' grants everything (fine-grained PATs).
    if "*" in granted:
        return True
    # Superset table.
    for super_scope, covered in _GOOGLE_SUPERSETS.items():
        if super_scope in granted and required in covered:
            return True
    return False
