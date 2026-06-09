"""Photos feature — inbound-only after Phase 3-C.

Outbound photo GENERATION (selfies, scene photos via OpenRouter Flux) has
been removed. What remains:

- ``tools.photos.classify`` — vision-classify inbound user photos so the
  runtime LLM picks the right downstream tool (used by
  ``agents/telegram_bridge.py:handle_photo``).
- ``OUTBOX`` — the ``data/photo_outbox/`` path constant re-exported here
  for the bridge's ``_drain_photo_outbox`` / ``_reconcile_photo_outbox_orphans``
  functions. The outbox is now only populated externally (legacy files); the
  agent no longer generates photos into it.
"""
from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
OUTBOX = Path(os.environ.get("HIKARI_PHOTO_OUTBOX") or REPO_ROOT / "data" / "photo_outbox")
