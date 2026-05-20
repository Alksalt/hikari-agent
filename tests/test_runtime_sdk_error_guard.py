"""SDK-error-text detection: visible-proactive output that's actually an
SDK auth/network error string must never reach the user."""
from __future__ import annotations

import pytest

from agents.runtime import looks_like_sdk_error


@pytest.mark.parametrize("text", [
    "Failed to authenticate. API Error: 401 The socket connection was closed unexpectedly. For more information, pass `verbose: true` in the second argument to fetch()",
    "Failed to authenticate. API Error: 401 Invalid authentication credentials",
    "API Error: 401 Unauthorized",
    "401: invalid token",
    "  Failed to authenticate. API Error: 401 ...",  # leading whitespace
    "FAILED TO AUTHENTICATE. API ERROR: 401",  # case
])
def test_looks_like_sdk_error_positives(text):
    assert looks_like_sdk_error(text) is True


@pytest.mark.parametrize("text", [
    "",
    "morning. three actual emails from people.",
    "ugh. fine. 14:00 standup, 16:30 dr. visit.",
    "i noticed you got a 401 from the api yesterday",  # mentions but isn't one
    "NO_MESSAGE",
])
def test_looks_like_sdk_error_negatives(text):
    assert looks_like_sdk_error(text) is False
