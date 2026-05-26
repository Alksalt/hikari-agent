"""Regression tests for agents.log_scrub redaction patterns.

Covers every pattern documented in _PATTERNS:
  - GitHub PATs: ghp_, gho_, ghs_, ghr_, github_pat_
  - DeepL API keys: :dl (Free) and :fx (Pro) UUID suffixes
  - Legitimate text that must pass through unchanged
"""

from __future__ import annotations

import logging

from agents.log_scrub import RedactingFilter, _PATTERNS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _scrub(text: str) -> str:
    """Apply every pattern in _PATTERNS to `text` and return the result."""
    out = text
    for pattern, replacement in _PATTERNS:
        out = pattern.sub(replacement, out)
    return out


def _make_record(msg: str) -> logging.LogRecord:
    record = logging.LogRecord(
        name="test", level=logging.INFO, pathname="", lineno=0,
        msg=msg, args=(), exc_info=None,
    )
    return record


def _filter_record(msg: str) -> str:
    """Pass a LogRecord through RedactingFilter and return the post-filter message."""
    f = RedactingFilter()
    record = _make_record(msg)
    f.filter(record)
    return record.getMessage()


# ---------------------------------------------------------------------------
# GitHub PAT patterns
# ---------------------------------------------------------------------------

class TestGitHubPATs:
    def test_ghp_classic_pat_redacted(self):
        token = "ghp_abc123def456789012345678901234567"
        result = _scrub(f"my token is {token}")
        assert token not in result
        assert "[REDACTED-GITHUB-PAT]" in result

    def test_ghp_in_authorization_header_redacted(self):
        token = "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ123456"
        result = _scrub(f"Authorization: Bearer {token}")
        assert token not in result

    def test_gho_oauth_redacted(self):
        token = "gho_ABCDEFGHIJKLMNOPQRSTUVWXYZ123456"
        result = _scrub(f"token={token}")
        assert token not in result
        assert "[REDACTED-GITHUB-OAUTH]" in result

    def test_ghs_server_to_server_redacted(self):
        token = "ghs_ABCDEFGHIJKLMNOPQRSTUVWXYZ123456"
        result = _scrub(f"server token: {token}")
        assert token not in result
        assert "[REDACTED-GITHUB-SERVER]" in result

    def test_ghr_refresh_redacted(self):
        token = "ghr_ABCDEFGHIJKLMNOPQRSTUVWXYZ123456"
        result = _scrub(f"refresh={token}")
        assert token not in result
        assert "[REDACTED-GITHUB-REFRESH]" in result

    def test_github_pat_fine_grained_redacted(self):
        # Fine-grained PATs: github_pat_ + 40+ alphanumeric/underscore chars
        token = "github_pat_" + "A1b2C3d4E5" * 5  # 50 chars body
        result = _scrub(f"using fine-grained pat {token}")
        assert token not in result
        assert "[REDACTED-GITHUB-PAT-FG]" in result

    def test_github_pat_minimum_length_boundary(self):
        # Fine-grained requires 40+ chars after "github_pat_"
        # Exactly 40 chars: should match
        token = "github_pat_" + "a" * 40
        result = _scrub(token)
        assert token not in result

    def test_ghp_too_short_not_redacted(self):
        # Less than 20 chars after ghp_ — should NOT be redacted (too short to be real)
        short = "ghp_abc12"
        result = _scrub(f"not a real token: {short}")
        assert short in result, "short ghp_ prefix should not be redacted"


# ---------------------------------------------------------------------------
# DeepL API key patterns
# ---------------------------------------------------------------------------

class TestDeepLKeys:
    def test_deepl_free_dl_suffix_redacted(self):
        key = "a1b2c3d4-e5f6-7890-abcd-ef1234567890:dl"
        result = _scrub(f"DEEPL_API_KEY={key}")
        assert key not in result
        assert "[REDACTED-DEEPL-KEY]" in result

    def test_deepl_pro_fx_suffix_redacted(self):
        key = "a1b2c3d4-e5f6-7890-abcd-ef1234567890:fx"
        result = _scrub(f"api key: {key}")
        assert key not in result
        assert "[REDACTED-DEEPL-KEY]" in result

    def test_deepl_key_in_url_params_redacted(self):
        key = "deadbeef-cafe-babe-feed-0123456789ab:fx"
        result = _scrub(f"POST /translate?auth_key={key}")
        assert key not in result

    def test_non_deepl_uuid_not_redacted(self):
        # A plain UUID without :dl/:fx suffix must NOT be redacted
        plain_uuid = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
        result = _scrub(f"session={plain_uuid}")
        assert plain_uuid in result, "plain UUID should not be redacted"


# ---------------------------------------------------------------------------
# Legitimate text passes through unchanged
# ---------------------------------------------------------------------------

class TestPassthrough:
    def test_plain_english_unchanged(self):
        text = "the user went to the market and bought milk"
        assert _scrub(text) == text

    def test_python_code_unchanged(self):
        code = "def hello(x): return x + 1"
        assert _scrub(code) == code

    def test_email_address_unchanged(self):
        email = "user@example.com"
        assert _scrub(email) == email

    def test_url_without_secrets_unchanged(self):
        url = "https://api.example.com/v1/endpoint?format=json"
        assert _scrub(url) == url

    def test_log_line_without_secrets_unchanged(self):
        line = "2026-05-26 12:00:00 INFO hikari started"
        assert _scrub(line) == line

    def test_partial_prefix_gho_no_body_unchanged(self):
        # "gho_" alone (no 20+ chars after it) is not a real token
        text = "gho_ is a prefix I was reading about"
        result = _scrub(text)
        # The short token fragment should remain
        assert "gho_" in result


# ---------------------------------------------------------------------------
# RedactingFilter integration (via logging.LogRecord)
# ---------------------------------------------------------------------------

class TestRedactingFilterIntegration:
    def test_filter_redacts_ghp_in_log_message(self):
        token = "ghp_TestToken1234567890ABCDEFGHIJK"
        msg = f"github token value: {token}"
        result = _filter_record(msg)
        assert token not in result
        assert "[REDACTED" in result

    def test_filter_leaves_clean_message_unchanged(self):
        msg = "user asked about the weather in Oslo"
        result = _filter_record(msg)
        assert result == msg

    def test_filter_clears_args_after_redaction(self):
        """When getMessage() succeeds AND redaction fires, record.args is cleared.

        Build a record where the format string has a %s slot so getMessage()
        returns successfully — that's the only code path where the filter can
        both obtain the formatted message and then clear args.
        """
        f = RedactingFilter()
        # msg has a %s slot so getMessage() succeeds and returns the formatted
        # string including the token.
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="", lineno=0,
            msg="token is %s",
            args=("ghp_AAAAABBBBBCCCCCDDDDDEEEEEFFFFF",),
            exc_info=None,
        )
        f.filter(record)
        # After redaction, args must be None so getMessage() doesn't
        # re-interpolate the raw token back into the message.
        assert record.args is None
        assert "[REDACTED-GITHUB-PAT]" in record.msg

    def test_filter_preserves_args_when_no_redaction_needed(self):
        """When nothing is redacted, record.args should be untouched."""
        f = RedactingFilter()
        record = _make_record("plain log message %s")
        record.args = ("value",)
        f.filter(record)
        # args preserved — getMessage() would re-interpolate normally
        assert record.args == ("value",)

    def test_multiple_tokens_all_redacted_in_one_message(self):
        ghp = "ghp_SomeLongClassicTokenABCDEF123456"
        deepl = "a1b2c3d4-e5f6-7890-abcd-ef1234567890:dl"
        msg = f"using {ghp} and deepl key {deepl}"
        result = _filter_record(msg)
        assert ghp not in result
        assert deepl not in result
