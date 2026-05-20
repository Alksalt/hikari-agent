"""YouTube Music feature — manifest.

One file per tool (``recent.py`` / ``search.py`` / ``library.py``);
shared client builder + track-shape normalizer live in ``_shared.py``.

Re-exports the ``_shared`` module and its two helpers so tests can
monkey-patch the canonical ``_client`` / ``_shape_track`` through this
package's namespace if needed. The handlers themselves look up
``_shared._client`` at call time, so patching
``tools.ytmusic._shared._client`` is the supported test seam.
"""
from __future__ import annotations

from tools.ytmusic import _shared  # noqa: F401 — re-export for test patching
from tools.ytmusic._shared import _client, _shape_track  # noqa: F401 — test deps
from tools.ytmusic.library import ytmusic_library
from tools.ytmusic.recent import ytmusic_recent
from tools.ytmusic.search import ytmusic_search

ALL_TOOLS = [ytmusic_recent, ytmusic_search, ytmusic_library]
