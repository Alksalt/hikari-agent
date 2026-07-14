import json
from datetime import datetime, timedelta
from types import SimpleNamespace

from agents import mail_handoff


def _write(tmp_path, monkeypatch, lines):
    p = tmp_path / "mail_handoff.md"
    p.write_text("<!-- header -->\n" + "\n".join(lines) + "\n")
    monkeypatch.setattr(mail_handoff, "_path", lambda: p)
    monkeypatch.setattr(mail_handoff, "_structured_actions", lambda: None)
    return p


def _stamp(hours_ago=1):
    return (datetime.now() - timedelta(hours=hours_ago)).strftime("%Y-%m-%d %H:%M")


def test_legacy_never_expires_and_delivery_marks_surfaced(tmp_path, monkeypatch):
    p = _write(tmp_path, monkeypatch, [
        f"- [{_stamp(1)}] svar: Svar fra kari@kommune.no — status: unprocessed",
        "    - emne: SV: Velferdsteknologi",
        f"- [{_stamp(100)}] frist: gammel — status: unprocessed",
        f"- [{_stamp(2)}] intervju: INTERVJU: Rådgiver — status: processed 2026-07-09",
    ])
    entries = mail_handoff.pull_unprocessed()
    assert len(entries) == 2
    assert entries[0]["summary"].startswith("svar: Svar fra kari")
    assert entries[0]["details"] == ["emne: SV: Velferdsteknologi"]
    mail_handoff.mark_processed(entries)
    text = p.read_text()
    assert "svar: Svar fra kari@kommune.no — status: surfaced" in text
    assert "frist: gammel — status: surfaced" in text
    # The compatibility hook never creates a new processed marker; historical
    # processed rows are preserved verbatim.
    assert "frist: gammel — status: processed" not in text

    repeated = mail_handoff.pull_unprocessed()
    assert [entry["summary"] for entry in repeated] == [
        "svar: Svar fra kari@kommune.no"
    ]


def test_format_lines(tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch,
           [f"- [{_stamp(1)}] bounce: Bounce: x@y.no — status: unprocessed",
            "    - beslutning: ny adresse eller Død?"])
    entries = mail_handoff.pull_unprocessed()
    out = mail_handoff.format_lines(entries)
    assert out == "- bounce: Bounce: x@y.no (beslutning: ny adresse eller Død?)"


def test_structured_actions_take_precedence_over_fallback(monkeypatch):
    payload = [{
        "id": 17,
        "priority": 0,
        "headline": "Interview tomorrow",
        "kind": "interview_invite",
        "details": ["09:00", "Teams"],
        "created_at": "2026-07-11T08:00:00Z",
        "surface_count": 2,
    }]
    monkeypatch.setattr(mail_handoff, "_run_cli", lambda *a, **kw: payload)
    monkeypatch.setattr(
        mail_handoff, "_pull_legacy",
        lambda: (_ for _ in ()).throw(AssertionError("must not read fallback")),
    )

    assert mail_handoff.pull_unprocessed() == [{
        "action_id": 17,
        "source": "structured",
        "stamp": "2026-07-11T08:00:00Z",
        "summary": "Interview tomorrow",
        "details": ["09:00", "Teams"],
        "kind": "interview_invite",
        "priority": 0,
        "attention_class": "push_now",
        "surface_count": 2,
        "options": [],
    }]


def test_structured_actions_passes_through_ask_user_options(monkeypatch):
    """Task 6: options_json is parsed by the owner CLI into 'options' — the
    normalized shape must carry it through so daily_brief's composer can
    render numbered questions. Malformed option entries are dropped."""
    payload = [{
        "id": 42,
        "priority": 0,
        "headline": "Feil adresse — send til ny kontakt?",
        "kind": "ask-user",
        "details": [],
        "created_at": "2026-07-12T09:00:00Z",
        "surface_count": 0,
        "decision": None,
        "options": [
            {"id": "a", "label": "ja, ny adresse"},
            {"id": "b", "label": "nei, dropp"},
            "not-a-dict",
        ],
    }]
    monkeypatch.setattr(mail_handoff, "_run_cli", lambda *a, **kw: payload)
    monkeypatch.setattr(
        mail_handoff, "_pull_legacy",
        lambda: (_ for _ in ()).throw(AssertionError("must not read fallback")),
    )
    entries = mail_handoff.pull_unprocessed()
    assert entries[0]["kind"] == "ask-user"
    assert entries[0]["attention_class"] == "push_now"  # legacy priority-0 row
    assert entries[0]["options"] == [
        {"id": "a", "label": "ja, ny adresse"},
        {"id": "b", "label": "nei, dropp"},
    ]


def test_structured_actions_preserve_explicit_attention_and_fail_closed(monkeypatch):
    payload = [
        {"id": 1, "priority": 0, "attention_class": "silent_hold"},
        {"id": 2, "priority": 0, "attention_class": "future_value"},
        {"id": 3, "priority": 2, "attention_class": "silent_file"},
    ]
    monkeypatch.setattr(mail_handoff, "_run_cli", lambda *a, **kw: payload)
    entries = mail_handoff.pull_unprocessed()
    assert [entry["attention_class"] for entry in entries] == [
        "silent_hold", "silent_hold", "silent_file",
    ]


def test_empty_structured_result_does_not_resurface_legacy(monkeypatch):
    monkeypatch.setattr(mail_handoff, "_run_cli", lambda *a, **kw: [])
    monkeypatch.setattr(
        mail_handoff, "_pull_legacy",
        lambda: (_ for _ in ()).throw(AssertionError("must not read fallback")),
    )
    assert mail_handoff.pull_unprocessed() == []


def test_mark_surfaced_uses_owner_cli_and_never_acknowledges(monkeypatch):
    calls = []
    monkeypatch.setattr(
        mail_handoff, "_run_cli",
        lambda *args, **kwargs: calls.append((args, kwargs)) or True,
    )
    monkeypatch.setattr(mail_handoff, "_mark_legacy_surfaced", lambda entries: None)

    assert mail_handoff.mark_processed([
        {"action_id": 3, "source": "structured"},
        {"action_id": 9, "source": "structured"},
    ]) is True
    assert calls == [(('mark-surfaced', '3', '9'), {})]


def test_mark_delivered_passes_durable_receipt_to_owner(monkeypatch):
    calls = []

    def fake_invoke(*args):
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(mail_handoff, "_invoke_cli", fake_invoke)
    assert mail_handoff.mark_delivered(
        action_id=17,
        event_id=44,
        dedup_key="mail_decisions:owner:abc123",
        telegram_message_id=9001,
    )
    assert calls == [(
        "mark-delivered", "17",
        "--event-id", "44",
        "--dedup-key", "mail_decisions:owner:abc123",
        "--telegram-message-id", "9001",
    )]


def test_mark_delivered_legacy_fallback_only_when_subcommand_missing(monkeypatch):
    monkeypatch.setattr(
        mail_handoff,
        "_invoke_cli",
        lambda *args: SimpleNamespace(
            returncode=2,
            stdout="",
            stderr="argument command: invalid choice: 'mark-delivered'",
        ),
    )
    surfaced = []
    monkeypatch.setattr(
        mail_handoff,
        "mark_surfaced",
        lambda entries: surfaced.append(entries) or True,
    )
    assert mail_handoff.mark_delivered(
        action_id=17,
        event_id=44,
        dedup_key="mail_decisions:legacy:17",
        telegram_message_id=None,
    )
    assert surfaced == [[{"action_id": 17}]]


def test_mark_delivered_write_failure_stays_pending(monkeypatch):
    monkeypatch.setattr(
        mail_handoff,
        "_invoke_cli",
        lambda *args: SimpleNamespace(
            returncode=1, stdout="", stderr="database is locked"
        ),
    )
    monkeypatch.setattr(
        mail_handoff,
        "mark_surfaced",
        lambda entries: (_ for _ in ()).throw(
            AssertionError("must not downgrade a real receipt failure")
        ),
    )
    assert not mail_handoff.mark_delivered(
        action_id=17,
        event_id=44,
        dedup_key="mail_decisions:legacy:17",
        telegram_message_id=9001,
    )


def test_ack_resolve_and_snooze_use_owner_cli(monkeypatch):
    calls = []
    monkeypatch.setattr(
        mail_handoff, "_run_cli",
        lambda *args, **kwargs: calls.append(args) or True,
    )

    assert mail_handoff.acknowledge(4)
    assert mail_handoff.resolve(4, "handled")
    assert mail_handoff.snooze(4, "2026-07-12T10:00:00Z")
    assert calls == [
        ("acknowledge", "4"),
        ("resolve", "4", "--note", "handled"),
        ("snooze", "4", "2026-07-12T10:00:00Z"),
    ]


def test_cli_failure_activates_fallback(monkeypatch):
    monkeypatch.setattr(mail_handoff, "_run_cli", lambda *a, **kw: None)
    monkeypatch.setattr(mail_handoff, "_pull_legacy", lambda: [{"summary": "fallback"}])
    assert mail_handoff.pull_unprocessed() == [{"summary": "fallback"}]


def test_decide_success_returns_true_and_row(monkeypatch):
    row = {"id": 42, "decision": "a", "options_json": "[]", "details_json": "[]"}

    def fake_invoke(*args):
        assert args == ("decide", "42", "--option", "a")
        return SimpleNamespace(returncode=0, stdout=json.dumps(row), stderr="")

    monkeypatch.setattr(mail_handoff, "_invoke_cli", fake_invoke)
    ok, result = mail_handoff.decide(42, "a")
    assert ok is True
    assert result == row


def test_decide_passes_note_flag_only_when_provided(monkeypatch):
    calls = []

    def fake_invoke(*args):
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout="{}", stderr="")

    monkeypatch.setattr(mail_handoff, "_invoke_cli", fake_invoke)
    mail_handoff.decide(42, "a")
    mail_handoff.decide(42, "a", note="fordi bruker sa det")
    assert calls == [
        ("decide", "42", "--option", "a"),
        ("decide", "42", "--option", "a", "--note", "fordi bruker sa det"),
    ]


def test_decide_rejected_transition_returns_false_and_message(monkeypatch):
    """The owner CLI's cmd_decide raises SystemExit(bokmål message) on an
    invalid transition (non-zero exit, message on stderr) — decide() must
    surface that message, not silently collapse to None like _run_cli does,
    and must not raise/retry."""
    def fake_invoke(*args):
        return SimpleNamespace(
            returncode=1, stdout="",
            stderr="Handling 42 har allerede fått en beslutning: 'a'",
        )

    monkeypatch.setattr(mail_handoff, "_invoke_cli", fake_invoke)
    ok, message = mail_handoff.decide(42, "b")
    assert ok is False
    assert "allerede fått en beslutning" in message


def test_decide_cli_unavailable_returns_false_without_raising(monkeypatch):
    monkeypatch.setattr(mail_handoff, "_invoke_cli", lambda *a: None)
    ok, message = mail_handoff.decide(42, "a")
    assert ok is False
    assert isinstance(message, str) and message


def test_owner_cli_runs_via_python_without_shell(tmp_path, monkeypatch):
    cli = tmp_path / "mail_actions_cli.py"
    cli.write_text("# intentionally not executable\n")
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout="[]", stderr="")

    monkeypatch.setattr(mail_handoff, "_cli_path", lambda: cli)
    monkeypatch.setattr(mail_handoff.subprocess, "run", fake_run)

    assert mail_handoff._run_cli("list", expect_json=True) == []
    argv, kwargs = calls[0]
    assert argv == [mail_handoff.sys.executable, str(cli), "list"]
    assert kwargs["cwd"] == str(tmp_path)
    assert kwargs["check"] is False
    assert "shell" not in kwargs
