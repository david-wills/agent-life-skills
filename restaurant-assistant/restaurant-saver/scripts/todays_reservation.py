#!/usr/bin/env python3
"""Is there a restaurant reservation on the calendar today (or --date)?

Feeds the day-of brief. Emits the matching restaurant row when the place is
already tracked, so the agent can research it and post what to order.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import restaurant_common as rc  # noqa: E402
from calendar_lib import fetch_events, reservation_detail  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None, help="YYYY-MM-DD (default: today)")
    args = ap.parse_args()

    day = dt.date.fromisoformat(args.date) if args.date else rc.today()
    events = fetch_events(day - dt.timedelta(days=1), day + dt.timedelta(days=2))
    found = reservation_detail(events, day)
    if not found:
        print(json.dumps({"has_reservation": False, "date": day.isoformat()}))
        return 0

    name = found["name"]
    conn = rc.connect()
    row = rc.find_restaurant(conn, name)
    conn.close()
    print(json.dumps({
        "has_reservation": True,
        "date": day.isoformat(),
        "name": name,
        "time": found["time"],
        "starts_at": found["starts_at"],
        "tracked": row is not None,
        "restaurant": dict(row) if row else None,
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
