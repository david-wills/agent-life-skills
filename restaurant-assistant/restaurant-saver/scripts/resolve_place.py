#!/usr/bin/env python3
"""Resolve a restaurant input to a canonical Google Maps place.

Accepts one of:
- A Google Maps URL (maps.app.goo.gl/..., google.com/maps/place/..., etc.)
- An article URL about a restaurant (a review site, a blog post)
- A plain restaurant name ("Casa Invent", "Blue Heron Sushi")

Strategy:
1. Detect input type by URL host / shape.
2. For Maps URL: navigate, extract canonical name + address from the place
   panel and capture the resulting place URL.
3. For article URL: fetch HTML, extract candidate name(s) via JSON-LD
   Restaurant markup, og:title, or the H1 heading — title and og tags only,
   no LLM fallback. Then run the name through the Maps-search path.
4. For plain name: search Maps, return up to 3 candidates with name +
   address + place URL.

``resolve_in_context`` is the whole strategy against an already-open browser
context; ``save_restaurant.py`` calls it so a save is one browser session.

Output: JSON to stdout.
    {"kind": "single", "place": {name, address, url}}
    {"kind": "candidates", "candidates": [{name, address, url}, ...]}
    {"kind": "error", "error": "<reason>"}
"""
from __future__ import annotations

import argparse
import html
import json
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))

from browser import maps_context  # noqa: E402

MAPS_HOSTS = ("google.com/maps", "maps.google.com", "maps.app.goo.gl", "goo.gl/maps")
ARTICLE_BLOCKLIST = ("youtube.com", "youtu.be", "instagram.com", "tiktok.com", "twitter.com", "x.com")


def is_maps_url(s: str) -> bool:
    return any(h in s for h in MAPS_HOSTS)


def is_url(s: str) -> bool:
    return s.startswith("http://") or s.startswith("https://")


def http_get(url: str, timeout: int = 15) -> str:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        encoding = r.headers.get_content_charset() or "utf-8"
    try:
        return raw.decode(encoding, errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


def extract_from_article(article_url: str) -> list[str]:
    """Pull candidate restaurant names from an article page.

    Returns a list of candidate strings (possibly empty), best-first.
    """
    try:
        body = http_get(article_url)
    except Exception:
        return []

    candidates: list[str] = []

    # JSON-LD Restaurant markup
    for m in re.finditer(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        body,
        flags=re.DOTALL | re.IGNORECASE,
    ):
        try:
            data = json.loads(m.group(1).strip())
        except Exception:
            continue
        for item in data if isinstance(data, list) else [data]:
            if not isinstance(item, dict):
                continue
            t = item.get("@type")
            if isinstance(t, list):
                t = next((x for x in t if x), None)
            if isinstance(t, str) and t.lower() in ("restaurant", "foodestablishment", "localbusiness"):
                name = item.get("name")
                if isinstance(name, str) and name.strip():
                    candidates.append(name.strip())

    # og:title — often "Restaurant Name | Publication"
    m = re.search(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']', body, re.IGNORECASE)
    if m:
        title = html.unescape(m.group(1)).strip()
        # Strip publication suffixes
        for sep in (" | ", " - ", " — ", " – "):
            if sep in title:
                title = title.split(sep)[0].strip()
                break
        if title:
            candidates.append(title)

    # H1
    m = re.search(r"<h1[^>]*>(.*?)</h1>", body, flags=re.DOTALL | re.IGNORECASE)
    if m:
        text = re.sub(r"<[^>]+>", "", m.group(1))
        text = html.unescape(text).strip()
        if text:
            candidates.append(text)

    # Dedupe preserving order
    seen: set[str] = set()
    out: list[str] = []
    for c in candidates:
        key = c.lower()
        if key not in seen and len(c) < 120:
            seen.add(key)
            out.append(c)
    return out


def maps_search(ctx, query: str, top: int = 3) -> list[dict[str, str]]:
    """Run a Google Maps search and return candidate places."""
    page = ctx.new_page()
    url = f"https://www.google.com/maps/search/{urllib.parse.quote(query)}"
    page.goto(url, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(3000)

    # Single-result auto-pivot: URL stays /maps/search/ but the place panel
    # is fully rendered (h1 + address button). Wait briefly for Maps to
    # rewrite the URL to /maps/place/ before reading.
    if page.locator('button[data-item-id="address"]').count() > 0:
        try:
            page.wait_for_url("**/maps/place/**", timeout=4000)
        except Exception:
            pass
        place = parse_place_page(page)
        page.close()
        return [place] if place and place.get("name") else []

    results: list[dict[str, str]] = []
    cards = page.locator("a.hfpxzc").all()
    for card in cards[: top * 2]:
        try:
            href = card.get_attribute("href") or ""
            name = card.get_attribute("aria-label") or ""
        except Exception:
            continue
        if not href or "/place/" not in href or not name:
            continue
        # Address: sibling node in the card. Grab the parent container's text.
        address = ""
        try:
            container = card.locator("xpath=ancestor::div[contains(@class,'Nv2PK')][1]").first
            txt = container.inner_text(timeout=1500) or ""
            # First line is name, second is rating, address often appears later
            for line in txt.split("\n"):
                line = line.strip()
                if not line or line == name:
                    continue
                if re.search(r"\d", line) and "·" not in line and len(line) < 120 and "review" not in line.lower():
                    address = line
                    break
        except Exception:
            pass
        results.append({"name": name.strip(), "address": address, "url": href})
        if len(results) >= top:
            break

    page.close()
    return results


def parse_place_page(page) -> dict[str, str] | None:
    """Extract canonical name + address + URL from a /maps/place/ page."""
    try:
        page.wait_for_selector("h1", timeout=8000)
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
        # aria-label is "Address: <street>, <city>, <state> <zip>"
        address = re.sub(r"^Address:\s*", "", address)
    except Exception:
        pass
    return {"name": name, "address": address, "url": page.url}


def resolve_maps_url(ctx, url: str) -> dict[str, str] | None:
    page = ctx.new_page()
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(2000)
    except Exception:
        page.close()
        return None
    place = parse_place_page(page)
    page.close()
    return place


def resolve_in_context(ctx, query: str, top: int = 3) -> dict[str, Any]:
    """Run the full resolve strategy against an open browser context."""
    q = query.strip()
    if not q:
        return {"kind": "error", "error": "empty_query"}

    if is_maps_url(q):
        place = resolve_maps_url(ctx, q)
        if place and place.get("name"):
            return {"kind": "single", "place": place}
        return {"kind": "error", "error": "maps_url_unresolved"}

    if is_url(q):
        if any(b in q for b in ARTICLE_BLOCKLIST):
            return {"kind": "error", "error": "unsupported_url_host"}
        names = extract_from_article(q)
        if not names:
            return {"kind": "error", "error": "no_name_extracted"}
        # Try the best candidate first
        results = maps_search(ctx, names[0], top=top)
        if not results:
            return {"kind": "error", "error": "no_maps_results", "tried": names[0]}
        if len(results) == 1:
            return {"kind": "single", "place": results[0], "extracted_name": names[0]}
        return {"kind": "candidates", "candidates": results, "extracted_name": names[0]}

    # Plain name
    results = maps_search(ctx, q, top=top)
    if not results:
        return {"kind": "error", "error": "no_maps_results"}
    if len(results) == 1:
        return {"kind": "single", "place": results[0]}
    return {"kind": "candidates", "candidates": results}


def main() -> int:
    ap = argparse.ArgumentParser(description="Resolve a name, Maps URL or article URL to a Google Maps place.")
    ap.add_argument("query", help="Restaurant name, Maps URL, or article URL")
    ap.add_argument("--top", type=int, default=3, help="Max candidates to return for a name search")
    ap.add_argument("--watch", action="store_true", help="Run with a visible browser window")
    args = ap.parse_args()

    with maps_context(headless=not args.watch) as ctx:
        result = resolve_in_context(ctx, args.query, top=args.top)
    print(json.dumps(result))
    return 0 if result["kind"] in ("single", "candidates") else 1


if __name__ == "__main__":
    raise SystemExit(main())
