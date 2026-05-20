"""Log redaction filter — strips common secret patterns from log records before
they're written to stdout or sent anywhere off-process.

Wire into the root logger in telegram_bridge.main(). For outbound Telegram
messages, a separate decorator in tools/approvals._redact() handles that path.
"""

from __future__ import annotations

import logging
import re

_PATTERNS = [
    (re.compile(r"sk-[a-zA-Z0-9_-]{20,}"), "[REDACTED-API-KEY]"),
    (re.compile(r"sk-ant-[a-zA-Z0-9_-]{20,}"), "[REDACTED-ANTHROPIC-KEY]"),
    (re.compile(r"sk-or-[a-zA-Z0-9_-]{20,}"), "[REDACTED-OPENROUTER-KEY]"),
    (re.compile(r"ya29\.[a-zA-Z0-9_-]+"), "[REDACTED-OAUTH-TOKEN]"),
    (re.compile(r"Bearer [a-zA-Z0-9._-]+"), "Bearer [REDACTED]"),
    (re.compile(r"\b[0-9]{9,11}:[A-Za-z0-9_-]{30,}"), "[REDACTED-TG-BOT-TOKEN]"),
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


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        out = msg
        for pattern, replacement in _PATTERNS:
            out = pattern.sub(replacement, out)
        if out != msg:
            # Replace formatted message; clear args so getMessage doesn't re-interpolate.
            record.msg = out
            record.args = None
        return True


class CanaryAlertFilter(logging.Filter):
    """If a log record contains the injection canary token, escalate to CRITICAL
    and tag the message. This is a leak-detection signal — the canary should
    only ever appear inside wrap_untrusted blocks; finding it outbound or in a
    log path that's about to ship somewhere is an exfiltration indicator."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            from agents.injection_guard import outbound_contains_canary
        except Exception:
            return True
        try:
            msg = record.getMessage()
        except Exception:
            return True
        if outbound_contains_canary(msg):
            # Force-escalate level so this stands out in any sink.
            record.levelno = max(record.levelno, logging.CRITICAL)
            record.levelname = "CRITICAL"
            record.msg = "[CANARY LEAK DETECTED] " + str(record.msg)
            record.args = None
        return True


def install_root_filter() -> None:
    """Attach the redacting + canary filters to the root logger."""
    root = logging.getLogger()
    if not any(isinstance(f, RedactingFilter) for f in root.filters):
        root.addFilter(RedactingFilter())
    if not any(isinstance(f, CanaryAlertFilter) for f in root.filters):
        root.addFilter(CanaryAlertFilter())
