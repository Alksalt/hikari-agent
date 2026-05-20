"""arxiv_search feature — manifest.

Single-tool feature promoted to a folder for layout uniformity (see
``tools/README.md``). The ``arxiv`` SDK import is lazy — pushed inside
the handler body so importing this manifest at boot is cheap.
"""
from __future__ import annotations

from tools.arxiv_search.search import arxiv_search

ALL_TOOLS = [arxiv_search]
