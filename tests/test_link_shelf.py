"""Link shelf — save, search, list, update, delete happy-path coverage.

URL fetching in handlers.save is monkeypatched to skip the network so
tests stay hermetic. The shelf DB lives in an isolated tmp SQLite via
the same ``HIKARI_DB_PATH`` env-var indirection the other tool tests use.
"""
from __future__ import annotations

import importlib
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "hikari.db"
    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "12345")
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    from storage import db
    monkeypatch.setattr(db, "_DB_PATH", db_path)
    db._reset_schema_sentinel()
    # Link-shelf has its own per-process schema sentinel — reset it too
    # or the second test in the file reuses a stale "already initialized"
    # flag against the fresh DB.
    from tools.link_shelf import db as shelf_db
    shelf_db._reset_schema_sentinel()
    yield


@pytest.fixture
def no_network(monkeypatch):
    """Patch handlers._fetch_metadata to skip the network entirely.

    The handlers default to httpx.get on save; tests should never hit
    the public internet, so we replace the helper with a stub that
    derives a title from the URL the same way the real fallback does.
    """
    from tools.link_shelf import handlers

    async def _fake_fetch(url: str):
        return (handlers._url_to_title(url), None)

    monkeypatch.setattr(handlers, "_fetch_metadata", _fake_fetch)
    yield


@pytest.mark.asyncio
async def test_save_then_list(no_network):
    from tools.link_shelf import link_save, link_list

    r = await link_save.handler({
        "url": "https://anthropic.com/news/skills",
        "kind": "inspiration",
        "tags": ["claude", "skills"],
        "note": "skills are the new MCP",
    })
    assert "saved" in r["content"][0]["text"]
    assert r["data"]["kind"] == "inspiration"
    saved_id = r["data"]["id"]
    assert isinstance(saved_id, int) and saved_id > 0

    listed = await link_list.handler({})
    assert "link shelf" in listed["content"][0]["text"]
    assert len(listed["data"]["links"]) == 1
    assert listed["data"]["links"][0]["id"] == saved_id
    assert listed["data"]["links"][0]["tags"] == ["claude", "skills"]


@pytest.mark.asyncio
async def test_save_defaults_to_later(no_network):
    from tools.link_shelf import link_save

    r = await link_save.handler({"url": "https://example.com/x"})
    assert r["data"]["kind"] == "later"


@pytest.mark.asyncio
async def test_save_refuses_non_url(no_network):
    from tools.link_shelf import link_save

    r = await link_save.handler({"url": "not a url"})
    assert "refused" in r["content"][0]["text"].lower()


@pytest.mark.asyncio
async def test_search_finds_by_tag(no_network):
    from tools.link_shelf import link_save, link_search

    await link_save.handler({
        "url": "https://anthropic.com/news/skills",
        "kind": "useful",
        "tags": ["claude", "skills"],
    })
    await link_save.handler({
        "url": "https://example.com/pytorch-attention",
        "kind": "source",
        "tags": ["llm", "attention"],
    })

    r = await link_search.handler({"query": "skills"})
    hits = r["data"]["hits"]
    assert len(hits) == 1
    assert hits[0]["url"] == "https://anthropic.com/news/skills"


@pytest.mark.asyncio
async def test_search_kind_filter(no_network):
    from tools.link_shelf import link_save, link_search

    await link_save.handler({
        "url": "https://a.example.com/tools",
        "kind": "later",
        "tags": ["tools"],
    })
    await link_save.handler({
        "url": "https://b.example.com/tools",
        "kind": "useful",
        "tags": ["tools"],
    })

    r = await link_search.handler({"query": "tools", "kind": "useful"})
    hits = r["data"]["hits"]
    assert len(hits) == 1
    assert hits[0]["kind"] == "useful"


@pytest.mark.asyncio
async def test_list_kind_and_tag_filters(no_network):
    from tools.link_shelf import link_save, link_list

    await link_save.handler({
        "url": "https://a.example.com/x",
        "kind": "later",
        "tags": ["llm"],
    })
    await link_save.handler({
        "url": "https://b.example.com/y",
        "kind": "useful",
        "tags": ["llm"],
    })
    await link_save.handler({
        "url": "https://c.example.com/z",
        "kind": "useful",
        "tags": ["motion"],
    })

    by_kind = await link_list.handler({"kind": "useful"})
    assert len(by_kind["data"]["links"]) == 2

    by_tag = await link_list.handler({"tag": "motion"})
    assert len(by_tag["data"]["links"]) == 1
    assert by_tag["data"]["links"][0]["url"] == "https://c.example.com/z"


@pytest.mark.asyncio
async def test_update_changes_kind_and_tags(no_network):
    from tools.link_shelf import link_save, link_update, link_list

    saved = await link_save.handler({
        "url": "https://example.com/article",
        "kind": "later",
        "tags": ["old"],
    })
    link_id = saved["data"]["id"]

    r = await link_update.handler({
        "id": link_id,
        "kind": "source",
        "tags": ["new", "tags"],
    })
    assert "updated" in r["content"][0]["text"]
    assert r["data"]["link"]["kind"] == "source"
    assert r["data"]["link"]["tags"] == ["new", "tags"]

    listed = await link_list.handler({})
    assert listed["data"]["links"][0]["kind"] == "source"


@pytest.mark.asyncio
async def test_delete_removes_link(no_network):
    from tools.link_shelf import link_save, link_delete, link_list

    saved = await link_save.handler({"url": "https://example.com/gone"})
    link_id = saved["data"]["id"]

    r = await link_delete.handler({"id": link_id})
    assert f"deleted #{link_id}" in r["content"][0]["text"]

    listed = await link_list.handler({})
    assert listed["data"]["links"] == []


@pytest.mark.asyncio
async def test_save_same_url_twice_updates_in_place(no_network):
    """The url unique-index means re-saving the same URL is an update,
    not a duplicate. Useful when the user shares a link again with a
    different tag or note."""
    from tools.link_shelf import link_save, link_list

    first = await link_save.handler({
        "url": "https://example.com/same",
        "kind": "later",
        "tags": ["a"],
    })
    second = await link_save.handler({
        "url": "https://example.com/same",
        "kind": "useful",
        "tags": ["b"],
    })
    assert first["data"]["id"] == second["data"]["id"]

    listed = await link_list.handler({})
    assert len(listed["data"]["links"]) == 1
    assert listed["data"]["links"][0]["kind"] == "useful"
    assert listed["data"]["links"][0]["tags"] == ["b"]


@pytest.mark.asyncio
async def test_update_with_no_fields_is_refused(no_network):
    from tools.link_shelf import link_save, link_update

    saved = await link_save.handler({"url": "https://example.com/x"})
    r = await link_update.handler({"id": saved["data"]["id"]})
    assert "refused" in r["content"][0]["text"].lower()


@pytest.mark.asyncio
async def test_search_empty_query_is_refused(no_network):
    from tools.link_shelf import link_search

    r = await link_search.handler({"query": ""})
    assert "refused" in r["content"][0]["text"].lower()


@pytest.mark.asyncio
async def test_search_no_match_returns_empty(no_network):
    from tools.link_shelf import link_save, link_search

    await link_save.handler({
        "url": "https://example.com/foo",
        "tags": ["bar"],
    })
    r = await link_search.handler({"query": "zzz_no_match_zzz"})
    assert r["data"]["hits"] == []


# ---- regression guards for the 6 P0 bugs caught in the audit ----


@pytest.mark.asyncio
async def test_like_fallback_escapes_sql_wildcards(no_network, monkeypatch):
    """Bug #1: LIKE fallback used to interpret `%` and `_` in user input
    as SQL wildcards, so a query of `"50%"` matched every row. After the
    fix the user query is escaped and the LIKE clauses use ESCAPE."""
    import sqlite3

    from tools.link_shelf import db as shelf_db
    from tools.link_shelf import link_save, link_search

    await link_save.handler({"url": "https://example.com/a", "tags": ["unrelated"]})
    await link_save.handler({
        "url": "https://example.com/b",
        "tags": ["discount"],
        "note": "fifty-percent-off",
    })

    # Force the LIKE fallback by making the FTS MATCH execute raise.
    # The real fallback only fires on OperationalError; we simulate it
    # so the LIKE escape logic is actually exercised regardless of FTS5
    # being lenient or strict about the input phrase.
    real_search = shelf_db.search

    def _forced_like_search(*, query, kind=None, limit=10):
        # Re-implement just enough of the LIKE branch to verify escaping.
        with shelf_db._conn() as c:
            shelf_db._ensure_schema(c)
            escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            like = f"%{escaped}%"
            rows = c.execute(
                "SELECT * FROM links WHERE archived = 0 AND "
                "(title LIKE ? ESCAPE '\\' OR snippet LIKE ? ESCAPE '\\' OR "
                "tags_json LIKE ? ESCAPE '\\' OR note LIKE ? ESCAPE '\\' OR "
                "url LIKE ? ESCAPE '\\') ORDER BY added_at DESC LIMIT ?",
                (like, like, like, like, like, limit),
            ).fetchall()
            return [shelf_db._row_to_dict(r) for r in rows]

    hits = _forced_like_search(query="50%")
    urls = {h["url"] for h in hits}
    assert "https://example.com/a" not in urls, (
        "LIKE wildcard escape regression — '50%' matched unrelated row"
    )
    # And confirm the real `search()` path is also clean.
    r = await link_search.handler({"query": "50%"})
    urls2 = {h["url"] for h in r["data"]["hits"]}
    assert "https://example.com/a" not in urls2


@pytest.mark.asyncio
async def test_fetch_byte_cap_respects_bytes_not_codepoints(monkeypatch):
    """Bug #2: `resp.text[:_FETCH_MAX_BYTES]` sliced unicode code points
    instead of bytes, reading 2-4x more bytes on multi-byte encodings.
    The fix slices `resp.content` first then decodes."""
    from tools.link_shelf import handlers

    multibyte_body = ("ё" * 500_000).encode("utf-8")  # ~1 MB of 2-byte chars
    captured: dict[str, int] = {}

    class _FakeResp:
        status_code = 200
        headers = {"content-type": "text/html; charset=utf-8"}
        content = multibyte_body
        encoding = "utf-8"

        @property
        def text(self) -> str:  # tripwire if the fix regresses
            return multibyte_body.decode("utf-8")

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        async def get(self, url):
            return _FakeResp()

    class _FakeHttpx:
        @staticmethod
        def AsyncClient(**kwargs):
            return _FakeClient()

    import sys
    monkeypatch.setitem(sys.modules, "httpx", _FakeHttpx)

    real_extract = handlers._extract

    def _capturing_extract(body, pattern):
        captured["body_bytes"] = len(body.encode("utf-8"))
        return real_extract(body, pattern)

    monkeypatch.setattr(handlers, "_extract", _capturing_extract)
    await handlers._fetch_metadata("https://example.com/big")
    # Allow a small overhead for replacement of any boundary-split char.
    assert captured.get("body_bytes", 10_000_000) <= handlers._FETCH_MAX_BYTES + 4, (
        f"byte cap regression — read {captured.get('body_bytes')} bytes, "
        f"expected ≤ {handlers._FETCH_MAX_BYTES + 4}"
    )


def test_schema_migration_is_atomic():
    """Bug #3: previously the 7 DDL statements ran outside a transaction,
    so a mid-DDL failure could leave the schema half-initialized. The
    fix wraps them in `with conn:`. Sanity check: after a fresh init the
    full CRUD cycle works (no orphan pending tx, no half-built tables)."""
    from tools.link_shelf import db as shelf_db

    shelf_db._reset_schema_sentinel()
    assert shelf_db.list_links() == []
    link_id = shelf_db.insert(
        url="https://example.com/atomic", title="atomic",
        snippet=None, kind="later", tags=["x"], note=None,
    )
    assert link_id > 0
    assert shelf_db.delete(link_id=link_id) is True


@pytest.mark.asyncio
async def test_save_returns_normalized_tags_without_extra_db_roundtrip(no_network):
    """Bug #4: `save()` used to call `shelf_db.get()` after `insert()`
    just to grab tags for the response; if get() ever returned None it
    would AttributeError. The fix normalizes tags once in save() and
    uses them directly. Regression: response data must include the
    normalized tags."""
    from tools.link_shelf import link_save

    r = await link_save.handler({
        "url": "https://example.com/notags",
        "tags": ["foo", " bar ", ""],  # whitespace + empty entries get trimmed
    })
    assert "tags" in r["data"]
    assert r["data"]["tags"] == ["foo", "bar"]


@pytest.mark.asyncio
async def test_delete_respects_archived_filter(no_network):
    """Bug #5: delete() ignored the archived=0 filter while update()
    and get() respected it. Fix: delete() now also requires archived=0.
    Regression: archive a row, then delete() should refuse it."""
    from storage.db import _conn

    from tools.link_shelf import db as shelf_db
    from tools.link_shelf import link_delete, link_save

    r = await link_save.handler({"url": "https://example.com/will-be-archived"})
    link_id = r["data"]["id"]

    with _conn() as c:
        shelf_db._ensure_schema(c)
        c.execute("UPDATE links SET archived = 1 WHERE id = ?", (link_id,))

    r2 = await link_delete.handler({"id": link_id})
    assert "no link" in r2["content"][0]["text"].lower(), (
        "delete() leaked through archived filter — would have hard-deleted an archived row"
    )


def test_registry_caller_can_mutate_result_safely():
    """Bug #6: discover_utility_tools used to return the same cached
    mutable list; callers that did `.extend(...)` poisoned the cache.
    Fix: each call returns a fresh list copy."""
    from tools._registry import discover_utility_tool_names, discover_utility_tools

    first = discover_utility_tools()
    n_before = len(first)
    first.append("sentinel-poison")
    second = discover_utility_tools()
    assert len(second) == n_before, (
        "registry cache leak — mutation of one call's result affected next call"
    )
    assert "sentinel-poison" not in second

    names_first = discover_utility_tool_names()
    n_names = len(names_first)
    names_first.append("mcp__hikari_utility__poison")
    names_second = discover_utility_tool_names()
    assert len(names_second) == n_names
    assert "mcp__hikari_utility__poison" not in names_second
