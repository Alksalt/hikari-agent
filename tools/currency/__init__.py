"""Currency feature — manifest.

Single-tool feature kept as a folder for repo uniformity (see
``tools/README.md``). Re-exports ``currency_convert`` so tests and the
registry can import it via ``tools.currency``.
"""
from __future__ import annotations

from tools.currency.convert import currency_convert

ALL_TOOLS = [currency_convert]
