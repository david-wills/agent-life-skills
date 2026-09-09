#!/usr/bin/env python3
"""Playwright helper: a persistent Chrome profile signed into Google Maps.

The profile lives at ``<data_root>/restaurant-saver/chrome-profile/`` and is
signed into ``accounts.personal`` once, via ``login.py``. Cookies persist, so
every later run is headless.

Playwright is imported inside ``maps_context`` so that every script's
``--help`` works without it installed.
"""
from __future__ import annotations

import contextlib
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import restaurant_common as rc  # noqa: E402

PLAYWRIGHT_HINT = "playwright is not installed: pip install -r requirements.txt && python -m playwright install"


def profile_dir() -> Path:
    return rc.skill_data_dir() / "chrome-profile"


@contextlib.contextmanager
def maps_context(headless: bool = True):
    """Yield a Playwright BrowserContext bound to the Maps profile.

    Drives the real installed Google Chrome (``channel="chrome"``, or the
    binary in ``CHROME_PATH``) rather than Playwright's bundled Chromium, which
    Google refuses to sign in ("this browser may not be secure").

    The flags below reduce automation fingerprinting so a signed-in Maps
    session behaves like a normal browser: no ``--enable-automation`` switch,
    ``navigator.webdriver`` undefined, and a UA that matches the installed
    Chrome. Be clear about what that is: this is scraping Google Maps through
    a logged-in account, and Google's terms of service may prohibit it. Use at
    your own risk.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise SystemExit(PLAYWRIGHT_HINT) from exc

    pdir = profile_dir()
    pdir.mkdir(parents=True, exist_ok=True)
    launch: dict = dict(
        user_data_dir=str(pdir),
        headless=headless,
        user_agent=rc.chrome_ua(),
        viewport={"width": 1280, "height": 900},
        locale="en-US",
        args=["--disable-blink-features=AutomationControlled"],
        ignore_default_args=["--enable-automation"],
    )
    if os.environ.get("CHROME_PATH"):
        launch["executable_path"] = rc.chrome_bin()
    else:
        launch["channel"] = "chrome"

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(**launch)
        ctx.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
        try:
            yield ctx
        finally:
            ctx.close()


def signed_in_email(page) -> str | None:
    """Return the email of the currently signed-in Google account, or None.

    Maps does not expose a stable account selector, so read it off the
    account home page once the session cookies are valid.
    """
    try:
        page.goto(
            "https://accounts.google.com/CheckCookie?continue=https%3A%2F%2Fmyaccount.google.com%2F",
            wait_until="domcontentloaded",
            timeout=20000,
        )
        page.goto("https://myaccount.google.com/", wait_until="domcontentloaded", timeout=20000)
    except Exception:
        return None
    try:
        body = (page.locator("body").first.inner_text(timeout=2500) or "").lower()
    except Exception:
        body = ""
    if "@" not in body:
        return None
    m = re.search(r"[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}", body)
    return m.group(0) if m else None
