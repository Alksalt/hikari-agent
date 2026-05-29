"""Regression tests for agents.log_scrub redaction patterns.

Covers every pattern documented in _PATTERNS:
  - GitHub PATs: ghp_, gho_, ghs_, ghr_, github_pat_
  - DeepL API keys: :dl (Free) and :fx (Pro) UUID suffixes
  - Legitimate text that must pass through unchanged

Phase-6 additions cover:
  - install_root_filter attaches filters to handlers (child-logger propagation fix)
  - CanaryAlertFilter redacts the raw canary; does not emit it to any sink
"""

from __future__ import annotations

import importlib
import io
import logging
from pathlib import Path

import pytest

from agents.log_scrub import (
    CanaryAlertFilter,
    RedactingFilter,
    _PATTERNS,
    install_record_factory,
    install_root_filter,
)


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
# sk- pattern ordering (B6 regression)
# ---------------------------------------------------------------------------

class TestSkPatternOrdering:
    """Generic sk- must NOT shadow the specific sk-ant-/sk-or- patterns.

    Before B6 the generic `sk-[a-zA-Z0-9_-]{20,}` pattern was listed first, so
    Anthropic and OpenRouter keys received the wrong `[REDACTED-API-KEY]` label
    instead of their specific labels.  The specific patterns must appear first.
    """

    def test_sk_ant_gets_anthropic_label(self):
        token = "sk-ant-api03-ABCDefghijklmnopq1234567890XXXX"
        result = _scrub(f"Authorization: {token}")
        assert token not in result
        assert "[REDACTED-ANTHROPIC-KEY]" in result, (
            f"Expected [REDACTED-ANTHROPIC-KEY], got: {result!r}"
        )
        assert "[REDACTED-API-KEY]" not in result, (
            "Generic label must not be used for sk-ant- tokens"
        )

    def test_sk_or_gets_openrouter_label(self):
        token = "sk-or-v1-ABCDefghijklmnopqrstuvwxyz12345"
        result = _scrub(f"key={token}")
        assert token not in result
        assert "[REDACTED-OPENROUTER-KEY]" in result, (
            f"Expected [REDACTED-OPENROUTER-KEY], got: {result!r}"
        )
        assert "[REDACTED-API-KEY]" not in result, (
            "Generic label must not be used for sk-or- tokens"
        )

    def test_generic_sk_still_catches_openai_style_key(self):
        # An OpenAI-style sk- key (no ant/or infix) must still be redacted.
        token = "sk-proj-ABCDefghijklmnopqrstuvwxyz12345"
        result = _scrub(f"OPENAI_API_KEY={token}")
        assert token not in result
        assert "[REDACTED-API-KEY]" in result

    def test_specific_patterns_before_generic_in_list(self):
        """Structural: verify the pattern list has sk-ant- and sk-or- before sk-."""
        patterns_text = [p.pattern for p, _ in _PATTERNS]
        generic_idx = next(
            i for i, p in enumerate(patterns_text) if p == r"sk-[a-zA-Z0-9_-]{20,}"
        )
        ant_idx = next(
            i for i, p in enumerate(patterns_text) if p == r"sk-ant-[a-zA-Z0-9_-]{20,}"
        )
        or_idx = next(
            i for i, p in enumerate(patterns_text) if p == r"sk-or-[a-zA-Z0-9_-]{20,}"
        )
        assert ant_idx < generic_idx, (
            f"sk-ant- pattern (idx {ant_idx}) must precede generic sk- (idx {generic_idx})"
        )
        assert or_idx < generic_idx, (
            f"sk-or- pattern (idx {or_idx}) must precede generic sk- (idx {generic_idx})"
        )


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


# ---------------------------------------------------------------------------
# Fixtures — Phase-6 isolation helpers
# ---------------------------------------------------------------------------

@pytest.fixture()
def _isolated_db(tmp_path: Path, monkeypatch):
    """Give each Phase-6 test a clean DB so canary operations don't collide."""
    db_path = tmp_path / "hikari.db"
    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    from agents import config
    config.reload()
    yield _db_mod


@pytest.fixture()
def _clean_root_logging():
    """Snapshot the root logger's handlers and filters; restore after each test.

    This prevents install_root_filter calls (and any logging.basicConfig inside
    the test) from leaking handlers or filters into later tests.
    """
    root = logging.getLogger()
    orig_handlers = list(root.handlers)
    orig_filters = list(root.filters)
    orig_level = root.level
    yield
    # Restore handlers (close any we added).
    for h in list(root.handlers):
        if h not in orig_handlers:
            root.removeHandler(h)
    root.handlers[:] = orig_handlers
    root.filters[:] = orig_filters
    root.setLevel(orig_level)


@pytest.fixture()
def _clean_record_factory():
    """Snapshot/restore logging.getLogRecordFactory() and the module-level
    _PREV_FACTORY guard so install_record_factory doesn't leak the scrubbing
    factory into other tests in the suite."""
    import agents.log_scrub as _ls
    orig_factory = logging.getLogRecordFactory()
    orig_prev = _ls._PREV_FACTORY
    yield
    logging.setLogRecordFactory(orig_factory)
    _ls._PREV_FACTORY = orig_prev


# ---------------------------------------------------------------------------
# Phase-6: install_root_filter — handler-level attachment
# ---------------------------------------------------------------------------

class TestInstallRootFilter:
    def test_attaches_filters_to_existing_handlers(self, _clean_root_logging):
        """install_root_filter must put both filter types on every root handler."""
        root = logging.getLogger()
        sink = io.StringIO()
        handler = logging.StreamHandler(sink)
        root.addHandler(handler)

        install_root_filter()

        assert any(isinstance(f, RedactingFilter) for f in handler.filters), (
            "RedactingFilter must be attached to the handler"
        )
        assert any(isinstance(f, CanaryAlertFilter) for f in handler.filters), (
            "CanaryAlertFilter must be attached to the handler"
        )

    def test_idempotent_double_call_does_not_duplicate(self, _clean_root_logging):
        """Calling install_root_filter twice must not double-add filters."""
        root = logging.getLogger()
        sink = io.StringIO()
        handler = logging.StreamHandler(sink)
        root.addHandler(handler)

        install_root_filter()
        install_root_filter()

        redacting_count = sum(1 for f in handler.filters if isinstance(f, RedactingFilter))
        canary_count = sum(1 for f in handler.filters if isinstance(f, CanaryAlertFilter))
        assert redacting_count == 1, f"Expected 1 RedactingFilter, got {redacting_count}"
        assert canary_count == 1, f"Expected 1 CanaryAlertFilter, got {canary_count}"

    def test_child_logger_propagated_record_is_scrubbed(self, _clean_root_logging):
        """KEY REGRESSION: a secret logged at a child logger must be scrubbed
        by the handler after propagation — not reach the sink in cleartext.

        Before Phase-6 the filters lived on the root logger only; Python's
        callHandlers() skips the logger's own filters for propagated records,
        so secrets from child loggers reached handlers unscrubbed.
        """
        token = "ghp_ChildLoggerLeak1234567890ABCDE"
        root = logging.getLogger()
        root.setLevel(logging.DEBUG)

        sink = io.StringIO()
        handler = logging.StreamHandler(sink)
        handler.setLevel(logging.DEBUG)
        root.addHandler(handler)

        install_root_filter()

        # Log the secret from a CHILD logger — it will propagate to root's handler.
        child_logger = logging.getLogger("agents.testchild_phase6")
        child_logger.error("secret leaked: %s", token)

        output = sink.getvalue()
        assert token not in output, (
            f"Raw token must not appear in handler output after Phase-6 fix; got: {output!r}"
        )
        assert "[REDACTED" in output, (
            f"Redaction marker must be present in handler output; got: {output!r}"
        )

    def test_fallback_to_logger_when_no_handlers(self, _clean_root_logging):
        """When root has no handlers, install_root_filter must fall back to
        attaching filters to the root logger (not crash) and those logger-level
        filters must scrub records logged directly at root."""
        root = logging.getLogger()
        # Ensure no handlers are attached for this test.
        for h in list(root.handlers):
            root.removeHandler(h)
        assert not root.handlers, "precondition: no handlers"

        install_root_filter()

        assert any(isinstance(f, RedactingFilter) for f in root.filters)
        assert any(isinstance(f, CanaryAlertFilter) for f in root.filters)


# ---------------------------------------------------------------------------
# Phase-6: CanaryAlertFilter — raw-canary redaction
# ---------------------------------------------------------------------------

class TestCanaryAlertFilterRedaction:
    def test_raw_canary_not_in_output(self, _isolated_db):
        """The raw canary must never reach record.getMessage() after filter runs."""
        from storage import db as _db
        canary = "HIKCAN-TestCanaryPhase6RedactXXXXX"
        _db.runtime_set("injection_canary_v1", canary)

        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="", lineno=0,
            msg=f"some message containing {canary} here",
            args=(), exc_info=None,
        )
        f = CanaryAlertFilter()
        result = f.filter(record)

        assert result is True, "filter must return True (never drop records)"
        msg_out = record.getMessage()
        assert canary not in msg_out, (
            f"Raw canary must be redacted from getMessage(); got: {msg_out!r}"
        )
        assert "[CANARY-REDACTED:" in msg_out, (
            f"Redaction marker [CANARY-REDACTED:<sha8>] must appear; got: {msg_out!r}"
        )
        assert "[CANARY LEAK DETECTED]" in msg_out, (
            f"Detection tag must be present; got: {msg_out!r}"
        )

    def test_canary_escalates_to_critical(self, _isolated_db):
        """A record containing the canary must be force-escalated to CRITICAL."""
        from storage import db as _db
        canary = "HIKCAN-TestCanaryPhase6CriticalXXX"
        _db.runtime_set("injection_canary_v1", canary)

        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="", lineno=0,
            msg=f"leak: {canary}",
            args=(), exc_info=None,
        )
        f = CanaryAlertFilter()
        f.filter(record)

        assert record.levelno == logging.CRITICAL
        assert record.levelname == "CRITICAL"

    def test_canary_redaction_marker_includes_sha8(self, _isolated_db):
        """The [CANARY-REDACTED:<sha8>] marker must contain the correct fingerprint."""
        import hashlib
        from storage import db as _db
        canary = "HIKCAN-TestCanaryPhase6Sha8XXXXXXX"
        _db.runtime_set("injection_canary_v1", canary)
        expected_sha8 = hashlib.sha256(canary.encode()).hexdigest()[:8]

        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="", lineno=0,
            msg=f"data: {canary}",
            args=(), exc_info=None,
        )
        f = CanaryAlertFilter()
        f.filter(record)

        assert f"[CANARY-REDACTED:{expected_sha8}]" in record.getMessage()

    def test_clean_record_passes_unchanged(self, _isolated_db):
        """A record without the canary must pass through untouched."""
        from storage import db as _db
        canary = "HIKCAN-TestCanaryPhase6CleanXXXXXX"
        _db.runtime_set("injection_canary_v1", canary)

        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="", lineno=0,
            msg="totally harmless log line",
            args=(), exc_info=None,
        )
        original_level = record.levelno
        f = CanaryAlertFilter()
        f.filter(record)

        assert record.levelno == original_level, "level must not change for clean records"
        assert "CANARY" not in record.getMessage()

    def test_args_cleared_after_canary_redaction(self, _isolated_db):
        """After canary redaction, record.args must be None so getMessage() does
        not re-interpolate the original raw token back into the message."""
        from storage import db as _db
        canary = "HIKCAN-TestCanaryPhase6ArgsClear"
        _db.runtime_set("injection_canary_v1", canary)

        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="", lineno=0,
            msg="value is %s",
            args=(canary,),
            exc_info=None,
        )
        f = CanaryAlertFilter()
        f.filter(record)

        assert record.args is None, "args must be cleared after redaction"
        assert canary not in record.getMessage()

    def test_exception_safety_with_broken_db(self, monkeypatch):
        """If the DB lookup raises, the filter must return True without crashing
        and must not modify the record."""
        import storage.db as _db_mod
        monkeypatch.setattr(_db_mod, "runtime_get", lambda k: (_ for _ in ()).throw(RuntimeError("db down")))

        record = logging.LogRecord(
            name="test", level=logging.WARNING, pathname="", lineno=0,
            msg="log line with no canary check possible",
            args=(), exc_info=None,
        )
        original_level = record.levelno
        f = CanaryAlertFilter()
        # Must not raise.
        result = f.filter(record)
        assert result is True
        assert record.levelno == original_level


# ---------------------------------------------------------------------------
# Phase-6 (security BLOCK fix): exc_info traceback redaction (Finding 1)
# ---------------------------------------------------------------------------

class TestExcInfoRedaction:
    def test_exc_info_traceback_token_and_canary_redacted(self, _isolated_db):
        """REGRESSION (Finding 1): the stdlib Formatter appends
        formatException(exc_info) AFTER filters run. A token / canary reprd in
        a traceback frame must therefore be scrubbed via record.exc_text, or it
        leaks in cleartext. Format with a real Formatter and assert NEITHER the
        ghp_ token NOR the raw canary appears, and the record is CRITICAL.

        Before the fix this test fails: the filters only touched
        record.getMessage() and the traceback reached the sink unredacted.
        """
        from storage import db as _db
        token = "ghp_ExcInfoLeak1234567890ABCDEFGHIJ"
        canary = "HIKCAN-ExcInfoTracebackLeakXXXXXXX"
        _db.runtime_set("injection_canary_v1", canary)

        # Raise an exception whose message embeds both secrets so they appear in
        # the rendered traceback text.
        try:
            raise ValueError(f"boom token={token} canary={canary}")
        except ValueError:
            exc_info = __import__("sys").exc_info()

        record = logging.LogRecord(
            name="test", level=logging.ERROR, pathname="", lineno=0,
            msg="an error occurred",
            args=(), exc_info=exc_info,
        )

        # Run both filters in the order they'd run on a handler.
        RedactingFilter().filter(record)
        CanaryAlertFilter().filter(record)

        # Format with a REAL formatter — this is what reaches the sink.
        formatter = logging.Formatter("%(levelname)s %(message)s")
        formatted = formatter.format(record)

        assert token not in formatted, (
            f"ghp_ token must not appear in formatted traceback; got: {formatted!r}"
        )
        assert canary not in formatted, (
            f"raw canary must not appear in formatted traceback; got: {formatted!r}"
        )
        assert "[REDACTED-GITHUB-PAT]" in formatted
        assert "[CANARY-REDACTED:" in formatted
        assert record.levelno == logging.CRITICAL
        assert record.levelname == "CRITICAL"

    def test_redacting_filter_sets_exc_text(self, _isolated_db):
        """RedactingFilter must populate record.exc_text with the scrubbed
        traceback so the handler's Formatter reuses it instead of re-rendering
        the raw exc_info."""
        token = "ghp_SetsExcText1234567890ABCDEFGHIJ"
        try:
            raise RuntimeError(f"secret={token}")
        except RuntimeError:
            exc_info = __import__("sys").exc_info()

        record = logging.LogRecord(
            name="test", level=logging.ERROR, pathname="", lineno=0,
            msg="failure", args=(), exc_info=exc_info,
        )
        RedactingFilter().filter(record)

        assert record.exc_text is not None
        assert token not in record.exc_text
        assert "[REDACTED-GITHUB-PAT]" in record.exc_text


# ---------------------------------------------------------------------------
# Phase-6 (security BLOCK fix): LogRecordFactory backstop (Finding 2)
# ---------------------------------------------------------------------------

class TestRecordFactoryBackstop:
    def test_factory_scrubs_secret_at_creation(self, _clean_record_factory):
        """install_record_factory must wrap the record factory so records
        created via the factory are scrubbed even with no handler filters."""
        install_record_factory()
        token = "ghp_FactoryBackstop1234567890ABCDEF"
        factory = logging.getLogRecordFactory()
        record = factory(
            "test", logging.INFO, "", 0,
            f"leak via factory: {token}", (), None,
        )
        assert token not in record.getMessage()
        assert "[REDACTED-GITHUB-PAT]" in record.getMessage()

    def test_factory_idempotent_no_stack(self, _clean_record_factory):
        """Calling install_record_factory twice must not stack wrappers.

        After two installs the active factory must be identical to the one after
        a single install (the guard short-circuits the second call)."""
        install_record_factory()
        first = logging.getLogRecordFactory()
        install_record_factory()
        second = logging.getLogRecordFactory()
        assert first is second, "second install must not wrap again"

    def test_late_added_handler_is_covered_by_factory(
        self, _clean_root_logging, _clean_record_factory, _isolated_db
    ):
        """Defense-in-depth: a handler added AFTER install_root_filter still
        receives scrubbed records, because the factory scrubs at creation."""
        root = logging.getLogger()
        root.setLevel(logging.DEBUG)

        install_root_filter()  # wires the factory backstop

        # Add a NEW handler that never got handler-level filters.
        sink = io.StringIO()
        late_handler = logging.StreamHandler(sink)
        late_handler.setLevel(logging.DEBUG)
        root.addHandler(late_handler)

        token = "ghp_LateHandlerLeak1234567890ABCDEF"
        logging.getLogger("agents.testchild_latehandler").error(
            "secret: %s", token
        )

        output = sink.getvalue()
        assert token not in output, (
            f"factory backstop must scrub records for late-added handlers; got: {output!r}"
        )
        assert "[REDACTED" in output

    def test_install_root_filter_installs_factory(
        self, _clean_root_logging, _clean_record_factory
    ):
        """install_root_filter must wire the factory backstop (single call site
        for both layers)."""
        import agents.log_scrub as _ls
        # Force a not-yet-installed state for this test; the fixture restores
        # both the factory and _PREV_FACTORY afterward.
        _ls._PREV_FACTORY = None
        logging.setLogRecordFactory(logging.LogRecord)
        install_root_filter()
        assert _ls._PREV_FACTORY is not None, (
            "install_root_filter must call install_record_factory"
        )
        # The active factory must now be our scrubbing wrapper, not the plain one.
        assert logging.getLogRecordFactory() is not logging.LogRecord


class TestCanaryReentryGuard:
    """The canary filter's DB read can recurse into logging (first-time schema
    init logs while holding a non-reentrant lock). The thread-local guard must
    break that cycle so we neither recurse infinitely nor deadlock."""

    def test_reentrant_runtime_get_does_not_re_enter_db_read(self, _isolated_db, monkeypatch):
        import storage.db as _db
        from agents.log_scrub import CanaryAlertFilter

        f = CanaryAlertFilter()
        calls = {"n": 0}

        def _runtime_get_that_logs(key):
            calls["n"] += 1
            if calls["n"] == 1:
                # Simulate schema-init emitting a log record WHILE inside the DB
                # read, on this same thread. The guard must make this nested
                # filter() short-circuit instead of calling runtime_get again.
                nested = logging.LogRecord(
                    "storage.db", logging.WARNING, "", 0,
                    "fts porter migration: something", (), None,
                )
                f.filter(nested)
            return None  # no canary configured → clean path

        monkeypatch.setattr(_db, "runtime_get", _runtime_get_that_logs)

        outer = logging.LogRecord("x", logging.INFO, "", 0, "outer line", (), None)
        f.filter(outer)

        # Without the guard the nested filter() would call runtime_get a 2nd
        # time (and in production re-enter the held schema lock → deadlock).
        assert calls["n"] == 1, "re-entry guard failed: nested record re-read the DB"
        # Guard is cleared after the outer call returns (not stuck active).
        import agents.log_scrub as _ls
        assert getattr(_ls._canary_reentry, "active", False) is False
