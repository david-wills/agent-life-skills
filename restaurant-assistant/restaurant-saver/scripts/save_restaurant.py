#!/usr/bin/env python3
"""Intake: turn anything the user drops in #restaurants into a tracked row.

    save_restaurant.py "<name | maps url | article url>" [--note ...] [--status ...]

Resolve, enrich and store happen in ONE browser session rather than three
separate Playwright launches — that is most of the difference between a
40-second save and a two-minute one.

Output: JSON to stdout.
    {"kind":"saved","created":true,"restaurant":{...}}
    {"kind":"candidates","candidates":[...]}     -> agent disambiguates
    {"kind":"error","error":"..."}
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import restaurant_common as rc  # noqa: E402
import resolve_place as rp  # noqa: E402
from browser import maps_context  # noqa: E402
from enrich_place import drive_from_home, place_details  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Resolve, enrich and store one restaurant.")
    ap.add_argument("query", help="Name, Maps URL, or article URL")
    ap.add_argument("--note", default="", help="Why the user saved it")
    ap.add_argument("--source-url", default=None, help="Article/link it came from")
    ap.add_argument("--source-raw", default=None, help="The user's raw message")
    ap.add_argument("--status", default=rc.STATUS_WANT,
                    choices=[rc.STATUS_WANT, rc.STATUS_BEEN, rc.STATUS_FAVORITE])
    ap.add_argument("--top", type=int, default=3, help="Max candidates to return for a name search")
    ap.add_argument("--skip-drive", action="store_true", help="Skip the drive-time lookup")
    ap.add_argument("--watch", action="store_true", help="Run with a visible browser window")
    args = ap.parse_args()

    with maps_context(headless=not args.watch) as ctx:
        resolved = rp.resolve_in_context(ctx, args.query, top=args.top)
        if resolved["kind"] != "single":
            print(json.dumps(resolved))
            return 0 if resolved["kind"] == "candidates" else 1

        place = resolved["place"]
        details = place_details(ctx, place["url"])
        if not details.get("name"):
            details = dict(place)
            details["maps_url"] = place.get("url")
        if not args.skip_drive:
            lat, lng = details.get("lat"), details.get("lng")
            dest = f"{lat},{lng}" if lat is not None else (details.get("address") or details["name"])
            details.update(drive_from_home(ctx, str(dest)))

    # Straight-line fallback: the directions card does not always render miles.
    if not details.get("drive_miles"):
        miles = rc.miles_from_home(details.get("lat"), details.get("lng"))
        if miles is not None:
            details["drive_miles"] = round(miles, 1)

    source_url = args.source_url
    if source_url is None and rp.is_url(args.query) and not rp.is_maps_url(args.query):
        source_url = args.query

    row = {
        "name": details.get("name"),
        "address": details.get("address", ""),
        "lat": details.get("lat"),
        "lng": details.get("lng"),
        "maps_url": details.get("maps_url") or place.get("url"),
        "cuisine": details.get("cuisine"),
        "price": details.get("price"),
        "rating": details.get("rating"),
        "review_count": details.get("review_count"),
        "note": args.note,
        "source_url": source_url,
        "source_raw": args.source_raw,
        "drive_minutes": details.get("drive_minutes"),
        "drive_text": details.get("drive_text"),
        "reservation_platform": details.get("reservation_platform"),
        "reservation_url": details.get("reservation_url"),
        "status": args.status,
    }
    if args.status == rc.STATUS_BEEN:
        row["visited_at"] = rc.now_iso()

    conn = rc.connect()
    rid, created = rc.upsert_restaurant(conn, row)
    stored = dict(conn.execute("SELECT * FROM restaurants WHERE id=?", (rid,)).fetchone())
    stored["drive_miles"] = details.get("drive_miles")
    conn.close()

    print(json.dumps({"kind": "saved", "created": created, "restaurant": stored}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
