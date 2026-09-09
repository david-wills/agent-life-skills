#!/usr/bin/env python3
"""Add a Google Maps place to one of the user's saved lists.

Supports:
  - --list NAME           target list (default "Want to go")
  - --remove-from NAME    also un-check this list (e.g. promote Want-to-go → Been Here)

Usage:
    python3 add_to_list.py "<maps place URL>"
    python3 add_to_list.py --query "Bestia Los Angeles"
    python3 add_to_list.py --query "Bestia" --list "Been Here" --remove-from "Want to go"

Output JSON:
    {"success": true, "place": {name, address, url}, "list": "...",
     "removed_from": "Want to go" | null}
    {"success": false, "error": "<reason>", ...}
"""
from __future__ import annotations

import argparse
import json
import re
import urllib.parse

from browser import EXPECTED_ACCOUNT, maps_context, signed_in_email

DEFAULT_LIST = "Want to go"


def parse_place(page) -> dict[str, str] | None:
    try:
        page.wait_for_selector("h1", timeout=10000)
    except Exception:
        return None
    try:
        name = page.locator("h1").first.inner_text(timeout=3000).strip()
    except Exception:
        name = ""
    address = ""
    try:
        addr_btn = page.locator('button[data-item-id="address"]').first
        address = (addr_btn.get_attribute("aria-label") or "").strip()
        address = re.sub(r"^Address:\s*", "", address)
    except Exception:
        pass
    return {"name": name, "address": address, "url": page.url}


SAVE_CHIP_SELECTOR = (
    'button[aria-label="Save"], button[aria-label="Saved"], '
    'button[aria-label^="Saved ("]'
)
LIST_MENU_SELECTOR = '[role="menu"][aria-label="Save in your lists"]'


def open_lists_menu(page) -> bool:
    """Click the place-panel Save chip and wait for the lists menu to open."""
    chip = page.locator(SAVE_CHIP_SELECTOR).first
    if chip.count() == 0:
        return False
    try:
        chip.click(timeout=5000)
    except Exception:
        return False
    try:
        page.wait_for_selector(LIST_MENU_SELECTOR, timeout=5000)
    except Exception:
        return False
    return True


def find_list_item(page, list_name: str):
    """Locator for the named list inside the Save-in-your-lists menu.

    Matches case-insensitively against the visible text of menuitemradio rows.
    The list items render slightly after the menu container appears, so wait
    for at least one menuitemradio before iterating.
    """
    try:
        page.wait_for_selector(
            f'{LIST_MENU_SELECTOR} [role="menuitemradio"]', timeout=5000
        )
    except Exception:
        return None
    items = page.locator(f'{LIST_MENU_SELECTOR} [role="menuitemradio"]').all()
    target = list_name.strip().lower()
    # Each menuitemradio's inner_text is multi-line: icon glyphs, then the
    # list name on its own line, then "Private · N places". Match by line.
    # The user may have several lists named "Want to go" (their own + a shared one);
    # iterate in DOM order and return the first match — their own list ranks
    # higher than shared lists in the menu.
    for item in items:
        try:
            text = (item.inner_text(timeout=1000) or "").lower()
        except Exception:
            continue
        lines = [l.strip() for l in text.split("\n") if l.strip()]
        if target in lines:
            return item
    return None


def list_item_checked(item) -> bool:
    try:
        return (item.get_attribute("aria-checked") or "").lower() == "true"
    except Exception:
        return False


def close_menu(page) -> None:
    try:
        page.keyboard.press("Escape")
        page.wait_for_timeout(400)
    except Exception:
        pass


def toggle_list(page, list_name: str, want_checked: bool) -> dict:
    """Toggle the named list to the desired checked state. Returns status dict."""
    if not open_lists_menu(page):
        return {"ok": False, "error": "save_chip_not_found"}
    item = find_list_item(page, list_name)
    if item is None:
        close_menu(page)
        return {"ok": False, "error": "list_not_found", "list": list_name}
    is_checked = list_item_checked(item)
    if is_checked == want_checked:
        close_menu(page)
        return {"ok": True, "changed": False, "list": list_name, "was_checked": is_checked}
    try:
        item.click(timeout=4000)
    except Exception as e:
        close_menu(page)
        return {"ok": False, "error": "click_failed", "detail": str(e), "list": list_name}
    page.wait_for_timeout(1200)
    # Verify by re-opening the menu and reading state.
    verified = None
    if open_lists_menu(page):
        item2 = find_list_item(page, list_name)
        if item2 is not None:
            verified = list_item_checked(item2) == want_checked
        close_menu(page)
    return {"ok": True, "changed": True, "list": list_name, "verified": verified}


def add_to_list(
    ctx,
    place_url: str | None,
    query: str | None,
    list_name: str,
    remove_from: str | None,
) -> dict:
    page = ctx.new_page()

    email = signed_in_email(page)
    if email is None:
        page.close()
        return {"success": False, "error": "not_logged_in"}
    if email.lower() != EXPECTED_ACCOUNT.lower():
        page.close()
        return {"success": False, "error": "wrong_account", "active_account": email}

    if place_url:
        target = place_url
    elif query:
        target = f"https://www.google.com/maps/search/{urllib.parse.quote(query)}"
    else:
        page.close()
        return {"success": False, "error": "no_input"}

    try:
        page.goto(target, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(3000)
    except Exception as e:
        page.close()
        return {"success": False, "error": "navigation_failed", "detail": str(e)}

    if "/maps/place/" not in (page.url or ""):
        try:
            page.wait_for_url("**/maps/place/**", timeout=4000)
        except Exception:
            pass
    if "/maps/place/" not in (page.url or "") and page.locator('button[data-item-id="address"]').count() == 0:
        page.close()
        return {"success": False, "error": "not_a_place_page", "url": page.url}

    place = parse_place(page) or {}

    # Toggle target list ON.
    add_result = toggle_list(page, list_name, want_checked=True)
    if not add_result.get("ok"):
        page.close()
        return {
            "success": False,
            "error": add_result.get("error", "add_failed"),
            "detail": add_result.get("detail"),
            "list": list_name,
            "place": place,
        }

    already_on_list = (
        not add_result.get("changed", False) and add_result.get("was_checked", False)
    )

    # Toggle remove_from list OFF. Wait briefly first — Maps' menu state can
    # lag by a beat after the first toggle, causing a freshly-saved place to
    # appear unchecked on the source list when re-opened.
    removed_from = None
    if remove_from and remove_from.strip().lower() != list_name.strip().lower():
        page.wait_for_timeout(2000)
        rm_result = toggle_list(page, remove_from, want_checked=False)
        if rm_result.get("ok") and rm_result.get("changed"):
            removed_from = remove_from

    page.close()
    return {
        "success": True,
        "list": list_name,
        "place": place,
        "already_on_list": already_on_list,
        "verified": add_result.get("verified"),
        "removed_from": removed_from,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("place_url", nargs="?", help="Google Maps place URL")
    ap.add_argument("--query", help="Restaurant name to search for and add (best-match)")
    ap.add_argument("--list", default=DEFAULT_LIST, help='Target list (default "Want to go")')
    ap.add_argument("--remove-from", dest="remove_from", help="Un-check this list after adding")
    ap.add_argument("--watch", action="store_true", help="Run with a visible browser window")
    args = ap.parse_args()

    if not args.place_url and not args.query:
        print(json.dumps({"success": False, "error": "missing_input"}))
        return 1

    with maps_context(headless=not args.watch) as ctx:
        result = add_to_list(
            ctx,
            args.place_url,
            args.query,
            args.list,
            args.remove_from,
        )
    print(json.dumps(result))
    return 0 if result.get("success") else 1


if __name__ == "__main__":
    raise SystemExit(main())
