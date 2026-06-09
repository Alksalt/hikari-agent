"""Controls feature — in-process tools that write runtime-state switches.

Three tools: ``set_silence``, ``set_proactive_source``, ``checkin_control``.
All are gate: null — local state writes with no destructive side-effects.
"""
from __future__ import annotations

from tools.controls.checkin import checkin_control
from tools.controls.proactive import set_proactive_source
from tools.controls.silence import set_silence

ALL_TOOLS = [set_silence, set_proactive_source, checkin_control]
