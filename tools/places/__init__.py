"""Places feature — manifest.

One file per tool (``search.py`` / ``open_now.py``), shared Overpass
query builder + HTTP fetch in ``_shared.py``.

Re-exports ``_places_search_impl`` because ``place_open_now`` calls it
directly (cross-tool sharing through the helper module, not through
``__init__``) — listed here purely so it's discoverable when grepping
for the public surface of this package.
"""
from __future__ import annotations

from tools.places._shared import _places_search_impl  # noqa: F401 — public helper
from tools.places.open_now import place_open_now
from tools.places.search import places_search

ALL_TOOLS = [places_search, place_open_now]
