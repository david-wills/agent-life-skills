#!/usr/bin/env python3
"""Open a visible Chromium window to sign into the Maps account (accounts.personal).

Usage:
    python3 login.py

The window opens to accounts.google.com. Sign in (handle 2FA), then press
Enter in the terminal to close. Cookies persist in chrome-profile/.
"""
from __future__ import annotations

import sys

from browser import EXPECTED_ACCOUNT, maps_context, signed_in_email


def main() -> int:
    print(f"Opening visible browser. Sign into {EXPECTED_ACCOUNT}.")
    print("After you see the Google account home, press Enter here to close.")
    with maps_context(headless=False) as ctx:
        page = ctx.new_page()
        page.goto("https://accounts.google.com/", wait_until="domcontentloaded", timeout=60000)
        try:
            input()
        except (EOFError, KeyboardInterrupt):
            pass
        email = signed_in_email(page)
        if email is None:
            print("ERROR: no signed-in account detected.", file=sys.stderr)
            return 1
        if email.lower() != EXPECTED_ACCOUNT.lower():
            print(f"ERROR: signed in as {email}, expected {EXPECTED_ACCOUNT}.", file=sys.stderr)
            return 2
        print(f"OK — signed in as {email}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
