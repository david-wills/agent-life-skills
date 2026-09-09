#!/usr/bin/env python3
"""Fetch live open-now / today's hours / rating for a single Maps place.

Usage:
    python3 place_hours.py "<maps place URL>"

Output JSON:
    {
      "success": true,
      "name": "Bestia",
      "url": "...",
      "open_now": true,          # null if unknown
      "today_hours": "5:00 PM – 11:00 PM",
      "rating": "4.6",
      "category": "Italian restaurant",
      "phone": "+1 213-514-5724"
    }

Designed for the few candidates the agent has already shortlisted from the
cached list — not for bulk enrichment. One place at a time.
"""
from __future__ import annotations

import argparse
import json
import re
import sys

from browser import maps_context


def parse_hours_label(label: str) -> tuple[bool | None, str]:
    """Parse the aria-label of the place-page hours button.

    Examples:
        "Hours · Open · Closes 11 PM"
        "Hours · Closed · Opens 11 AM Thu"
        "Hours · Open 24 hours"
        "Hours · 5:00 PM – 11:00 PM"
    """
    if not label:
        return None, ""
    s = re.sub(r"^Hours\s*[·•:]\s*", "", label).strip()
    open_now: bool | None = None
    low = s.lower()
    if "closed" in low and "closes" not in low:
        open_now = False
    elif "open" in low or "closes" in low:
        open_now = True
    return open_now, s


def fetch(ctx, url: str) -> dict:
    page = ctx.new_page()
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=20000)
        page.wait_for_timeout(2200)
    except Exception as e:
        page.close()
        return {"success": False, "error": "navigation_failed", "detail": str(e)}

    if "/maps/place/" not in (page.url or ""):
        try:
            page.wait_for_url("**/maps/place/**", timeout=4000)
        except Exception:
            pass

    name = ""
    try:
        if page.locator("h1").count():
            name = page.locator("h1").first.inner_text(timeout=3000).strip()
    except Exception:
        pass

    open_now: bool | None = None
    today_hours = ""
    try:
        hrs_btn = page.locator('button[data-item-id="oh"]').first
        if hrs_btn.count():
            label = hrs_btn.get_attribute("aria-label") or ""
            open_now, today_hours = parse_hours_label(label)
    except Exception:
        pass

    rating = ""
    review_count = ""
    try:
        rating_node = page.locator('[role="img"][aria-label*="stars"]').first
        if rating_node.count():
            label = rating_node.get_attribute("aria-label") or ""
            m = re.search(r"(\d+(?:\.\d+)?)\s*stars?", label)
            if m:
                rating = m.group(1)
    except Exception:
        pass
    try:
        # Review count near the rating
        rev_btn = page.locator('button[aria-label*="reviews"]').first
        if rev_btn.count():
            label = rev_btn.get_attribute("aria-label") or ""
            m = re.search(r"([\d,]+)\s*reviews?", label)
            if m:
                review_count = m.group(1)
    except Exception:
        pass

    category = ""
    try:
        cat_btn = page.locator('button[jsaction*="category"]').first
        if cat_btn.count():
            category = (cat_btn.inner_text(timeout=1500) or "").strip()
    except Exception:
        pass

    phone = ""
    try:
        phone_btn = page.locator('button[data-item-id^="phone:"]').first
        if phone_btn.count():
            label = phone_btn.get_attribute("aria-label") or ""
            phone = re.sub(r"^Phone:\s*", "", label).strip()
    except Exception:
        pass

    address = ""
    try:
        addr_btn = page.locator('button[data-item-id="address"]').first
        if addr_btn.count():
            label = addr_btn.get_attribute("aria-label") or ""
            address = re.sub(r"^Address:\s*", "", label).strip()
    except Exception:
        pass

    page.close()
    return {
        "success": True,
        "name": name,
        "url": page.url,
        "address": address,
        "open_now": open_now,
        "today_hours": today_hours,
        "rating": rating,
        "review_count": review_count,
        "category": category,
        "phone": phone,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("url", help="Google Maps place URL")
    ap.add_argument("--watch", action="store_true", help="Visible browser window")
    args = ap.parse_args()

    with maps_context(headless=not args.watch) as ctx:
        result = fetch(ctx, args.url)
    print(json.dumps(result))
    return 0 if result.get("success") else 1


if __name__ == "__main__":
    raise SystemExit(main())
