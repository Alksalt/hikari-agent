"""Personal Link Shelf — save URLs into kinds (later/useful/source/
inspiration) with zero feed, then search and resurface them.

Why this exists: the user reads a lot, saves things, never comes back.
The shelf is a write-mostly bucket designed so Hikari can pull a
relevant past link into the conversation when the topic resurfaces —
"i remember you sent me this".

This file is the *manifest only*. Heavy code (httpx for URL fetching,
DB writes, FTS search) lives in ``handlers.py`` and is loaded lazily
on first invocation via ``tools._lazy.lazy_tool``. The schema lives
in ``db.py`` and is bootstrapped on the first DB touch.
"""
from __future__ import annotations

from tools._lazy import lazy_tool

_IMPL = "tools.link_shelf.handlers"

link_save = lazy_tool(
    name="link_save",
    description=(
        "Save a URL to the user's personal link shelf. Use whenever the "
        "user shares a link — articles, papers, repos, docs, posts — "
        "even if they don't explicitly say 'save this'. The shelf is "
        "write-mostly: the point is to capture for later, not to read "
        "now. "
        "url: required, the link to save. "
        "kind: one of 'later' (i'll read it later), 'useful' (reference "
        "material), 'source' (citation / where i learned X), "
        "'inspiration' (something to come back to). Defaults to 'later' "
        "if unclear. "
        "tags: optional list of short topic tags (e.g. ['llm', 'tool-use', "
        "'mcp']). The user uses these to find things again later. "
        "note: optional one-line user note ('this is the better article on X')."
    ),
    schema={"url": str, "kind": str, "tags": list, "note": str},
    impl=f"{_IMPL}:save",
)

link_search = lazy_tool(
    name="link_search",
    description=(
        "Search the user's link shelf by topic / keyword. Use this "
        "PROACTIVELY when the conversation touches a topic the user "
        "may have saved something about — 'do we have anything on X?', "
        "'remember that article about Y?', or just whenever Hikari "
        "wants to resurface a relevant past link mid-conversation. "
        "query: plain text. Matches against title, snippet, tags, and "
        "note. "
        "kind: optional filter — 'later' / 'useful' / 'source' / "
        "'inspiration'. Omit to search all kinds. "
        "limit: max hits (default 10). "
        "Returns id, url, title, kind, tags, added_at for each match."
    ),
    schema={"query": str, "kind": str, "limit": int},
    impl=f"{_IMPL}:search",
)

link_list = lazy_tool(
    name="link_list",
    description=(
        "Browse the user's link shelf with no query — for 'what's in my "
        "later pile?' or 'show me everything tagged llm'. "
        "kind: optional filter ('later' / 'useful' / 'source' / "
        "'inspiration'). "
        "tag: optional single-tag filter. "
        "limit: max rows (default 20). Most recently added first."
    ),
    schema={"kind": str, "tag": str, "limit": int},
    impl=f"{_IMPL}:list_links",
)

link_update = lazy_tool(
    name="link_update",
    description=(
        "Update fields on an existing link. Use when the user changes "
        "their mind about a tag, kind, or note. "
        "id: required, returned by link_save / link_search / link_list. "
        "kind, tags, note: any subset of fields to overwrite (tags "
        "replaces, doesn't append)."
    ),
    schema={"id": int, "kind": str, "tags": list, "note": str},
    impl=f"{_IMPL}:update",
)

link_delete = lazy_tool(
    name="link_delete",
    description=(
        "Remove a link from the shelf permanently. Use when the user "
        "says 'forget that link' or 'remove the X article'. "
        "id: required, returned by link_save / link_search / link_list."
    ),
    schema={"id": int},
    impl=f"{_IMPL}:delete",
)

ALL_TOOLS = [link_save, link_search, link_list, link_update, link_delete]
