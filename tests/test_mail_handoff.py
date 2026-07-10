from datetime import datetime, timedelta
from pathlib import Path

from agents import config as cfg
from agents import mail_handoff


def _write(tmp_path, monkeypatch, lines):
    p = tmp_path / "mail_handoff.md"
    p.write_text("<!-- header -->\n" + "\n".join(lines) + "\n")
    monkeypatch.setattr(mail_handoff, "_path", lambda: p)
    return p


def _stamp(hours_ago=1):
    return (datetime.now() - timedelta(hours=hours_ago)).strftime("%Y-%m-%d %H:%M")


def test_pull_and_mark(tmp_path, monkeypatch):
    p = _write(tmp_path, monkeypatch, [
        f"- [{_stamp(1)}] svar: Svar fra kari@kommune.no — status: unprocessed",
        "    - emne: SV: Velferdsteknologi",
        f"- [{_stamp(100)}] frist: gammel — status: unprocessed",     # too old
        f"- [{_stamp(2)}] intervju: INTERVJU: Rådgiver — status: processed 2026-07-09",
    ])
    entries = mail_handoff.pull_unprocessed()
    assert len(entries) == 1
    assert entries[0]["summary"].startswith("svar: Svar fra kari")
    assert entries[0]["details"] == ["emne: SV: Velferdsteknologi"]
    mail_handoff.mark_processed(entries)
    text = p.read_text()
    assert "svar: Svar fra kari@kommune.no — status: processed" in text
    assert "frist: gammel — status: unprocessed" in text              # untouched


def test_format_lines(tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch,
           [f"- [{_stamp(1)}] bounce: Bounce: x@y.no — status: unprocessed",
            "    - beslutning: ny adresse eller Død?"])
    entries = mail_handoff.pull_unprocessed()
    out = mail_handoff.format_lines(entries)
    assert out == "- bounce: Bounce: x@y.no (beslutning: ny adresse eller Død?)"
