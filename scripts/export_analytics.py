"""Analytics verilerini CSV olarak export eder.

Kullanim:
  python scripts/export_analytics.py [--since 2026-01-01] [--until 2026-07-28] [--output analytics_export.csv]
"""

import sys
import csv
import argparse
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.core.database import db


def export_analytics(since: str | None, until: str | None, output: str):
    with db.cursor() as cur:
        query = "SELECT id, event_type, user_id, session_id, ticker, details, created_at FROM analytics_events WHERE 1=1"
        params = []

        if since:
            query += " AND created_at >= %s"
            params.append(since)
        if until:
            query += " AND created_at <= %s"
            params.append(until + " 23:59:59")

        query += " ORDER BY created_at ASC"

        cur.execute(query, params)
        rows = cur.fetchall()

    with open(output, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["id", "event_type", "user_id", "session_id", "ticker", "details", "created_at"])
        for row in rows:
            writer.writerow(row)

    print(f"Exported {len(rows)} events to {output}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Export analytics events to CSV")
    parser.add_argument("--since", help="Start date (YYYY-MM-DD)")
    parser.add_argument("--until", help="End date (YYYY-MM-DD)")
    parser.add_argument("--output", default="analytics_export.csv", help="Output CSV file")
    args = parser.parse_args()
    export_analytics(args.since, args.until, args.output)
