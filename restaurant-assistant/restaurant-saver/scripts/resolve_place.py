#!/usr/bin/env python3
"""Resolve a restaurant input to a canonical Google Maps place.

Accepts one of:
- A Google Maps URL (maps.app.goo.gl/..., google.com/maps/place/..., etc.)
- An article URL about a restaurant (NYT, Eater, LA Times, blogs)
- A plain restaurant name ("Bestia", "Sushi Park")

Strategy:
1. Detect input type by URL host / shape.
2. For Maps URL: navigate, extract canonical name + address from the place
   panel and capture the resulting place URL.
3. For article URL: fetch HTML, extract candidate name(s) via og:title,
   JSON-LD Restaurant markup, or H1 heading. Fall back to gemini if all
   heuristics fail. Then run the name through the Maps-search path.
4. For plain name: search Maps, return up to 3 candidates with name +
   address + place URL.

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
from typing import Any

from browser import maps_context

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


def gemini_extract_name(article_url: str) -> str | None:
    """Last-resort: ask Gemini to extract the primary restaurant name from an article."""
    import subprocess
    try:
        body = http_get(article_url)
    except Exception:
        return None
    text = re.sub(r"<script[\s\S]*?</script>", " ", body, flags=re.IGNORECASE)
    text = re.sub(r"<style[\s\S]*?</style>", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", html.unescape(text)).strip()[:8000]
    prompt = (
        "Extract the primary restaurant name from this article. Reply with ONLY the "
        "restaurant name, no quotes, no commentary. If the article is not about a "
        "specific restaurant, reply with the single word NONE.\n\n"
        f"Article text:\n{text}"
    )
    try:
        r = subprocess.run(
            ["gemini", "-p", prompt, "-m", "gemini-2.5-flash"],
            capture_output=True,
            text=True,
            timeout=45,
        )
    except Exception:
        return None
    if r.returncode != 0:
        return None
    name = (r.stdout or "").strip().splitlines()[-1].strip().strip('"').strip("'")
    if not name or name.upper() == "NONE" or len(name) > 100:
        return None
    return name


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
        # aria-label is often "Address: 2121 E 7th Pl, Los Angeles, CA 90021"
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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("query", help="Restaurant name, Maps URL, or article URL")
    ap.add_argument("--top", type=int, default=3, help="Max candidates to return for a name search")
    ap.add_argument("--watch", action="store_true", help="Run with a visible browser window")
    args = ap.parse_args()

    q = args.query.strip()
    if not q:
        print(json.dumps({"kind": "error", "error": "empty_query"}))
        return 1

    with maps_context(headless=not args.watch) as ctx:
        if is_maps_url(q):
            place = resolve_maps_url(ctx, q)
            if place and place.get("name"):
                print(json.dumps({"kind": "single", "place": place}))
                return 0
            print(json.dumps({"kind": "error", "error": "maps_url_unresolved"}))
            return 1

        if is_url(q):
            if any(b in q for b in ARTICLE_BLOCKLIST):
                print(json.dumps({"kind": "error", "error": "unsupported_url_host"}))
                return 1
            names = extract_from_article(q)
            if not names:
                gemini = gemini_extract_name(q)
                if gemini:
                    names = [gemini]
            if not names:
                print(json.dumps({"kind": "error", "error": "no_name_extracted"}))
                return 1
            # Try the best candidate first
            results = maps_search(ctx, names[0], top=args.top)
            if not results:
                print(json.dumps({"kind": "error", "error": "no_maps_results", "tried": names[0]}))
                return 1
            payload: dict[str, Any] = (
                {"kind": "single", "place": results[0], "extracted_name": names[0]}
                if len(results) == 1
                else {"kind": "candidates", "candidates": results, "extracted_name": names[0]}
            )
            print(json.dumps(payload))
            return 0

        # Plain name
        results = maps_search(ctx, q, top=args.top)
        if not results:
            print(json.dumps({"kind": "error", "error": "no_maps_results"}))
            return 1
        if len(results) == 1:
            print(json.dumps({"kind": "single", "place": results[0]}))
        else:
            print(json.dumps({"kind": "candidates", "candidates": results}))
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
