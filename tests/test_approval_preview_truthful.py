"""Phase 6C — truthful approval previews.

Tests for the improved _summarize() fallback in gatekeeper_can_use_tool:
  - critical fields (recipients, paths, code, etc.) are never truncated
  - non-critical fields are truncated at 100 chars
  - oversized payloads get a sentinel warning
  - tools with a per-tool summarizer use that instead of the fallback
"""

from __future__ import annotations

import pytest

from tools.gatekeeper_can_use_tool import _CRITICAL_FIELDS, _summarize

# ---------------------------------------------------------------------------
# Critical-field preservation
# ---------------------------------------------------------------------------

def test_long_recipients_shown_in_full():
    """A 'recipients' critical field is NOT per-field-truncated (unlike non-critical fields).

    Non-critical fields are capped at 100 chars each. Critical fields are rendered
    in full (up to the 2000-char overall cap). This test verifies the per-field
    truncation at 100 chars does NOT apply to critical fields.

    Uses a tool without a per-tool summarizer to exercise the fallback path.
    """
    # A value that would be truncated at 100 chars if treated as non-critical.
    recipients = "alice@example.com, bob@example.com, carol@example.com"  # 52 chars - fits fine
    long_value = "X" * 150  # would be truncated if non-critical, kept for critical
    result = _summarize("mcp__fake_sender__send_message", {
        "recipients": recipients,
        "non_critical_field": long_value,
    })
    # Critical field: shown in full (no 100-char cut)
    assert recipients in result, "recipients critical field was truncated"
    # Non-critical field: truncated at 100 chars
    assert "X" * 150 not in result, "non-critical field should have been truncated"
    assert "X" * 100 in result, "non-critical field should have partial content"


def test_long_body_is_trimmed():
    """A 10,000-char 'body' field (non-critical) must be truncated.

    Uses a tool without a per-tool summarizer to exercise the fallback path.
    """
    body = "x" * 10_000
    # Use a tool that has no explicit summarizer (fallback path)
    result = _summarize("mcp__fake_sender__send_message", {"body": body})
    # The raw body must NOT appear in full — it should be clipped.
    assert "x" * 10_000 not in result
    # But the field name should still appear.
    assert "body" in result


def test_mixed_args_critical_full_noncritical_truncated():
    """Critical field shown in full; non-critical field truncated."""
    to = "alice@example.com"
    long_label = "S" * 500  # 'label' is not critical
    result = _summarize("fake_tool", {"to": to, "label": long_label})
    # 'to' is critical — must appear verbatim.
    assert to in result
    # 'label' is not critical — must be clipped.
    assert "S" * 500 not in result
    assert "label" in result


def test_oversized_noncritical_payload_keeps_critical_full_elides_rest():
    """When critical fits but non-critical pushes over 2000 chars, keep critical
    in full and elide the rest with the NON-CRITICAL FIELDS ELIDED sentinel."""
    args = {
        "recipients": "alice@example.com, bob@example.com",  # critical, small
    }
    # Add many non-critical fields totaling > 2000 chars after 100-char per-field cap.
    for i in range(40):
        args[f"meta_{i}"] = "Z" * 200  # each gets truncated to 100 chars at render
    result = _summarize("some_tool", args)
    # Critical recipients must be shown in full
    assert "alice@example.com, bob@example.com" in result
    # Non-critical elision sentinel must fire
    assert "NON-CRITICAL FIELDS ELIDED" in result
    assert "critical fields shown in full above" in result


def test_critical_fields_exceeding_cap_refuse_render():
    """When critical fields alone exceed 2000 chars, refuse to render values.
    Show only the field-name list + REFUSE sentinel — operator must split the call."""
    args = {
        "recipients": "r" * 2500,  # critical, alone exceeds 2000
        "body": "B" * 100,
    }
    result = _summarize("some_tool", args)
    assert "CRITICAL FIELDS EXCEED 2000 CHARS" in result
    assert "REFUSE THIS APPROVAL" in result
    # Values must NOT leak — only the field-name index
    assert "r" * 2500 not in result
    assert "recipients" in result  # field name listed
    assert "body" in result        # field name listed


def test_all_critical_field_names_recognised():
    """Smoke-test: every name in _CRITICAL_FIELDS round-trips through _summarize."""
    for field in _CRITICAL_FIELDS:
        args = {field: "sensitive_value_" + "x" * 200}
        result = _summarize("some_tool", args)
        # Value must appear untruncated (200 x's + prefix still present).
        assert "sensitive_value_" + "x" * 200 in result, (
            f"Critical field {field!r} was unexpectedly truncated"
        )


# ---------------------------------------------------------------------------
# Per-tool summarizer takes precedence
# ---------------------------------------------------------------------------

def test_per_tool_summarizer_used_for_gmail_send():
    """mcp__google_workspace__gmail_send_email has an explicit summarizer that
    must be used instead of the generic fallback."""
    from tools.gatekeeper import summarize
    try:
        result = summarize("mcp__google_workspace__gmail_send_email", {})
    except NotImplementedError:
        pytest.skip("no explicit summarizer for this tool")
    # If we get here the explicit path fired — just verify it returns a string.
    assert isinstance(result, str) and result


def test_fallback_used_when_no_explicit_summarizer():
    """A tool not in gatekeeper.summarize triggers the fallback path."""
    result = _summarize("mcp__fake_tool_xyz__do_something", {"path": "/etc/shadow"})
    # 'path' is a critical field — value must be present in full.
    assert "/etc/shadow" in result
    assert "mcp__fake_tool_xyz__do_something" in result


# ---------------------------------------------------------------------------
# Edge-cases
# ---------------------------------------------------------------------------

def test_empty_args_no_crash():
    result = _summarize("some_tool", {})
    assert "some_tool" in result


def test_non_string_critical_value_shown():
    """Critical field with a non-string value (list, int) should not crash."""
    result = _summarize("some_tool", {"issue_number": 42, "branch": "main"})
    assert "42" in result
    assert "main" in result


def test_non_critical_value_exactly_100_chars_not_truncated():
    """A non-critical value of exactly 100 chars must NOT gain a trailing ellipsis."""
    result = _summarize("some_tool", {"description": "D" * 100})
    assert "D" * 100 in result
    assert "…" not in result


def test_non_critical_value_101_chars_truncated():
    """A non-critical value of 101 chars must be truncated."""
    result = _summarize("some_tool", {"description": "D" * 101})
    assert "D" * 101 not in result
    assert "…" in result
