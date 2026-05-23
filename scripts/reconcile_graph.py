"""Read-only drift report: facts vs outbox sent rows, bucketed by day (last 30 days).

Prints a table:
  date        | facts | sent | drift% | abs_diff

"drift" here means facts inserted on that day that do NOT have a 'sent' outbox
row yet (i.e. still pending, failed, or skipped). A healthy system has drift ~0%.

Exit 0 always — this is a diagnostic tool, not a gate.

Usage:
    uv run python scripts/reconcile_graph.py [--days N]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from storage import db  # noqa: E402


def reconcile(days: int = 30) -> None:
    with db._conn() as c:
        # Facts created per calendar date (last N days).
        facts_by_date = {
            row["date"]: row["n"]
            for row in c.execute(
                "SELECT date(created_at) AS date, COUNT(*) AS n "
                "FROM facts "
                "WHERE created_at >= date('now', ?) "
                "GROUP BY 1 ORDER BY 1 DESC",
                (f"-{days} days",),
            ).fetchall()
        }

        # Sent outbox rows per date (facts table + processed_at date).
        # We join via source_id → facts.id to get the fact's created_at date.
        sent_by_date = {
            row["date"]: row["n"]
            for row in c.execute(
                "SELECT date(f.created_at) AS date, COUNT(*) AS n "
                "FROM graph_outbox g "
                "JOIN facts f ON f.id = g.source_id AND g.source_table = 'facts' "
                "WHERE g.status = 'sent' "
                "AND f.created_at >= date('now', ?) "
                "GROUP BY 1 ORDER BY 1 DESC",
                (f"-{days} days",),
            ).fetchall()
        }

    all_dates = sorted(set(facts_by_date) | set(sent_by_date), reverse=True)

    if not all_dates:
        print("reconcile: no data in the last", days, "days")
        return

    print(f"{'date':<12} {'facts':>6} {'sent':>6} {'drift%':>8} {'abs_diff':>9}")
    print("-" * 48)
    total_facts = total_sent = 0
    for date in all_dates:
        n_facts = facts_by_date.get(date, 0)
        n_sent = sent_by_date.get(date, 0)
        drift = n_facts - n_sent
        drift_pct = (drift / n_facts * 100) if n_facts else 0.0
        total_facts += n_facts
        total_sent += n_sent
        print(f"{date:<12} {n_facts:>6} {n_sent:>6} {drift_pct:>7.1f}% {drift:>9}")

    print("-" * 48)
    total_drift = total_facts - total_sent
    total_pct = (total_drift / total_facts * 100) if total_facts else 0.0
    print(f"{'TOTAL':<12} {total_facts:>6} {total_sent:>6} {total_pct:>7.1f}% {total_drift:>9}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=30,
                        help="Number of past days to include (default: 30)")
    args = parser.parse_args()
    reconcile(days=args.days)
    sys.exit(0)


if __name__ == "__main__":
    main()
