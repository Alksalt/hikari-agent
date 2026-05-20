"""Phase 11: click-Allow UI hallucination backstop."""
from __future__ import annotations
import pytest

from agents.post_filter import _CLICK_ALLOW_RE, _CLICK_ALLOW_REPLACEMENT, _strip_click_allow


# Real strings Hikari sent on 2026-05-19 (verbatim from the db)
HALLUCINATIONS = [
    "needs your permission to access the calendar — the google workspace integration is connected but hasn't been granted yet. you'll need to allow it first.",
    "google workspace tools are connected but need your explicit permission before they can run. it's a one-time thing on your end.",
    "notion tools need your permission before they can run.",
    "claude code shows a permission prompt on your end — you have to click allow there. saying it here doesn't register at the system level.",
    "try asking me to do something with notion or google again and hit allow when the prompt appears.",
    "you'll need to grant google permission first.",
]

LEGITIMATE = [
    "she said yes.",
    "i'll allow it. just this once.",  # contains 'allow' but not the pattern
    "no clicking around. just tell me what's broken.",
    "got it. checking your calendar now.",
    "permission slips? what is this, school?",
    "the prompt says 'session expired'.",  # contains 'prompt' but not 'permission prompt'
]


@pytest.mark.parametrize("bad", HALLUCINATIONS)
def test_click_allow_regex_catches_hallucination(bad):
    assert _CLICK_ALLOW_RE.search(bad), f"should have matched: {bad!r}"


@pytest.mark.parametrize("ok", LEGITIMATE)
def test_click_allow_regex_passes_legitimate(ok):
    assert not _CLICK_ALLOW_RE.search(ok), f"should NOT have matched: {ok!r}"


@pytest.mark.parametrize("bad", HALLUCINATIONS)
def test_strip_click_allow_replaces_bad(bad):
    result, fired = _strip_click_allow(bad)
    assert fired is True
    assert result == _CLICK_ALLOW_REPLACEMENT


@pytest.mark.parametrize("ok", LEGITIMATE)
def test_strip_click_allow_passes_legitimate(ok):
    result, fired = _strip_click_allow(ok)
    assert fired is False
    assert result == ok
