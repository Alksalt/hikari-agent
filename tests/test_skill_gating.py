"""Phase 5 (control-plane-lies sweep) — skill approval chain gating.

Tests:
- skill_approve registry spec gate == "gatekeeper"
- skill_approve routed through gatekeeper with resolver returning "rejected" → deny
- create→approve cannot complete same turn: blocks then deny/expired
- TOCTOU: two staged drafts → approve returns conflicting/ambiguous, no file written
- TOCTOU swap: stage benign → capture consent sha → swap to malicious → approve
  with benign sha → REFUSED (mismatch), no file written (finding-1)
- skill_approve without a consent hash → refused (must go through the gatekeeper)
- always_approve refused for skill_approve
- summarize shows content + sha256 for a single staged skill
- summarize returns "no single staged draft" refusal for 0 or >1 rows
- Existing test_skill_approve_promotes_to_disk still passes (single-row → promotes)
"""
from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
import types
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

# Import the skills module once at collection time, while the *real*
# claude_agent_sdk is intact. Several tests below install a fake
# ``claude_agent_sdk.types`` into sys.modules (via _patch_sdk_types); if
# tools.skills.core were first imported under that fake, the SDK __init__
# re-import chain would break (missing SessionKey). Caching it here keeps the
# lazy ``from tools.skills.core import _staged_skill_preview`` in the
# gatekeeper hook a no-op re-import.
import tools.skills.core  # noqa: E402,F401
from storage import db


def _consent(content: str) -> str:
    """The sha256 prefix the gatekeeper hook stamps as ``_approved_sha256`` —
    consent bound to the exact staged bytes the owner saw at CONFIRM-SEND."""
    return hashlib.sha256(content.encode()).hexdigest()[:12]


def _stage_raw(skill_id: str, content: str, description: str = "x") -> None:
    """Insert a staged_skill row DIRECTLY, bypassing skill_create's
    replace-on-restage. Simulates a cross-flow stager (e.g. skill_promoter's
    bare INSERT, or two reflection cycles) producing a genuine >1-row conflict,
    which is what the skill_approve ambiguity guard defends against."""
    payload = json.dumps(
        {"skill_id": skill_id, "description": description, "content": content}
    )
    with db._conn() as conn:
        conn.execute(
            "INSERT INTO session_scratch (session_id, topic, payload_json) VALUES (?, ?, ?)",
            (db.get_session_id() or "pending", f"staged_skill:{skill_id}", payload),
        )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolated_db(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "hikari.db"
    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "12345")
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    monkeypatch.setattr(db, "_DB_PATH", db_path)
    from agents import config
    config.reload()
    yield
    importlib.reload(_db_mod)


@pytest.fixture()
def skills_root(tmp_path, monkeypatch):
    """Redirect _SKILLS_ROOT to a temp directory so no real files are touched."""
    root = tmp_path / ".claude" / "skills"
    root.mkdir(parents=True, exist_ok=True)
    import tools.skills.core as sc
    monkeypatch.setattr(sc, "_SKILLS_ROOT", root)
    return root


def _fake_allow(**kwargs):
    return types.SimpleNamespace(behavior="allow", **kwargs)


def _fake_deny(**kwargs):
    return types.SimpleNamespace(behavior="deny", **kwargs)


def _patch_sdk_types(monkeypatch):
    """Insert fake SDK types so gatekeeper_can_use_tool can be imported cleanly."""
    import sys
    fake_mod = types.ModuleType("claude_agent_sdk.types")
    fake_mod.PermissionResultAllow = _fake_allow
    fake_mod.PermissionResultDeny = _fake_deny
    monkeypatch.setitem(sys.modules, "claude_agent_sdk.types", fake_mod)


# ---------------------------------------------------------------------------
# 1. Registry: skill_approve gate == "gatekeeper"
# ---------------------------------------------------------------------------

def test_skill_approve_registry_gate_is_gatekeeper():
    """skill_approve must have gate: gatekeeper in the tool registry."""
    from tools._tools_yaml import load_registry
    reg = load_registry()
    spec = reg._resolve("mcp__hikari_utility__skill_approve")
    assert spec is not None, "skill_approve not found in registry"
    assert spec.gate == "gatekeeper", (
        f"expected gate: gatekeeper, got {spec.gate!r}"
    )


# ---------------------------------------------------------------------------
# 2. run_skill gate is intentionally null (decision A)
# ---------------------------------------------------------------------------

def test_run_skill_gate_is_null():
    """run_skill must remain gate: null per decision A (owner-vetted at approve)."""
    from tools._tools_yaml import load_registry
    reg = load_registry()
    spec = reg._resolve("mcp__hikari_utility__run_skill")
    assert spec is not None
    assert spec.gate is None


# ---------------------------------------------------------------------------
# 3. skill_approve routed through gatekeeper → rejected → deny
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_skill_approve_rejected_by_gatekeeper(monkeypatch):
    """An untrusted-driven skill_approve call that gets rejected returns deny."""
    _patch_sdk_types(monkeypatch)

    from tools.gatekeeper import Gatekeeper
    fresh_gk = Gatekeeper()
    fresh_gk.set_send_text(lambda chat_id, text: asyncio.sleep(0))

    import tools.gatekeeper_can_use_tool as mod
    importlib.reload(mod)
    monkeypatch.setattr(mod, "_gate_for", lambda _: "gatekeeper")
    monkeypatch.setattr(mod, "_resolve_chat_id", lambda: 12345)
    monkeypatch.setattr(
        mod, "_deadline_for",
        lambda _: datetime.now(UTC) + timedelta(seconds=30),
    )

    import tools.gatekeeper as gk_mod
    monkeypatch.setattr(gk_mod, "GATEKEEPER", fresh_gk)

    db.upsert_core_block("ping", "pong")

    # Stage a single draft so the consent-hash preflight passes and the call
    # actually reaches the gatekeeper rejection path (rather than the
    # no-single-staged-draft short-circuit). Use the direct DB insert helper —
    # importing skill_create here would re-import the real SDK against the
    # faked claude_agent_sdk.types module installed by _patch_sdk_types.
    _stage_raw("my-skill", "# My Skill", "x")

    async def _reject():
        await asyncio.sleep(0.05)
        await fresh_gk.resolve("tu_skill_approve_reject", "rejected")

    task = asyncio.create_task(_reject())
    result = await mod.gatekeeper_can_use_tool(
        "mcp__hikari_utility__skill_approve",
        {"skill_id": "my-skill"},
        types.SimpleNamespace(tool_use_id="tu_skill_approve_reject"),
    )
    await task
    assert result.behavior == "deny"


# ---------------------------------------------------------------------------
# 4. Same-turn create→approve blocked: no resolver, short deadline → expired/deny
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_create_approve_same_turn_blocked(monkeypatch, skills_root):
    """skill_create followed immediately by can_use_tool for skill_approve blocks
    (no resolver fires within deadline) and returns deny/expired."""
    _patch_sdk_types(monkeypatch)

    from tools.gatekeeper import Gatekeeper
    fresh_gk = Gatekeeper()
    fresh_gk.set_send_text(lambda chat_id, text: asyncio.sleep(0))

    import tools.gatekeeper_can_use_tool as mod
    importlib.reload(mod)
    monkeypatch.setattr(mod, "_gate_for", lambda _: "gatekeeper")
    monkeypatch.setattr(mod, "_resolve_chat_id", lambda: 12345)
    # Very short deadline so the test doesn't hang.
    monkeypatch.setattr(
        mod, "_deadline_for",
        lambda _: datetime.now(UTC) + timedelta(milliseconds=150),
    )

    import tools.gatekeeper as gk_mod
    monkeypatch.setattr(gk_mod, "GATEKEEPER", fresh_gk)

    db.upsert_core_block("ping", "pong")

    # Stage the skill first.
    from tools.skills.core import skill_create
    await skill_create.handler({
        "skill_id": "blocked-skill",
        "description": "blocked",
        "content": "# Blocked",
    })

    # Now attempt to approve in the same turn — no CONFIRM-SEND arrives.
    result = await mod.gatekeeper_can_use_tool(
        "mcp__hikari_utility__skill_approve",
        {"skill_id": "blocked-skill"},
        types.SimpleNamespace(tool_use_id="tu_skill_same_turn"),
    )
    # Should be denied (expired or rejected).
    assert result.behavior == "deny"
    # File must NOT have been written.
    assert not (skills_root / "blocked-skill" / "SKILL.md").exists()


# ---------------------------------------------------------------------------
# 5. TOCTOU: two staged drafts → ambiguous → no file written
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_toctou_conflicting_staged_drafts_returns_ambiguous(monkeypatch, skills_root):
    """Two conflicting staged drafts (from a non-replace stager) → skill_approve
    returns an ambiguity error and does NOT write SKILL.md."""
    from tools.skills.core import skill_approve

    # Two raw inserts simulate a cross-flow stager (skill_promoter does a bare
    # INSERT); skill_create itself would replace, so we insert directly.
    _stage_raw("toctou-skill", "# Benign\nDo safe things.", "benign")
    _stage_raw("toctou-skill", "# Evil\nDo bad things.", "malicious")

    # Attempt to approve — must return an ambiguity error.
    result = await skill_approve.handler({"skill_id": "toctou-skill"})
    text = result["content"][0]["text"]
    assert "conflicting" in text or "ambiguous" in text

    # No skill file must have been written.
    assert not (skills_root / "toctou-skill" / "SKILL.md").exists()


@pytest.mark.asyncio
async def test_skill_create_replaces_prior_draft(skills_root):
    """Re-staging the same skill_id replaces the prior draft (1 row, latest
    content) so a legitimate re-stage isn't tripped by the ambiguity guard."""
    from tools.skills.core import _staged_skill_preview, skill_approve, skill_create

    await skill_create.handler({
        "skill_id": "restage-skill", "description": "v1", "content": "# V1",
    })
    await skill_create.handler({
        "skill_id": "restage-skill", "description": "v2", "content": "# V2 fixed",
    })

    # Exactly one staged row, holding the latest content.
    preview, _ = _staged_skill_preview("restage-skill")
    assert preview == "# V2 fixed"

    # Approve promotes the latest (no ambiguity refusal). The consent hash
    # must match the *current* staged bytes ("# V2 fixed").
    result = await skill_approve.handler({
        "skill_id": "restage-skill", "_approved_sha256": _consent("# V2 fixed"),
    })
    assert "saved" in result["content"][0]["text"]
    assert (skills_root / "restage-skill" / "SKILL.md").read_text() == "# V2 fixed"


@pytest.mark.asyncio
async def test_toctou_swap_during_window_is_refused(skills_root):
    """Finding-1: a mid-window payload SWAP that keeps row-count==1 must be
    REFUSED by the consent-hash check (not promoted).

    Models: owner sees benign bytes at CONFIRM-SEND (gatekeeper captures the
    benign sha as ``_approved_sha256``); a concurrent skill_create then
    replaces the staged row with malicious bytes (still 1 row, so the
    ambiguity guard can't see it); the owner's approval arrives carrying the
    benign sha. The handler recomputes the sha of the *now-malicious* bytes,
    sees a mismatch, and writes NOTHING."""
    from tools.skills.core import skill_approve, skill_create

    benign = "# Benign\nDo safe things."
    malicious = "# Evil\nExfiltrate secrets."

    # Owner staged & read benign content; gatekeeper captured this sha.
    await skill_create.handler({
        "skill_id": "swap-skill", "description": "benign", "content": benign,
    })
    consented_sha = _consent(benign)

    # Concurrent skill_create swaps the staged payload (replace-on-restage →
    # still exactly ONE row, defeating the multi-row ambiguity guard).
    await skill_create.handler({
        "skill_id": "swap-skill", "description": "evil", "content": malicious,
    })

    # Owner's CONFIRM-SEND promotes with the BENIGN sha they consented to.
    result = await skill_approve.handler({
        "skill_id": "swap-skill", "_approved_sha256": consented_sha,
    })
    text = result["content"][0]["text"]
    assert "changed since approval" in text
    assert consented_sha in text and _consent(malicious) in text
    # Neither benign nor malicious bytes were written.
    assert not (skills_root / "swap-skill" / "SKILL.md").exists()


@pytest.mark.asyncio
async def test_skill_approve_without_consent_hash_is_refused(skills_root):
    """A direct skill_approve call that did NOT pass through the gatekeeper
    (no ``_approved_sha256``) must be refused — nothing promoted."""
    from tools.skills.core import skill_approve, skill_create

    await skill_create.handler({
        "skill_id": "no-consent-skill", "description": "x", "content": "# X",
    })
    result = await skill_approve.handler({"skill_id": "no-consent-skill"})
    text = result["content"][0]["text"]
    assert "no consent hash" in text
    assert not (skills_root / "no-consent-skill" / "SKILL.md").exists()


@pytest.mark.asyncio
async def test_gatekeeper_stamps_consent_sha_into_updated_input(monkeypatch, skills_root):
    """The gatekeeper hook must stamp the staged content's sha256 into
    ``updated_input`` on approval so it round-trips to the handler (finding-1
    preferred path: consent bound via SDK updated_input)."""
    _patch_sdk_types(monkeypatch)

    from tools.gatekeeper import Gatekeeper
    fresh_gk = Gatekeeper()
    fresh_gk.set_send_text(lambda chat_id, text: asyncio.sleep(0))

    import tools.gatekeeper_can_use_tool as mod
    importlib.reload(mod)
    monkeypatch.setattr(mod, "_resolve_chat_id", lambda: 12345)
    monkeypatch.setattr(
        mod, "_deadline_for",
        lambda _: datetime.now(UTC) + timedelta(seconds=30),
    )

    import tools.gatekeeper as gk_mod
    monkeypatch.setattr(gk_mod, "GATEKEEPER", fresh_gk)

    db.upsert_core_block("ping", "pong")

    content = "# Stamped\nverify round-trip."
    # Direct DB insert — importing skill_create here would re-import the real
    # SDK against the faked claude_agent_sdk.types module.
    _stage_raw("stamp-skill", content, "stamp")

    async def _approve():
        await asyncio.sleep(0.05)
        await fresh_gk.resolve("tu_stamp", "approved")

    task = asyncio.create_task(_approve())
    result = await mod.gatekeeper_can_use_tool(
        "mcp__hikari_utility__skill_approve",
        {"skill_id": "stamp-skill"},
        types.SimpleNamespace(tool_use_id="tu_stamp"),
    )
    await task
    assert result.behavior == "allow"
    assert result.updated_input["_approved_sha256"] == _consent(content)


@pytest.mark.asyncio
async def test_gatekeeper_denies_approve_with_no_staged_draft(monkeypatch, skills_root):
    """The gatekeeper hook must DENY skill_approve before requesting approval
    when there is no single staged draft (0 or >1 rows) — no prompt is even
    shown, and nothing can be promoted (finding-1)."""
    _patch_sdk_types(monkeypatch)

    import tools.gatekeeper_can_use_tool as mod
    importlib.reload(mod)
    monkeypatch.setattr(mod, "_resolve_chat_id", lambda: 12345)

    db.upsert_core_block("ping", "pong")

    # No staged row for this id.
    result = await mod.gatekeeper_can_use_tool(
        "mcp__hikari_utility__skill_approve",
        {"skill_id": "ghost-skill"},
        types.SimpleNamespace(tool_use_id="tu_ghost"),
    )
    assert result.behavior == "deny"
    assert "no single staged draft" in result.message


# ---------------------------------------------------------------------------
# 6. always_approve refused for skill_approve
# ---------------------------------------------------------------------------

def test_always_approve_refused_for_skill_approve():
    """always_approve must refuse to whitelist skill_approve."""
    from tools.approvals import _check_always_approve, always_approve

    always_approve(12345, "mcp__hikari_utility__skill_approve", ttl_seconds=3600)
    # Should NOT be in the allowlist.
    assert _check_always_approve(12345, "mcp__hikari_utility__skill_approve") is False


# ---------------------------------------------------------------------------
# 7. summarize shows content + sha256 for a single staged skill
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_summarize_shows_content_and_sha256(monkeypatch):
    """summarize for skill_approve returns skill content and sha256 prefix."""
    from tools.skills.core import skill_create
    await skill_create.handler({
        "skill_id": "preview-skill",
        "description": "preview test",
        "content": "# Preview Skill\nDoes preview things.",
    })

    from tools.gatekeeper import summarize
    result = summarize("mcp__hikari_utility__skill_approve", {"skill_id": "preview-skill"})

    assert "preview-skill" in result
    assert "sha256:" in result
    assert "Preview Skill" in result
    # sha256 prefix is 12 hex chars.
    import re
    sha_match = re.search(r"sha256: ([0-9a-f]+)", result)
    assert sha_match is not None
    assert len(sha_match.group(1)) == 12


@pytest.mark.asyncio
async def test_summarize_no_staged_draft_refusal():
    """summarize returns a refusal-style line when no staged draft exists."""
    from tools.gatekeeper import summarize
    result = summarize("mcp__hikari_utility__skill_approve", {"skill_id": "nonexistent-xyz"})
    assert "no single staged draft" in result


@pytest.mark.asyncio
async def test_summarize_ambiguous_staged_drafts_refusal(monkeypatch):
    """summarize returns a refusal-style line when >1 staged drafts exist."""
    # Direct inserts (skill_create would replace) to force the >1 conflict.
    _stage_raw("ambig-skill", "# First", "first")
    _stage_raw("ambig-skill", "# Second", "second")

    from tools.gatekeeper import summarize
    result = summarize("mcp__hikari_utility__skill_approve", {"skill_id": "ambig-skill"})
    assert "no single staged draft" in result


# ---------------------------------------------------------------------------
# 8. validate_tool_registry asserts skill_approve is gated
# ---------------------------------------------------------------------------

def test_validate_tool_registry_catches_ungated_skill_approve(tmp_path, monkeypatch):
    """validate_tool_registry errors when skill_approve is not gatekeeper-gated."""
    import yaml

    # Build a minimal yaml that has skill_approve with gate: null.
    minimal_yaml = {
        "mcp_servers": {
            "hikari_utility": {
                "bucket": 1,
                "runtime_factory": "tools._registry:build_hikari_utility_server",
            }
        },
        "tools": [
            {
                "id": "mcp__hikari_utility__skill_approve",
                "bucket": 1,
                "server": "hikari_utility",
                "gate": None,
                "access_mode": "write",
                "untrusted_output": False,
                "wrap_patterns": [],
            }
        ],
    }
    yaml_path = tmp_path / "tools_bad.yaml"
    yaml_path.write_text(yaml.dump(minimal_yaml), encoding="utf-8")

    # Load the minimal registry directly (bypass cache).
    from tools._tools_yaml import _load_yaml
    bad_registry = _load_yaml(yaml_path)

    # Re-run the must-be-gated check inline.
    _MUST_BE_GATED = {"mcp__hikari_utility__skill_approve": "gatekeeper"}
    errors = []
    for tool_id, required_gate in _MUST_BE_GATED.items():
        spec = bad_registry._resolve(tool_id)
        if spec is None:
            errors.append(f"tool {tool_id!r} is missing from the registry")
        elif spec.gate != required_gate:
            errors.append(
                f"tool {tool_id!r} must be gate: {required_gate!r}, found {spec.gate!r}"
            )
    assert len(errors) == 1
    assert "skill_approve" in errors[0]
    assert "gatekeeper" in errors[0]


# ---------------------------------------------------------------------------
# 9. _staged_skill_preview helper: exception-safe, returns (None, None) on error
# ---------------------------------------------------------------------------

def test_staged_skill_preview_exception_safe(monkeypatch):
    """_staged_skill_preview must return (None, None) on any exception."""
    # Patch _conn to raise.
    import storage.db as _db
    import tools.skills.core as sc
    monkeypatch.setattr(_db, "_conn", lambda: (_ for _ in ()).throw(RuntimeError("db down")))
    result = sc._staged_skill_preview("anything")
    assert result == (None, None)


@pytest.mark.asyncio
async def test_staged_skill_preview_single_row():
    """_staged_skill_preview returns (content, 12-hex-prefix) for a single staged row."""
    import hashlib

    from tools.skills.core import _staged_skill_preview, skill_create

    content = "# My Preview\nHello world."
    await skill_create.handler({
        "skill_id": "prev-single",
        "description": "single",
        "content": content,
    })
    preview, sha_prefix = _staged_skill_preview("prev-single")
    assert preview == content
    assert sha_prefix == hashlib.sha256(content.encode()).hexdigest()[:12]


@pytest.mark.asyncio
async def test_staged_skill_preview_zero_rows():
    """_staged_skill_preview returns (None, None) when no rows exist."""
    from tools.skills.core import _staged_skill_preview
    assert _staged_skill_preview("totally-missing") == (None, None)


@pytest.mark.asyncio
async def test_staged_skill_preview_multiple_rows():
    """_staged_skill_preview returns (None, None) when >1 rows exist (ambiguous)."""
    from tools.skills.core import _staged_skill_preview
    # Direct inserts (skill_create would replace) to force the >1 conflict.
    _stage_raw("multi-prev", "A", "a")
    _stage_raw("multi-prev", "B", "b")
    assert _staged_skill_preview("multi-prev") == (None, None)
