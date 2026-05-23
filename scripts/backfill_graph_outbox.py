"""Backfill graph_outbox with facts that have no corresponding outbox row.

Idempotent: the UNIQUE INDEX on (source_table, source_id) ensures re-running
this script never creates duplicate rows. Safe to run against a live DB.

Usage:
    uv run python scripts/backfill_graph_outbox.py [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

# Ensure the repo root is on sys.path when run directly.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from storage import db  # noqa: E402


def _iso_now() -> str:
    return datetime.now(UTC).isoformat()


def backfill(dry_run: bool = False) -> int:
    """Insert pending outbox rows for every fact not yet in graph_outbox.

    Returns the number of rows inserted (or that would be inserted in dry-run).
    """
    with db._conn() as c:
        facts = c.execute(
            "SELECT f.id, f.subject, f.predicate, f.object "
            "FROM facts f "
            "WHERE NOT EXISTS ("
            "  SELECT 1 FROM graph_outbox g "
            "  WHERE g.source_table='facts' AND g.source_id=f.id"
            ") "
            "ORDER BY f.id ASC"
        ).fetchall()

    if not facts:
        print("backfill: nothing to do — all facts already have outbox rows")
        return 0

    print(f"backfill: found {len(facts)} facts without outbox rows")
    if dry_run:
        for row in facts:
            print(f"  [dry-run] would insert fact_id={row['id']}: "
                  f"{row['subject']} {row['predicate']} {row['object'][:60]}")
        return len(facts)

    inserted = 0
    for row in facts:
        payload = {
            "v": 1,
            "name": f"fact_{row['id']}",
            "episode_body": f"{row['subject']} {row['predicate']} {row['object']}",
            "source": "text",
            "source_description": "fact (backfill)",
            "group_id": "hikari_chat",
            "reference_time": _iso_now(),
        }
        row_id = db.graph_outbox_insert("facts", row["id"], json.dumps(payload))
        if row_id is not None:
            inserted += 1

    print(f"backfill: inserted {inserted} outbox rows "
          f"({len(facts) - inserted} skipped / already present)")
    return inserted


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would be inserted without writing")
    args = parser.parse_args()
    count = backfill(dry_run=args.dry_run)
    sys.exit(0 if count >= 0 else 1)


if __name__ == "__main__":
    main()
