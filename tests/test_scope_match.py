"""Tests for auth.scope_match.scope_satisfies."""
from __future__ import annotations

import pytest

from auth.scope_match import scope_satisfies


class TestScopeMatch:
    def test_scope_match_exact(self):
        """Required scope present in granted set → satisfies."""
        assert scope_satisfies(
            "https://www.googleapis.com/auth/gmail.readonly",
            {"https://www.googleapis.com/auth/gmail.readonly"},
        )

    def test_scope_match_google_mail_superset_covers_modify(self):
        """Granted https://mail.google.com/ covers gmail.modify."""
        assert scope_satisfies(
            "https://www.googleapis.com/auth/gmail.modify",
            {"https://mail.google.com/"},
        )

    def test_scope_match_calendar_superset_covers_events(self):
        """Granted auth/calendar covers calendar.events."""
        assert scope_satisfies(
            "https://www.googleapis.com/auth/calendar.events",
            {"https://www.googleapis.com/auth/calendar"},
        )

    def test_scope_match_wildcard_always_satisfies(self):
        """Granted {'*'} satisfies any required scope."""
        assert scope_satisfies("https://www.googleapis.com/auth/calendar.events", {"*"})
        assert scope_satisfies("https://mail.google.com/", {"*"})
        assert scope_satisfies("some.random.scope", {"*"})

    def test_scope_match_missing_unrelated_scope(self):
        """Granted gmail.readonly does NOT satisfy auth/calendar."""
        assert not scope_satisfies(
            "https://www.googleapis.com/auth/calendar",
            {"https://www.googleapis.com/auth/gmail.readonly"},
        )

    def test_scope_match_drive_superset_covers_drive_file(self):
        """Granted auth/drive covers drive.file."""
        assert scope_satisfies(
            "https://www.googleapis.com/auth/drive.file",
            {"https://www.googleapis.com/auth/drive"},
        )

    def test_scope_match_empty_granted_set(self):
        """Empty granted set satisfies nothing."""
        assert not scope_satisfies("https://mail.google.com/", set())

    def test_scope_match_superset_not_in_granted_no_match(self):
        """Superset not granted, required not exact — no match."""
        assert not scope_satisfies(
            "https://www.googleapis.com/auth/gmail.modify",
            {"https://www.googleapis.com/auth/gmail.readonly"},
        )
