#!/usr/bin/env python3
"""Playwright context for booking sites (Resy / OpenTable / Tock).

Deliberately a SECOND profile. Maps runs as ``accounts.personal``; the Resy
and OpenTable accounts are ``accounts.shopping``. Sharing one profile would
sign the wrong identity into one of them.
"""
from __future__ import annotations

import contextlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from playwright.sync_api import sync_playwright

WORKSPACE = Path.home() / ".openclaw" / "workspace"
PROFILE_DIR = WORKSPACE / "restaurant-saver" / "booking-profile"
PROFILE_DIR.mkdir(parents=True, exist_ok=True)

import restaurant_common as rc

# Resolved lazily (PEP 562); real value in the gitignored config.local.json.
_CONFIG_ATTRS = {"BOOKING_ACCOUNT": "accounts.shopping"}


def __getattr__(name: str):
    key = _CONFIG_ATTRS.get(name)
    if key is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return rc.cfg(key)


def __dir__() -> list[str]:
    return sorted(list(globals()) + list(_CONFIG_ATTRS))


@contextlib.contextmanager
def booking_context(headless: bool = True):
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            channel="chrome",
            headless=headless,
            # Visible runs keep Chrome's native UA so it matches the client
            # hints Chrome sends anyway. Only mask the headless tell.
            user_agent=rc.chrome_ua() if headless else None,
            viewport={"width": 1280, "height": 900},
            locale="en-US",
            args=["--disable-blink-features=AutomationControlled"],
            ignore_default_args=["--enable-automation"],
        )
        ctx.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
        try:
            yield ctx
        finally:
            ctx.close()
