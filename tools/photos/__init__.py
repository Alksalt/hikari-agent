"""Photos feature — manifest.

DEDICATED MCP SERVER. ``agents/runtime.py`` does
``from tools import photos as photo_tools`` and registers
``photo_tools.ALL_TOOLS`` against an in-process ``hikari_photo`` server.
The shared registry skips ``photos`` on purpose (see
``tools/_registry.py:_DEDICATED_SERVER_MODULES``) so this package is
NOT auto-discovered into the utility server. Keep ``ALL_TOOLS``
accessible at ``tools.photos.ALL_TOOLS``.

Re-exports: the module-level constants and private helpers from
``_shared.py`` so existing callers (``agents/telegram_bridge.py`` imports
``OUTBOX``; tests reload the package and call ``photos.generate_photo``)
keep working through the package namespace. ``httpx`` is also re-exported
because integration tests may monkey-patch ``photos.httpx`` to stub the
OpenRouter call.
"""
from __future__ import annotations

import httpx  # noqa: F401 — re-exported for tests that patch ``photos.httpx``

from tools.photos._shared import (  # noqa: F401 — back-compat re-exports
    APPEARANCE_MD,
    DAILY_CAP,
    DEFAULT_MODEL,
    OPENROUTER_IMG_URL,
    OUTBOX,
    REPO_ROOT,
    _FALLBACK_SCENES,
    _SCENES_BY_MOOD,
    _call_flux,
    _photos_sent_today,
    _read_appearance_base,
    _record_photo_sent,
    _resolve_mood,
    _scene_suffix,
)
from tools.photos.generate import generate_photo

ALL_TOOLS = [generate_photo]
