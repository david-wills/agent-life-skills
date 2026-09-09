#!/usr/bin/env python3
"""One-time visible sign-in for the Maps browser profile.

    python3 login.py

Opens a visible Chrome window at accounts.google.com. Sign in as
``accounts.personal`` (handle 2FA), then press Enter in the terminal to close.
Cookies persist in ``<data_root>/restaurant-saver/chrome-profile/`` and every
later run is headless. Re-run this whenever a script reports ``not_logged_in``
or ``wrong_account``.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import restaurant_common as rc  # noqa: E402
from browser import maps_context, signed_in_email  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.parse_args()

    expected = rc.cfg("accounts.personal")
    print(f"Opening visible browser. Sign into {expected}.")
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
            print("ERROR: not_logged_in — no signed-in account detected.", file=sys.stderr)
            return 1
        if email.lower() != expected.lower():
            print(f"ERROR: wrong_account — signed in as {email}, expected {expected}.", file=sys.stderr)
            return 2
        print(f"OK — signed in as {email}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
