#!/usr/bin/env python3
"""Shared Playwright helper for the restaurant-saver skill.

Persistent browser context dedicated to the Maps Google account
(``accounts.personal``). First run requires visible-browser sign-in;
cookies persist.
"""
from __future__ import annotations

import contextlib
import sys
from pathlib import Path

from playwright.sync_api import BrowserContext, Page, sync_playwright

sys.path.insert(0, str(Path(__file__).parent))
import restaurant_common as rc  # noqa: E402

WORKSPACE = Path.home() / ".openclaw" / "workspace"
PROFILE_DIR = WORKSPACE / "restaurant-saver" / "chrome-profile"
PROFILE_DIR.mkdir(parents=True, exist_ok=True)

# Resolved lazily (PEP 562) so a missing config key cannot break import for
# callers that never touch the account. Real value: config.local.json.
_CONFIG_ATTRS = {"EXPECTED_ACCOUNT": "accounts.personal"}


def __getattr__(name: str):
    key = _CONFIG_ATTRS.get(name)
    if key is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return rc.cfg(key)


def __dir__() -> list[str]:
    return sorted(list(globals()) + list(_CONFIG_ATTRS))

# Match real Chrome on macOS (channel="chrome" reports HeadlessChrome in
# headless mode; overriding keeps Google from flagging it).
def USER_AGENT() -> str:  # noqa: N802 - kept callable so the version is read live
    return rc.chrome_ua()


@contextlib.contextmanager
def maps_context(headless: bool = True):
    """Yield a Playwright BrowserContext bound to the Maps profile.

    Uses the real installed Google Chrome (channel="chrome") rather than
    Playwright's bundled "Chrome for Testing" — Google blocks the latter
    on sign-in with "this browser may not be secure". Adds light stealth
    (drop navigator.webdriver, suppress --enable-automation switches) so
    Google's bot heuristics don't trip on the persistent profile.
    """
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            channel="chrome",
            headless=headless,
            user_agent=USER_AGENT(),
            viewport={"width": 1280, "height": 900},
            locale="en-US",
            args=[
                "--disable-blink-features=AutomationControlled",
            ],
            ignore_default_args=["--enable-automation"],
        )
        ctx.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
        try:
            yield ctx
        finally:
            ctx.close()


def signed_in_email(page: Page) -> str | None:
    """Return the email of the currently signed-in Google account, or None.

    Maps does not expose a stable account selector. We fetch the lightweight
    accounts.google.com endpoint that returns the active account info as text
    once the session cookies are valid.
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
    import re
    m = re.search(r"[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}", body)
    return m.group(0) if m else None


def assert_correct_account(page: Page) -> None:
    """Raise if the active Google session is not the expected Maps account."""
    email = signed_in_email(page)
    if email is None:
        raise RuntimeError("not_logged_in")
    if email.lower() != rc.cfg("accounts.personal").lower():
        raise RuntimeError(f"wrong_account:{email}")
