"""send_and_persist: already_filtered=True must bypass internal filter.

Complementary to test_send_and_persist_api.py — when already_filtered=True
the internal filter and rewrite pipeline must not run.
"""
from __future__ import annotations

import importlib
import sys
import types
from pathlib import Path

import pytest

from storage import db


# ---------------------------------------------------------------------------
# DB isolation
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolated_db(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "hikari.db"
    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "12345")
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    monkeypatch.setattr(db, "_DB_PATH", db_path)
    yield


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class _SentMsg:
    def __init__(self, message_id: int = 1):
        self.message_id = message_id


class _FakeBot:
    def __init__(self):
        self.sent: list[str] = []

    async def send_message(self, chat_id: int, text: str) -> _SentMsg:
        self.sent.append(text)
        return _SentMsg()

    async def send_chat_action(self, chat_id: int, action: str) -> None:
        pass


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_already_filtered_true_skips_filter():
    """already_filtered=True: filter_outgoing must NOT be called; raw text persisted."""
    mod = types.ModuleType("agents.post_filter")

    def filter_outgoing(text: str):
        raise AssertionError("filter_outgoing called despite already_filtered=True")

    async def rewrite_or_fallback(*a, **kw):
        raise AssertionError("rewrite_or_fallback called despite already_filtered=True")

    mod.filter_outgoing = filter_outgoing
    mod.rewrite_or_fallback = rewrite_or_fallback
    sys.modules["agents.post_filter"] = mod

    if "agents.messaging" in sys.modules:
        del sys.modules["agents.messaging"]

    from agents.messaging import send_and_persist

    bot = _FakeBot()
    result = await send_and_persist(
        bot=bot,
        chat_id=7,
        text="pre-filtered text",
        source="chat",
        skip_choreography=True,
        already_filtered=True,
    )

    assert result.ok is True
    assert result.final_text == "pre-filtered text"

    with db._conn() as c:
        rows = c.execute("SELECT content FROM messages WHERE role='assistant'").fetchall()
    assert len(rows) == 1
    assert rows[0]["content"] == "pre-filtered text"


@pytest.mark.asyncio
async def test_already_filtered_false_runs_filter():
    """already_filtered=False (default): filter_outgoing IS called and result persisted."""
    mod = types.ModuleType("agents.post_filter")
    called = {"n": 0}

    class _FR:
        def __init__(self, text):
            self.text = "FILTERED:" + text
            self.refusal_short_replaced = False
            self.refusal_hits = []
            self.sycophancy_triggered = False
            self.sycophancy_violations = []
            self.needs_llm_rewrite = False
            self.rewrite_instruction = None

    def filter_outgoing(text: str):
        called["n"] += 1
        return _FR(text)

    async def rewrite_or_fallback(*a, **kw):
        return "REWRITTEN"

    mod.filter_outgoing = filter_outgoing
    mod.rewrite_or_fallback = rewrite_or_fallback
    sys.modules["agents.post_filter"] = mod

    if "agents.messaging" in sys.modules:
        del sys.modules["agents.messaging"]

    from agents.messaging import send_and_persist

    bot = _FakeBot()
    result = await send_and_persist(
        bot=bot,
        chat_id=7,
        text="raw text",
        source="chat",
        skip_choreography=True,
        already_filtered=False,
    )

    assert result.ok is True
    assert called["n"] == 1
    assert result.final_text == "FILTERED:raw text"


def test_no_direct_reply_text_in_telegram_bridge():
    """Every reply_text call in telegram_bridge must go via send_ephemeral_ack
    or send_and_persist. A bare message.reply_text() is a bypass."""
    import pathlib
    import re
    src = pathlib.Path(__file__).resolve().parent.parent / "agents" / "telegram_bridge.py"
    text = src.read_text()
    raw = list(re.finditer(r'\.reply_text\(', text))
    assert not raw, (
        f"Found {len(raw)} direct .reply_text() call(s) in telegram_bridge.py. "
        "Route via send_ephemeral_ack() or send_and_persist() instead."
    )


def test_no_direct_bot_send_message_in_telegram_bridge():
    """bot.send_message and bot.send_photo must go via send_ephemeral_ack
    or _drain_media_outbox (the latter is the outbox drainer)."""
    import ast
    import pathlib
    src = pathlib.Path(__file__).resolve().parent.parent / "agents" / "telegram_bridge.py"
    tree = ast.parse(src.read_text())
    ALLOWED_FUNCS = {
        "_send_outbox_text",
        "_send_outbox_sticker",
        "_send_outbox_document",
        "_send_outbox_photo",
        "_drain_photo_outbox",
        "_drain_media_outbox",
        # Inline-keyboard callback acks — short bot.send_message replies
        # to button taps (not agent turns; ephemeral by nature).
        "_cb_approvals",
        "_cb_checkin",
        "_cb_reminder",
        "cmd_approvals",
        "cmd_checkin",
        "cmd_reminders",
    }
    violations: list[str] = []

    class V(ast.NodeVisitor):
        def __init__(self):
            self.stack: list[str] = []
        def visit_AsyncFunctionDef(self, node):
            self.stack.append(node.name)
            self.generic_visit(node)
            self.stack.pop()
        def visit_FunctionDef(self, node):
            self.stack.append(node.name)
            self.generic_visit(node)
            self.stack.pop()
        def visit_Call(self, node):
            if isinstance(node.func, ast.Attribute) and node.func.attr in {"send_message", "send_photo", "send_document", "send_sticker"}:
                caller_id = None
                if isinstance(node.func.value, ast.Name) and node.func.value.id == "bot":
                    caller_id = "bot"
                elif (isinstance(node.func.value, ast.Attribute)
                      and node.func.value.attr == "bot"
                      and isinstance(node.func.value.value, ast.Name)
                      and node.func.value.value.id == "context"):
                    caller_id = "context.bot"
                if caller_id and (not self.stack or self.stack[-1] not in ALLOWED_FUNCS):
                    violations.append(f"line {node.lineno} in {self.stack[-1] if self.stack else '<module>'}: {caller_id}.{node.func.attr}(...)")
            self.generic_visit(node)

    V().visit(tree)
    assert not violations, (
        f"Found {len(violations)} direct bot.send_*() call(s) outside outbox dispatchers:\n"
        + "\n".join(f"  - {v}" for v in violations)
    )
