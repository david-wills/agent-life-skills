#!/usr/bin/env python3
"""Scrape the user's "Want to go" Google Maps list to a local cache.

Output: workspace/restaurant-saver/want-to-go.json
    {
      "list": "Want to go",
      "fetched_at": "2026-04-29T20:30:00-07:00",
      "count": 47,
      "places": [
        {"name", "address", "url", "note", "rating", "review_count",
         "price", "category", "lat", "lng"},
        ...
      ]
    }

The cache is consumed by the OpenClaw agent at query time. Re-run on demand
or when the cache is older than ~7 days.

Navigation path:
    https://www.google.com/maps  →  click "Saved"  →  click "Want to go"
    Then iterate the rendered cards. For each, read metadata from the card
    DOM and click into the place panel to capture the canonical URL +
    coordinates.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from browser import EXPECTED_ACCOUNT, maps_context, signed_in_email

WORKSPACE = Path.home() / ".openclaw" / "workspace"
CACHE_PATH = WORKSPACE / "restaurant-saver" / "want-to-go.json"
LIST_NAME = "Want to go"

# A real per-place card has a direct child `.BsJqK` wrapper. The list pane
# itself also matches `m6QErb.XiKgde` but its direct children are different
# containers, so the `>` combinator inside `:has()` discriminates.
CARD_SELECTOR = "div.m6QErb.XiKgde:has(> div.BsJqK)"


def coords_from_url(href: str) -> tuple[float | None, float | None]:
    """Extract (lat, lng) from a Maps place URL."""
    m = re.search(r"!3d(-?\d+\.\d+)!4d(-?\d+\.\d+)", href)
    if m:
        return float(m.group(1)), float(m.group(2))
    m = re.search(r"/@(-?\d+\.\d+),(-?\d+\.\d+)", href)
    if m:
        return float(m.group(1)), float(m.group(2))
    return None, None


def open_list(page, list_name: str) -> str | None:
    """Navigate maps.google.com → Saved → <list_name>. Returns error or None."""
    try:
        page.goto("https://www.google.com/maps", wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(3500)
    except Exception:
        return "navigation_failed"

    saved_btn = page.locator('button:has-text("Saved")').first
    if saved_btn.count() == 0:
        return "saved_button_not_found"
    try:
        saved_btn.click(timeout=5000)
        page.wait_for_timeout(2500)
    except Exception:
        return "saved_click_failed"

    list_btn = page.locator(f'button:has-text("{list_name}")').first
    if list_btn.count() == 0:
        return "list_link_not_found"
    try:
        list_btn.click(timeout=5000)
        page.wait_for_timeout(7000)
    except Exception:
        return "list_click_failed"

    if page.locator(CARD_SELECTOR).count() == 0:
        return "no_cards_rendered"
    return None


def scroll_load_all(page, max_rounds: int = 80) -> int:
    """Scroll the list container until card count stabilises. Returns final count."""
    last_count = -1
    stale = 0
    for _ in range(max_rounds):
        count = page.locator(CARD_SELECTOR).count()
        if count == last_count:
            stale += 1
            if stale >= 4:
                break
        else:
            stale = 0
            last_count = count
        # Find a real card and walk up to its scrollable ancestor.
        page.evaluate(
            """() => {
                const cards = document.querySelectorAll('div.m6QErb.XiKgde');
                const realCards = Array.from(cards).filter(c => c.querySelector(':scope > .BsJqK'));
                if (!realCards.length) return;
                let n = realCards[0].parentElement;
                while (n && n !== document.body) {
                    const s = window.getComputedStyle(n);
                    if (/auto|scroll/.test(s.overflowY) && n.scrollHeight > n.clientHeight) {
                        n.scrollTop = n.scrollHeight;
                        return;
                    }
                    n = n.parentElement;
                }
            }"""
        )
        page.wait_for_timeout(1300)
    return page.locator(CARD_SELECTOR).count()


def parse_rating(card_text: str) -> tuple[str, str, str, str]:
    """From card text, derive (rating, review_count, price, category).

    Card layouts seen:
        Picala\\n5.0(4)\\nSpanish\\n…
        Sqirl\\n4.4(1,581)\\n$20–30\\n· Restaurant\\n…
        Bestia\\n4.5(3,997)\\n$100+\\n· Italian\\n…

    Lines are: [name, rating(reviews), price-or-category, ·category-if-price-on-prev].
    """
    lines = [l.strip() for l in (card_text or "").split("\n") if l.strip()]
    rating = ""
    reviews = ""
    price = ""
    category = ""
    if len(lines) >= 2:
        m = re.match(r"^(\d+(?:\.\d+)?)\(([\d,]+)\)$", lines[1])
        if m:
            rating = m.group(1)
            reviews = m.group(2)
    if len(lines) >= 3:
        third = lines[2]
        if third.startswith("$"):
            price = third
            if len(lines) >= 4 and lines[3].startswith("·"):
                category = lines[3].lstrip("·").strip()
        else:
            category = third.lstrip("·").strip()
    return rating, reviews, price, category


def read_card(card) -> dict:
    """Extract name + meta + note from a single card element."""
    out = {"name": "", "rating": "", "review_count": "", "price": "", "category": "", "note": ""}
    try:
        name_el = card.locator(".fontHeadlineSmall").first
        if name_el.count():
            out["name"] = (name_el.inner_text(timeout=1000) or "").strip()
    except Exception:
        pass

    try:
        text = (card.inner_text(timeout=1500) or "").strip()
    except Exception:
        text = ""
    rating, reviews, price, category = parse_rating(text)
    out["rating"] = rating
    out["review_count"] = reviews
    out["price"] = price
    out["category"] = category

    # User note: the note button's aria-label is "Add note" when empty,
    # otherwise (likely) "Edit note" or contains the note text. The visible
    # text inside the button is the note itself (else "Note" placeholder).
    try:
        note_btns = card.locator('button[aria-label*="note" i]').all()
        for btn in note_btns:
            aria = (btn.get_attribute("aria-label") or "").lower()
            if "add note" in aria:
                continue  # empty note
            txt = (btn.inner_text(timeout=500) or "").strip()
            # Strip the leading icon char ( etc.)
            txt = re.sub(r"[-]", "", txt).strip()
            if txt and txt.lower() != "note":
                out["note"] = txt
                break
    except Exception:
        pass

    return out


def click_card_get_url(page, card_index: int, prev_url: str = "", retries: int = 2) -> str | None:
    """Click the Nth card and wait for the URL to settle on a NEW /maps/place/.

    Maps swaps the place panel asynchronously, so a quick read after click
    returns the previous card's URL. Pass prev_url to require a change.
    """
    for _ in range(retries + 1):
        cards = page.locator(CARD_SELECTOR).all()
        if card_index >= len(cards):
            return None
        card = cards[card_index]
        target = card.locator(".GYMy9d").first
        if target.count() == 0:
            target = card.locator(".fontHeadlineSmall").first
        if target.count() == 0:
            target = card
        try:
            target.click(timeout=4000)
        except Exception:
            page.wait_for_timeout(800)
            continue

        # Wait for URL to (a) be /maps/place/, AND (b) differ from prev_url.
        try:
            page.wait_for_function(
                """(prev) => location.href.includes('/maps/place/') && location.href !== prev""",
                arg=prev_url,
                timeout=8000,
            )
            # Settle: address button takes another beat to appear.
            page.wait_for_timeout(800)
            return page.url
        except Exception:
            page.wait_for_timeout(1500)
            current = page.url or ""
            if "/maps/place/" in current and current != prev_url:
                return current
    return None


def navigate_back_to_list(page, list_url: str) -> bool:
    """Return to the saved-list view after viewing a place.

    Strategy: click the panel's Back button if present; else navigate to
    list_url. Returns True when cards are present after the action.
    """
    # Try the place-panel back arrow first (faster, no full reload).
    for sel in ['button[aria-label="Back"]', 'button[jsaction*="back"]', 'button[aria-label*="Back to"]']:
        btn = page.locator(sel).first
        if btn.count():
            try:
                btn.click(timeout=2500)
                page.wait_for_timeout(1200)
                if page.locator(CARD_SELECTOR).count() > 0:
                    return True
            except Exception:
                pass

    # Fallback: navigate to the list URL.
    try:
        page.goto(list_url, wait_until="domcontentloaded", timeout=20000)
        page.wait_for_timeout(2500)
    except Exception:
        return False
    return page.locator(CARD_SELECTOR).count() > 0


def scrape_list(ctx, list_name: str = LIST_NAME, debug: bool = False, max_places: int | None = None) -> dict:
    page = ctx.new_page()

    email = signed_in_email(page)
    if email is None:
        page.close()
        return {"success": False, "error": "not_logged_in"}
    if email.lower() != EXPECTED_ACCOUNT.lower():
        page.close()
        return {"success": False, "error": "wrong_account", "active_account": email}

    err = open_list(page, list_name)
    if err:
        page.close()
        return {"success": False, "error": err}

    list_url = page.url

    total = scroll_load_all(page)
    if max_places:
        total = min(total, max_places)

    # Collect static metadata first
    metas = []
    cards = page.locator(CARD_SELECTOR).all()
    for i, card in enumerate(cards[:total]):
        meta = read_card(card)
        metas.append(meta)

    # Click each card to get URL + coords. Don't navigate back; the list pane
    # stays attached and the next click swaps the place panel. Pass the prior
    # URL so we wait for a real change before reading. If the browser crashes
    # mid-loop, persist what we have.
    places = []
    prev_url = ""
    crashed = False
    for i in range(total):
        meta = metas[i]
        try:
            place_url = click_card_get_url(page, i, prev_url=prev_url)
        except Exception as e:
            if "closed" in str(e).lower() or "target" in str(e).lower():
                if debug:
                    print(f"  [crash] browser closed at i={i}: {e}", file=sys.stderr)
                crashed = True
                break
            place_url = None
        if place_url:
            prev_url = place_url
        lat, lng = (None, None)
        address = ""
        canonical_url = place_url or ""
        if place_url:
            lat, lng = coords_from_url(place_url)
            try:
                page.wait_for_selector('button[data-item-id="address"]', timeout=2500)
                label = (
                    page.locator('button[data-item-id="address"]').first.get_attribute("aria-label") or ""
                )
                address = re.sub(r"^Address:\s*", "", label).strip()
            except Exception as e:
                if "closed" in str(e).lower() or "target" in str(e).lower():
                    crashed = True
                    if debug:
                        print(f"  [crash] reading address at i={i}: {e}", file=sys.stderr)

        places.append({
            "name": meta["name"],
            "address": address,
            "url": canonical_url,
            "note": meta["note"],
            "rating": meta["rating"],
            "review_count": meta["review_count"],
            "price": meta["price"],
            "category": meta["category"],
            "lat": lat,
            "lng": lng,
        })

        if debug:
            print(f"  [{i+1}/{total}] {meta['name']!r:40s} lat={lat} lng={lng} note={meta['note']!r}", file=sys.stderr)

        if crashed:
            break

        # Don't navigate back — the list panel stays attached and the next
        # card click swaps the place panel. Just scroll the next card into
        # view to ensure it's mounted.
        if i + 1 < total:
            try:
                page.evaluate(
                    """(idx) => {
                        const cards = document.querySelectorAll('div.m6QErb.XiKgde');
                        const real = Array.from(cards).filter(c => c.querySelector(':scope > .BsJqK'));
                        if (real[idx]) real[idx].scrollIntoView({block: 'center', behavior: 'instant'});
                    }""",
                    i + 1,
                )
                page.wait_for_timeout(300)
            except Exception:
                crashed = True
                break

    try:
        page.close()
    except Exception:
        pass

    return {
        "success": True,
        "list": list_name,
        "fetched_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "count": len(places),
        "places": places,
        "crashed": crashed,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", default=LIST_NAME, help="List name to scrape")
    ap.add_argument("--watch", action="store_true", help="Run with a visible browser window")
    ap.add_argument("--debug", action="store_true", help="Print per-card progress to stderr")
    ap.add_argument("--max", type=int, help="Stop after N places (testing)")
    ap.add_argument("--out", default=str(CACHE_PATH), help="Output JSON path")
    ap.add_argument("--stdout-only", action="store_true", help="Skip file write")
    args = ap.parse_args()

    with maps_context(headless=not args.watch) as ctx:
        result = scrape_list(ctx, args.list, debug=args.debug, max_places=args.max)

    if not result.get("success"):
        print(json.dumps(result))
        return 1

    if not args.stdout_only:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        persist = {k: v for k, v in result.items() if k != "success"}
        out.write_text(json.dumps(persist, indent=2))

    print(json.dumps({
        "success": True,
        "list": result["list"],
        "count": result["count"],
        "fetched_at": result["fetched_at"],
        "cache_path": args.out if not args.stdout_only else None,
        "places": result["places"] if args.stdout_only else None,
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
