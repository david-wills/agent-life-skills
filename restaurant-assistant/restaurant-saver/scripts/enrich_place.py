#!/usr/bin/env python3
"""Enrich a resolved Maps place with the facts the planner needs.

Adds three things resolve_place.py does not give us:
  1. drive time from home (traffic-aware, as Maps reports it)
  2. the reservation platform + booking URL
  3. cuisine / price / rating off the place panel

Price is stored exactly as Maps prints it — "$$", "$$$$", "$20–30", "$100+" —
not normalised to a dollar count, so the card shows what the user would see.

Usage:
    enrich_place.py "<maps place url>" [--watch] [--skip-drive]
Output: JSON to stdout.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import restaurant_common as rc  # noqa: E402
from browser import maps_context  # noqa: E402

# "1 hr 5 min", "2 hr" (a round hour prints no minutes), "25 min".
DURATION_RE = re.compile(r"\b(\d+)\s*hr(?:\s*(\d+)\s*min)?\b|\b(\d+)\s*min\b", re.I)

# Maps renders price as either a tier ("$", "$$" ... "$$$$") or a per-person
# range ("$20–30", "$30-50", "$100+"). Match either, as a whole token.
PRICE_RE = re.compile(
    r"(?<![\w$])("
    r"\${1,4}(?![\w$])"                                  # $ … $$$$
    r"|\$\d[\d,]*(?:\s*[\u2013\u2014-]\s*\$?\d[\d,]*)?\+?"  # $20–30, $100+
    r")(?![\w$])"
)

# Booking hosts worth capturing off the Maps place panel.
BOOKING_HOSTS = (
    "resy.com", "opentable.com", "exploretock.com", "sevenrooms.com",
    "yelp.com/reservations", "tablein.com", "quandoo",
)


def parse_duration_minutes(text: str) -> int | None:
    m = DURATION_RE.search(text or "")
    if not m:
        return None
    if m.group(3):
        return int(m.group(3))
    hours = int(m.group(1) or 0)
    mins = int(m.group(2) or 0)
    return hours * 60 + mins


def drive_from_home(ctx, dest: str) -> dict[str, object]:
    """Scrape Maps for the driving time from home to dest.

    `dest` is an address string or "lat,lng". Returns {} when Maps does not
    render a usable route — callers fall back to straight-line distance.
    """
    page = ctx.new_page()
    url = (
        "https://www.google.com/maps/dir/?api=1"
        f"&origin={urllib.parse.quote(rc.cfg('user.home')['address'])}"
        f"&destination={urllib.parse.quote(dest)}"
        "&travelmode=driving"
    )
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(4500)
        try:
            page.wait_for_selector("#section-directions-trip-0", timeout=12000)
        except Exception:
            pass
        text = page.locator("body").first.inner_text(timeout=5000) or ""
    except Exception:
        page.close()
        return {}
    page.close()

    # The first trip card carries the recommended route's duration + distance.
    minutes = parse_duration_minutes(text)
    label = ""
    m = re.search(r"(\d+(?:\.\d+)?)\s*mi\b", text)
    miles = float(m.group(1)) if m else None
    dm = DURATION_RE.search(text or "")
    if dm:
        label = dm.group(0).strip()
    return {"drive_minutes": minutes, "drive_miles": miles, "drive_text": label}


def place_details(ctx, place_url: str) -> dict[str, object]:
    """Pull cuisine / price / rating / reservation link off a place page."""
    page = ctx.new_page()
    out: dict[str, object] = {}
    try:
        page.goto(place_url, wait_until="domcontentloaded", timeout=40000)
        page.wait_for_selector("h1", timeout=12000)
        page.wait_for_timeout(2000)
    except Exception:
        page.close()
        return out

    try:
        out["name"] = page.locator("h1").first.inner_text(timeout=3000).strip()
    except Exception:
        pass
    try:
        addr = page.locator('button[data-item-id="address"]').first
        out["address"] = re.sub(r"^Address:\s*", "", (addr.get_attribute("aria-label") or "").strip())
    except Exception:
        pass

    body = ""
    try:
        body = page.locator("body").first.inner_text(timeout=4000) or ""
    except Exception:
        pass

    m = re.search(r"\b([1-5]\.\d)\s*\n?\(?([\d,]+)\)?\s*(?:reviews?)?", body)
    if m:
        out["rating"] = m.group(1)
        out["review_count"] = m.group(2)
    m = PRICE_RE.search(body)
    if m:
        out["price"] = m.group(1)

    # Category chip: Maps renders it as a button next to the rating.
    try:
        cat = page.locator('button[jsaction*="category"]').first
        if cat.count():
            out["cuisine"] = (cat.inner_text(timeout=2000) or "").strip()
    except Exception:
        pass

    # Reservation link: Maps surfaces booking partners as outbound anchors.
    reservation_url = None
    try:
        for a in page.locator("a[href]").all()[:400]:
            href = a.get_attribute("href") or ""
            if any(h in href.lower() for h in BOOKING_HOSTS):
                reservation_url = href
                break
    except Exception:
        pass
    if reservation_url:
        out["reservation_url"] = reservation_url
        out["reservation_platform"] = rc.platform_for_url(reservation_url)

    lat, lng = rc.coords_from_url(page.url)
    if lat is not None:
        out["lat"], out["lng"] = lat, lng
    out["maps_url"] = page.url
    page.close()
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Enrich a Maps place URL with drive time, booking link and details.")
    ap.add_argument("place_url", help="A google.com/maps/place/... URL")
    ap.add_argument("--watch", action="store_true", help="Run with a visible browser window")
    ap.add_argument("--skip-drive", action="store_true", help="Skip the drive-time lookup")
    args = ap.parse_args()

    with maps_context(headless=not args.watch) as ctx:
        details = place_details(ctx, args.place_url)
        if not details.get("name"):
            print(json.dumps({"error": "not_a_place_page"}))
            return 1
        if not args.skip_drive:
            dest = details.get("address") or details["name"]
            if details.get("lat") is not None:
                dest = f"{details['lat']},{details['lng']}"
            details.update(drive_from_home(ctx, str(dest)))
    print(json.dumps(details))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
