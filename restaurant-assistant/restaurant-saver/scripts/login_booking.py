#!/usr/bin/env python3
"""One-time visible sign-in to Resy and OpenTable. Run from a terminal.

    skills/restaurant-saver/.venv/bin/python \
        skills/restaurant-saver/scripts/login_booking.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from booking_browser import BOOKING_ACCOUNT, booking_context  # noqa: E402

SITES = [
    ("Resy", "https://resy.com/"),
    ("OpenTable", "https://www.opentable.com/"),
]


def main() -> int:
    print(f"Sign in to each site as {BOOKING_ACCOUNT}.")
    with booking_context(headless=False) as ctx:
        for name, url in SITES:
            page = ctx.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            input(f"  -> Sign in to {name}, then press Enter here... ")
            page.close()
    print("Saved. Cookies persist in restaurant-saver/booking-profile/.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
