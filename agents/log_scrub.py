"""Log redaction filter — strips common secret patterns from log records before
they're written to stdout or sent anywhere off-process.

Wire into the root logger in telegram_bridge.main(). For outbound Telegram
messages, a separate decorator in tools/approvals._redact() handles that path.

Pattern coverage (one-line contract; keep examples in sync with _PATTERNS):
  - `sk-…`, `sk-ant-…`, `sk-or-…`        → OpenAI / Anthropic / OpenRouter keys
  - `Bearer <token>`                      → any HTTP Authorization header value
  - `ya29.…`                              → Google OAuth access tokens
  - `<digits>:<urlsafe>`                  → Telegram bot tokens
  - `<jwt-shape>`                         → Anthropic OAuth JWTs
  - `ghp_/gho_/ghs_/ghr_/github_pat_…`    → GitHub PATs (classic + fine-grained)
  - `<uuid>:dl` / `<uuid>:fx`             → DeepL Free/Pro API keys
  - OAuth-secret JSON/url fields          → client_secret/access_token/refresh_token/code_verifier/?code=
"""

from __future__ import annotations

import hashlib
import logging
import re
import threading

# Re-entry guard for CanaryAlertFilter's DB read. runtime_get() can trigger
# first-time storage.db._ensure_schema (which holds a NON-reentrant lock and
# itself logs during the fts-porter migration). That nested log record must not
# re-enter the canary DB read, or the second _ensure_schema blocks on the held
# lock → startup deadlock. The flag is per-thread.
_canary_reentry = threading.local()

_PATTERNS = [
    # Specific sk- prefixes MUST come before the generic sk- pattern, or the
    # generic pattern consumes the entire token first and the specific label is
    # never reached (regex alternation is ordered, not longest-match).
    (re.compile(r"sk-ant-[a-zA-Z0-9_-]{20,}"), "[REDACTED-ANTHROPIC-KEY]"),
    (re.compile(r"sk-or-[a-zA-Z0-9_-]{20,}"), "[REDACTED-OPENROUTER-KEY]"),
    (re.compile(r"sk-[a-zA-Z0-9_-]{20,}"), "[REDACTED-API-KEY]"),
    (re.compile(r"ya29\.[a-zA-Z0-9_-]+"), "[REDACTED-OAUTH-TOKEN]"),
    (re.compile(r"Bearer [a-zA-Z0-9._-]+"), "Bearer [REDACTED]"),
    (re.compile(r"\b[0-9]{9,11}:[A-Za-z0-9_-]{30,}"), "[REDACTED-TG-BOT-TOKEN]"),
    # GitHub Personal Access Tokens — httpx stack traces from the github MCP
    # server include raw `Authorization: Bearer ghp_…` even after the Bearer
    # pattern above runs (some libs log the value separately). Catch the bare
    # token form explicitly. Fine-grained PATs (`github_pat_…`) are longer and
    # include underscores so they need their own pattern.
    (re.compile(r"\bghp_[A-Za-z0-9]{20,}"), "[REDACTED-GITHUB-PAT]"),
    (re.compile(r"\bgho_[A-Za-z0-9]{20,}"), "[REDACTED-GITHUB-OAUTH]"),
    (re.compile(r"\bghs_[A-Za-z0-9]{20,}"), "[REDACTED-GITHUB-SERVER]"),
    (re.compile(r"\bghr_[A-Za-z0-9]{20,}"), "[REDACTED-GITHUB-REFRESH]"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{40,}"), "[REDACTED-GITHUB-PAT-FG]"),
    # DeepL API keys end in the literal `:dl` (Free) or `:fx` (Pro) suffix.
    # UUID-shaped body (8-4-4-4-12 hex with dashes) followed by the suffix.
    (re.compile(
        r"\b[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}:(?:dl|fx)\b"
    ), "[REDACTED-DEEPL-KEY]"),
    # Anthropic OAuth tokens
    (re.compile(r"\b[a-zA-Z0-9_-]{32,}\.[a-zA-Z0-9_-]{32,}\.[a-zA-Z0-9_-]{16,}"), "[REDACTED-JWT]"),
    # OAuth 2.1 secrets — emitted by mcp_external/. All are token_urlsafe(32) or
    # longer, which is 43+ char base64url alphabet. Capture the value following
    # the key name in JSON / urlencoded / log formats, redact only the value.
    (re.compile(
        r"(client_secret['\"\s:=]+)[A-Za-z0-9_-]{32,}"
    ), r"\1[REDACTED-OAUTH-CLIENT-SECRET]"),
    (re.compile(
        r"(access_token['\"\s:=]+)[A-Za-z0-9_-]{32,}"
    ), r"\1[REDACTED-OAUTH-ACCESS-TOKEN]"),
    (re.compile(
        r"(refresh_token['\"\s:=]+)[A-Za-z0-9_-]{32,}"
    ), r"\1[REDACTED-OAUTH-REFRESH-TOKEN]"),
    (re.compile(
        r"(code_verifier['\"\s:=]+)[A-Za-z0-9_-]{32,}"
    ), r"\1[REDACTED-OAUTH-CODE-VERIFIER]"),
    # Auth code in URL query string after redirect (anchor on ?code= or &code=):
    (re.compile(
        r"([?&]code=)[A-Za-z0-9_-]{32,}"
    ), r"\1[REDACTED-OAUTH-CODE]"),
]


def _scrub_text(text: str) -> str:
    """Apply every secret pattern to `text`. Pure; never raises on str input."""
    out = text
    for pattern, replacement in _PATTERNS:
        out = pattern.sub(replacement, out)
    return out


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            msg = None
        if msg is not None:
            out = _scrub_text(msg)
            if out != msg:
                # Replace formatted message; clear args so getMessage doesn't re-interpolate.
                record.msg = out
                record.args = None
        # Scrub the exception traceback too. The stdlib Formatter appends
        # formatException(record.exc_info) to its output AFTER filters run, so a
        # token reprd inside a traceback frame (e.g. an OAuth secret in a
        # raising call) would otherwise leak in cleartext. Render exc_text once
        # and scrub it; setting record.exc_text is load-bearing — the Formatter
        # reuses a non-None exc_text instead of re-rendering exc_info, so the
        # redacted version is what reaches the sink.
        try:
            if record.exc_info or record.exc_text:
                exc_text = record.exc_text
                if exc_text is None:
                    exc_text = logging.Formatter().formatException(record.exc_info)
                scrubbed = _scrub_text(exc_text)
                record.exc_text = scrubbed
        except Exception:
            return True
        return True


class CanaryAlertFilter(logging.Filter):
    """If a log record contains the injection canary token, escalate to CRITICAL,
    redact the raw canary out of the message, and tag it with a detection marker.

    This is a leak-detection signal — the canary should only ever appear inside
    wrap_untrusted blocks; finding it outbound or in a log path that's about to
    ship somewhere is an exfiltration indicator.

    The raw canary is NEVER emitted: it is replaced with
    ``[CANARY-REDACTED:<sha8>]`` where sha8 is the first 8 hex chars of
    sha256(canary), letting operators correlate without re-exposing the secret.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        # Re-entry guard: the runtime_get() below can recurse into logging (via
        # first-time schema init) on the same thread. If we're already inside a
        # canary DB read here, skip the read for this nested record to avoid a
        # deadlock on storage.db's non-reentrant schema lock. RedactingFilter
        # still scrubs the nested record's secrets independently.
        if getattr(_canary_reentry, "active", False):
            return True
        try:
            from agents.injection_guard import _CANARY_KEY
            from storage import db as _db
        except Exception:
            return True
        _canary_reentry.active = True
        try:
            try:
                msg = record.getMessage()
            except Exception:
                msg = None
            try:
                canary = _db.runtime_get(_CANARY_KEY)
            except Exception:
                canary = None
            if not canary:
                return True

            # The canary can leak through either the formatted message or the
            # exception traceback. The stdlib Formatter appends
            # formatException(record.exc_info) to its output after filters run,
            # so a canary reprd in a traceback frame must be caught here too.
            # Render exc_text once if needed (set it so the Formatter reuses our
            # redacted copy rather than re-rendering the raw exc_info).
            exc_text = record.exc_text
            if exc_text is None and record.exc_info:
                try:
                    exc_text = logging.Formatter().formatException(record.exc_info)
                    record.exc_text = exc_text
                except Exception:
                    exc_text = None

            hit_in_msg = bool(msg) and canary in msg
            hit_in_exc = bool(exc_text) and canary in exc_text
            if not (hit_in_msg or hit_in_exc):
                return True

            # Compute a short hash fingerprint for correlation (never the raw token).
            sha8 = hashlib.sha256(canary.encode()).hexdigest()[:8]
            redacted_marker = f"[CANARY-REDACTED:{sha8}]"

            # Force-escalate level so this stands out in any sink.
            record.levelno = max(record.levelno, logging.CRITICAL)
            record.levelname = "CRITICAL"

            if hit_in_msg:
                # Replace the literal canary in the already-formatted message.
                safe_msg = msg.replace(canary, redacted_marker)
                record.msg = "[CANARY LEAK DETECTED] " + safe_msg
                record.args = None
            if hit_in_exc:
                record.exc_text = exc_text.replace(canary, redacted_marker)
        except Exception:
            return True
        finally:
            _canary_reentry.active = False
        return True


# Singleton filter instances reused by the LogRecordFactory backstop so we do
# not construct a new filter per record.
_FACTORY_REDACTOR = RedactingFilter()
_FACTORY_CANARY = CanaryAlertFilter()

# Guard against double-installing the factory (which would stack our wrapper on
# top of itself). None means "not installed yet".
_PREV_FACTORY = None


def install_record_factory() -> None:
    """Install a LogRecordFactory backstop that scrubs every record at creation.

    Handler-level filters (``install_root_filter``) only run for handlers that
    exist when that function is called. Any handler added later — a future
    logfire ``LoggingHandler``, an ad-hoc ``StreamHandler``, a library that
    attaches its own handler — would otherwise receive records unscrubbed.

    The record factory runs for every record the logging machinery creates, so
    scrubbing here covers handlers added at any time. We keep the handler-level
    filters too: on some manual code paths the factory may run before
    ``exc_info`` is attached to the record, so both layers are needed. They are
    idempotent — re-scrubbing already-redacted text is a no-op, and a second
    canary pass sees the ``[CANARY-REDACTED:…]`` marker, not the raw token.

    Idempotent: a module-level ``_PREV_FACTORY`` guard ensures repeated calls do
    not stack wrappers. Reuses singleton filter instances (no per-record
    construction). The root logger sits at INFO, so sub-threshold DEBUG records
    are not created and the per-record cost is comparable to handler-level
    filtering.
    """
    global _PREV_FACTORY
    if _PREV_FACTORY is not None:
        # Already installed — do not stack.
        return
    prev_factory = logging.getLogRecordFactory()
    _PREV_FACTORY = prev_factory

    def _scrubbing_factory(*args, **kwargs):
        record = prev_factory(*args, **kwargs)
        try:
            _FACTORY_REDACTOR.filter(record)
            _FACTORY_CANARY.filter(record)
        except Exception:
            # Logging must never crash. A scrubbing failure falls back to the
            # raw record rather than dropping it.
            pass
        return record

    logging.setLogRecordFactory(_scrubbing_factory)


def install_root_filter() -> None:
    """Attach the redacting + canary filters to every handler on the root logger.

    Attaching at the handler level (rather than the logger level) ensures that
    records propagated from child loggers (``agents.*``, ``mcp_external.*``,
    httpx, etc.) are also scrubbed.  Python's logging machinery applies a
    logger's *own* filters only to records logged directly at that logger —
    propagated records skip the logger's filters and go straight to the
    handlers via ``callHandlers()``.  Filters installed on the handler itself
    run for every record the handler processes, regardless of origin.

    Attaching to handlers is strictly more complete: a record logged directly
    at the root logger still passes through its handlers, so both paths are
    covered.  The install is idempotent — calling this function more than once
    will not double-add filters.

    Fall-back: if the root logger has no handlers at call time (uncommon in
    production but possible in tests), attach to the root logger as a safety
    net and emit a warning that handler-level scrubbing is unavailable.
    """
    # Defense-in-depth backstop: scrub at record creation so handlers added
    # after this call (future logfire LoggingHandler, ad-hoc StreamHandler, a
    # library's own handler) are still covered. Idempotent — safe to call here
    # as the single wiring site for both layers.
    install_record_factory()

    root = logging.getLogger()
    handlers = root.handlers
    if not handlers:
        # No handlers yet — fall back to logger-level attachment and warn.
        if not any(isinstance(f, RedactingFilter) for f in root.filters):
            root.addFilter(RedactingFilter())
        if not any(isinstance(f, CanaryAlertFilter) for f in root.filters):
            root.addFilter(CanaryAlertFilter())
        logging.getLogger(__name__).warning(
            "install_root_filter: no handlers on root logger; "
            "handler-level scrubbing unavailable — child-logger records may "
            "reach sinks unredacted until handlers are added."
        )
        return
    for handler in handlers:
        if not any(isinstance(f, RedactingFilter) for f in handler.filters):
            handler.addFilter(RedactingFilter())
        if not any(isinstance(f, CanaryAlertFilter) for f in handler.filters):
            handler.addFilter(CanaryAlertFilter())
