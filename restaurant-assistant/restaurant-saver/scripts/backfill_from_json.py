#!/usr/bin/env python3
"""One-time migration: want-to-go.json (v1 cache) -> restaurants.db.

Reuses a single browser context across every place; enriching 50+ places with
one Playwright launch each was the difference between minutes and an hour.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import restaurant_common as rc  # noqa: E402
from browser import maps_context  # noqa: E402
from enrich_place import drive_from_home, place_details  # noqa: E402

CACHE = rc.WORKSPACE / "restaurant-saver" / "want-to-go.json"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--no-enrich", action="store_true",
                    help="Import names/addresses only; skip the slow browser pass")
    args = ap.parse_args()

    places = json.loads(CACHE.read_text())["places"]
    if args.limit:
        places = places[: args.limit]
    conn = rc.connect()

    # Pass 1 — cheap: everything the v1 cache already knows.
    ids: list[tuple[int, str, str]] = []
    for p in places:
        category = (p.get("category") or "").strip()
        closed = "permanently closed" in category.lower()
        lat, lng = p.get("lat"), p.get("lng")
        if lat is None:
            lat, lng = rc.coords_from_url(p.get("url", ""))
        row = {
            "name": p.get("name"),
            "address": p.get("address", ""),
            "lat": lat,
            "lng": lng,
            "maps_url": p.get("url"),
            "cuisine": "" if closed else category,
            "price": p.get("price"),
            "rating": p.get("rating"),
            "review_count": p.get("review_count"),
            "note": p.get("note", ""),
            "source_raw": "migrated from want-to-go.json",
            "status": "closed" if closed else rc.STATUS_WANT,
            "maps_saved": 1,
        }
        if not row["name"]:
            continue
        rid, created = rc.upsert_restaurant(conn, row)
        print(f"{'+' if created else '=':2s} {row['name']}", file=sys.stderr)
        if not closed:
            ids.append((rid, row["name"], row["maps_url"] or ""))

    if args.no_enrich:
        conn.close()
        print(json.dumps({"imported": len(places), "enriched": 0}))
        return 0

    # Pass 2 — expensive: drive time + reservation link, one context for all.
    enriched = 0
    with maps_context(headless=True) as ctx:
        for i, (rid, name, url) in enumerate(ids, 1):
            existing = conn.execute(
                "SELECT drive_minutes, reservation_url FROM restaurants WHERE id=?", (rid,)
            ).fetchone()
            if existing["drive_minutes"] and existing["reservation_url"]:
                continue
            print(f"  [{i}/{len(ids)}] {name}", file=sys.stderr)
            try:
                details = place_details(ctx, url) if url else {}
                lat, lng = details.get("lat"), details.get("lng")
                if lat is not None:
                    details.update(drive_from_home(ctx, f"{lat},{lng}"))
            except Exception as e:  # one bad place must not abort the run
                print(f"      skipped: {e}", file=sys.stderr)
                continue
            if not details.get("drive_miles"):
                miles = rc.miles_from_home(details.get("lat"), details.get("lng"))
                if miles is not None:
                    details["drive_miles"] = round(miles, 1)
            update = {k: details.get(k) for k in
                      ("cuisine", "price", "rating", "review_count", "lat", "lng",
                       "drive_minutes", "drive_text", "reservation_platform",
                       "reservation_url")
                      if details.get(k)}
            if update:
                sql = f"UPDATE restaurants SET {', '.join(f'{k}=?' for k in update)} WHERE id=?"
                conn.execute(sql, [*update.values(), rid])
                conn.commit()
                enriched += 1
    conn.close()
    print(json.dumps({"imported": len(places), "enriched": enriched}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
