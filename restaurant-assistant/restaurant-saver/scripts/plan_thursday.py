#!/usr/bin/env python3
"""Wednesday planning input: free Thursdays + ranked candidates.

A data shovel. It does not choose — it hands the agent a scored shortlist and
the agent writes the copy and calls post_suggestions.py.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import re  # noqa: E402

import restaurant_common as rc  # noqa: E402
from calendar_lib import free_thursdays  # noqa: E402

# Categories that are on the Maps list but are not dinner-out candidates.
NON_DINNER = ("hang gliding", "flight school", "museum", "gym", "hotel")
# Dropped map pins land in the list as "34.110890, -118.41541" with cuisine "Note".
COORD_NAME = re.compile(r"^-?\d{1,3}\.\d+,\s*-?\d{1,3}\.\d+$")


def score(row, suggested_recently: set[int]) -> float:
    s = 0.0
    if row["reservation_url"]:
        s += 3.0                      # bookable end-to-end
    if row["rating"]:
        try:
            s += (float(row["rating"]) - 4.0) * 2.0
        except ValueError:
            pass
    dm = row["drive_minutes"]
    if dm is not None:
        s += 2.0 if dm <= 20 else (0.5 if dm <= 35 else -1.5)
    if row["note"]:
        s += 1.0                      # the user wrote down a reason
    if row["id"] in suggested_recently:
        s -= 6.0                      # don't re-offer the same places
    if not row["lat"]:
        s -= 2.0
    return s


def candidates(conn, limit: int) -> list[dict]:
    recent = {
        r["restaurant_id"]
        for r in conn.execute(
            "SELECT DISTINCT restaurant_id FROM suggestions "
            "WHERE posted_at > datetime('now', '-45 days')"
        )
    }
    rows = conn.execute(
        "SELECT * FROM restaurants WHERE status=? ORDER BY name", (rc.STATUS_WANT,)
    ).fetchall()
    scored = []
    for r in rows:
        blob = f"{r['name']} {r['cuisine'] or ''}".lower()
        if any(k in blob for k in NON_DINNER):
            continue
        if COORD_NAME.match(r['name'] or '') or (r['cuisine'] or '') == 'Note':
            continue
        scored.append((score(r, recent), r))
    scored.sort(key=lambda t: -t[0])

    out = []
    seen_cuisine: dict[str, int] = {}
    for sc, r in scored:
        # Spread the shortlist across cuisines rather than five Italian places.
        c = (r["cuisine"] or "?").lower()
        if seen_cuisine.get(c, 0) >= 2:
            continue
        seen_cuisine[c] = seen_cuisine.get(c, 0) + 1
        out.append({
            "id": r["id"], "name": r["name"], "address": r["address"],
            "cuisine": r["cuisine"], "rating": r["rating"], "price": r["price"],
            "note": r["note"], "drive_minutes": r["drive_minutes"],
            "drive_text": r["drive_text"], "maps_url": r["maps_url"],
            "reservation_platform": r["reservation_platform"],
            "reservation_url": r["reservation_url"],
            "added_at": r["added_at"], "score": round(sc, 2),
        })
        if len(out) >= limit:
            break
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--weeks", type=int, default=4)
    ap.add_argument("--limit", type=int, default=8)
    args = ap.parse_args()

    conn = rc.connect()
    payload = {
        "thursdays": free_thursdays(args.weeks),
        "candidates": candidates(conn, args.limit),
    }
    conn.close()
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
