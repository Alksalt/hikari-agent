"""DEPRECATED — use `uv run python -m scripts.auth google grant` instead.

This shim prints the migration notice and delegates to the new CLI.

Run directly as: uv run python scripts/setup_google_oauth.py
"""
from __future__ import annotations

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main() -> int:
    print("scripts/setup_google_oauth.py is deprecated.")
    print("use: uv run python -m scripts.auth google grant")
    print()
    print("delegating …")
    from scripts.auth import main as auth_main
    return auth_main(["google", "grant"])


if __name__ == "__main__":
    sys.exit(main())
