"""Tests for widened _redact patterns and /audit id defense-in-depth."""
from __future__ import annotations

from tools.approvals import _redact

# ---------------------------------------------------------------------------
# Pattern-based redaction
# ---------------------------------------------------------------------------

def test_redact_openrouter_key():
    text = "sk-or-v1-abc123defghijklmnopqrstuvwxyz01"
    assert "[REDACTED" in _redact(text)
    assert "sk-or-" not in _redact(text)


def test_redact_anthropic_key():
    text = "sk-ant-api01-abcdefghijklmnopqrstuvwxyz1234"
    assert "[REDACTED" in _redact(text)


def test_redact_github_pat():
    text = "ghp_" + "A" * 36
    assert "[REDACTED-GH-PAT]" in _redact(text)


def test_redact_github_pat_long():
    text = "github_pat_" + "A" * 82
    assert "[REDACTED-GH-PAT]" in _redact(text)


def test_redact_gh_token_variants():
    for prefix in ("gho_", "ghp_", "ghr_", "ghs_"):
        text = prefix + "B" * 35
        result = _redact(text)
        assert "B" * 35 not in result, f"{prefix} token not redacted"


def test_redact_slack_token():
    text = "xoxb-1234567890-abcdefghijklmnop"
    assert "[REDACTED-SLACK]" in _redact(text)


def test_redact_notion_secret():
    text = "secret_" + "a" * 43
    assert "[REDACTED-NOTION]" in _redact(text)


def test_redact_telegram_bot_token():
    # 10-digit bot id + colon + exactly 35 alphanumeric chars
    text = "1234567890:" + "A" * 35
    assert "[REDACTED-TG-BOT]" in _redact(text)


def test_redact_jwt():
    # minimal structural JWT (header.payload.sig, each base64url-encoded)
    text = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1c2VyIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
    assert "[REDACTED-JWT]" in _redact(text)


def test_redact_bearer_token():
    text = "Authorization: Bearer abc123.def456.ghi789"
    result = _redact(text)
    assert "abc123" not in result
    assert "Bearer [REDACTED]" in result


def test_redact_bearer_with_spaces():
    """Bearer pattern should handle whitespace after Bearer."""
    text = "Bearer  some_token_value_here_long_enough"
    result = _redact(text)
    assert "some_token_value_here_long_enough" not in result


# ---------------------------------------------------------------------------
# Key-name based redaction
# ---------------------------------------------------------------------------

def test_redact_api_key_by_name():
    text = '{"api_key": "abc123def456"}'
    result = _redact(text)
    assert "abc123def456" not in result
    assert "[REDACTED]" in result


def test_redact_password_by_name():
    text = '{"password": "supersecret"}'
    result = _redact(text)
    assert "supersecret" not in result


def test_redact_authorization_header_by_name():
    text = '{"authorization": "Bearer some_token_here"}'
    result = _redact(text)
    assert "some_token_here" not in result


def test_redact_webhook_url_by_name():
    text = '{"webhook_url": "https://hooks.example.com/services/T000/B000/xyz"}'
    result = _redact(text)
    assert "xyz" not in result


def test_redact_access_token_by_name():
    text = '{"access_token": "ya29.abc123_token"}'
    result = _redact(text)
    assert "ya29.abc123_token" not in result


# ---------------------------------------------------------------------------
# Empty / no-op cases
# ---------------------------------------------------------------------------

def test_redact_empty_string():
    assert _redact("") == ""


def test_redact_clean_text():
    text = "hello world, no secrets here"
    assert _redact(text) == text
