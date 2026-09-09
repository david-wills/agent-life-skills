#!/usr/bin/env python3
"""
Fetch RSS feeds for the morning news digest, dedupe, and emit JSON.

Usage:
  fetch_feeds.py [--hours N] [--out PATH]

Defaults: last 24 hours of items, JSON to stdout.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from html import unescape
from pathlib import Path
from typing import Iterable

import feedparser
import yaml
from dateutil import parser as dateparser


# The feed list is data, not code — see feeds.local.yaml (feeds.example.yaml
# is the published template). Keeping it out of here is what
# lets this script be published unchanged: the mechanics are generic, the choice of
# outlets is personal.
#
# .resolve() matters: this skill is reached through a symlink, so without it the
# parent lookup lands next to the symlink rather than in the repo (CONVENTIONS.md 1).
FEEDS_FILE = Path(__file__).resolve().parent.parent / "feeds.local.yaml"
EXAMPLE_FILE = FEEDS_FILE.with_name("feeds.example.yaml")


def load_feeds(path: Path = FEEDS_FILE) -> tuple[list[dict], dict[str, int]]:
    """Return (feeds, source_priority) from the YAML feed list.

    A missing or malformed file is fatal on purpose. Defaulting to a built-in
    list would let a typo silently produce a thin digest that looks like a quiet
    news day rather than a broken config.
    """
    if not path.is_file():
        # A fresh clone has the example but not the local copy. Say what to do
        # rather than just what is missing.
        hint = f"\n  cp {EXAMPLE_FILE.name} {path.name}   # then edit it" if EXAMPLE_FILE.is_file() else ""
        raise SystemExit(f"feed list not found: {path}{hint}")
    doc = yaml.safe_load(path.read_text()) or {}
    feeds = doc.get("feeds") or []
    if not feeds:
        raise SystemExit(f"no feeds defined in {path}")
    priority = {name: i for i, name in enumerate(doc.get("priority") or [])}
    # An unranked source sorts last rather than crashing the dedupe comparison.
    return feeds, priority


FEEDS, SOURCE_PRIORITY = load_feeds()

HTML_TAG_RE = re.compile(r"<[^>]+>")
WHITESPACE_RE = re.compile(r"\s+")


@dataclass
class Item:
    title: str
    url: str
    source: str
    section: str
    published_utc: str | None
    summary: str


def clean_text(raw: str) -> str:
    if not raw:
        return ""
    text = unescape(HTML_TAG_RE.sub(" ", raw))
    return WHITESPACE_RE.sub(" ", text).strip()


def parse_date(entry) -> datetime | None:
    for key in ("published", "updated", "created"):
        raw = entry.get(key)
        if not raw:
            continue
        try:
            dt = dateparser.parse(raw)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except (ValueError, TypeError):
            continue
    return None


def fetch_one(feed_cfg: dict) -> list[Item]:
    parsed = feedparser.parse(feed_cfg["url"])
    items: list[Item] = []
    for entry in parsed.entries:
        link = entry.get("link")
        title = clean_text(entry.get("title", ""))
        if not link or not title:
            continue
        summary = clean_text(entry.get("summary") or entry.get("description") or "")
        if len(summary) > 600:
            summary = summary[:600].rsplit(" ", 1)[0] + "…"
        published = parse_date(entry)
        items.append(Item(
            title=title,
            url=link,
            source=feed_cfg["source"],
            section=feed_cfg["section"],
            published_utc=published.isoformat() if published else None,
            summary=summary,
        ))
    return items


def fetch_all(feeds: Iterable[dict], timeout_s: int = 20) -> tuple[list[Item], list[dict]]:
    items: list[Item] = []
    errors: list[dict] = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(fetch_one, f): f for f in feeds}
        for future in as_completed(futures, timeout=timeout_s * len(futures) // 8 + timeout_s):
            cfg = futures[future]
            try:
                items.extend(future.result(timeout=timeout_s))
            except Exception as exc:
                errors.append({
                    "source": cfg["source"],
                    "section": cfg["section"],
                    "url": cfg["url"],
                    "error": f"{type(exc).__name__}: {exc}",
                })
    return items, errors


def normalize_title(title: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace — for near-dup detection."""
    t = re.sub(r"[^\w\s]", " ", title.lower())
    return WHITESPACE_RE.sub(" ", t).strip()


def dedupe(items: list[Item]) -> list[Item]:
    """Remove exact-URL dupes and near-duplicate titles.

    When the same story appears in multiple outlets, keep the highest-priority
    source (WSJ > NYT > WaPo > others).
    """
    by_url: dict[str, Item] = {}
    for it in items:
        if it.url not in by_url or SOURCE_PRIORITY.get(it.source, 99) < SOURCE_PRIORITY.get(by_url[it.url].source, 99):
            by_url[it.url] = it

    by_title: dict[str, Item] = {}
    for it in by_url.values():
        key = normalize_title(it.title)
        if not key:
            continue
        existing = by_title.get(key)
        if existing is None:
            by_title[key] = it
            continue
        if SOURCE_PRIORITY.get(it.source, 99) < SOURCE_PRIORITY.get(existing.source, 99):
            by_title[key] = it
    return sorted(by_title.values(), key=lambda x: (SOURCE_PRIORITY.get(x.source, 99), x.published_utc or ""))


def filter_recent(items: list[Item], hours: int) -> list[Item]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    kept = []
    for it in items:
        if it.published_utc is None:
            kept.append(it)
            continue
        try:
            pub = datetime.fromisoformat(it.published_utc)
        except ValueError:
            kept.append(it)
            continue
        if pub >= cutoff:
            kept.append(it)
    return kept


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=24, help="Keep items published within N hours (default 24)")
    ap.add_argument("--out", default="-", help="Output path, or - for stdout")
    ap.add_argument("--no-filter", action="store_true", help="Skip the recency filter")
    args = ap.parse_args()

    start = time.time()
    raw, errors = fetch_all(FEEDS)
    deduped = dedupe(raw)
    kept = deduped if args.no_filter else filter_recent(deduped, args.hours)

    payload = {
        "fetched_at_utc": datetime.now(timezone.utc).isoformat(),
        "window_hours": None if args.no_filter else args.hours,
        "counts": {
            "raw": len(raw),
            "after_dedupe": len(deduped),
            "after_recency_filter": len(kept),
            "feed_errors": len(errors),
        },
        "elapsed_s": round(time.time() - start, 2),
        "errors": errors,
        "items": [asdict(it) for it in kept],
    }

    out_text = json.dumps(payload, indent=2, ensure_ascii=False)
    if args.out == "-":
        print(out_text)
    else:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(out_text)
        print(f"Wrote {len(kept)} items to {args.out} (raw {len(raw)}, deduped {len(deduped)}, errors {len(errors)})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
