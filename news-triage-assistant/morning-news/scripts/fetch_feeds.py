#!/usr/bin/env python3
"""
Fetch RSS feeds for the morning news digest, dedupe, and emit JSON.

Usage:
  fetch_feeds.py [--hours N] [--out PATH] [--feeds PATH] [--timeout S] [--no-filter]

Defaults: last 24 hours of items, JSON to stdout, feed list from
feeds.local.yaml next to this skill.

Needs feedparser, python-dateutil and PyYAML (requirements.txt); the stdlib
is not enough for feed parsing.
"""
from __future__ import annotations

import argparse
import json
import re
import socket
import sys
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from html import unescape
from pathlib import Path
from typing import Iterable

import feedparser
import yaml
from dateutil import parser as dateparser


# The feed list is data, not code. feeds.local.yaml is gitignored and
# feeds.example.yaml is the published template. Keeping the list out of here is
# what lets this script be published unchanged: the mechanics are generic, the
# choice of outlets is personal.
#
# .resolve() so the lookup lands in the real skill directory even when the
# script is reached through a symlink.
SKILL_DIR = Path(__file__).resolve().parent.parent
DEFAULT_FEEDS_FILE = SKILL_DIR / "feeds.local.yaml"
EXAMPLE_FEEDS_FILE = SKILL_DIR / "feeds.example.yaml"

MAX_WORKERS = 8

HTML_TAG_RE = re.compile(r"<[^>]+>")
WHITESPACE_RE = re.compile(r"\s+")


def load_feeds(path: Path) -> tuple[list[dict], dict[str, int]]:
    """Return (feeds, source_priority) from the YAML feed list.

    A missing or malformed file is fatal on purpose. Defaulting to a built-in
    list would let a typo silently produce a thin digest that looks like a quiet
    news day rather than a broken config.
    """
    if not path.is_file():
        # A fresh clone has the example but not the local copy. Say what to do
        # rather than just what is missing.
        hint = ""
        if EXAMPLE_FEEDS_FILE.is_file():
            hint = f"\n  cp {EXAMPLE_FEEDS_FILE} {path}   # then edit it"
        raise SystemExit(f"feed list not found: {path}{hint}")
    doc = yaml.safe_load(path.read_text()) or {}
    feeds = doc.get("feeds") or []
    if not feeds:
        raise SystemExit(f"no feeds defined in {path}")
    for i, f in enumerate(feeds):
        for key in ("source", "section", "url"):
            if not f.get(key):
                raise SystemExit(f"feed #{i + 1} in {path} is missing '{key}'")
    priority = {name: i for i, name in enumerate(doc.get("priority") or [])}
    # An unranked source sorts last rather than crashing the dedupe comparison.
    return feeds, priority


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
        except (ValueError, TypeError, OverflowError):
            continue
    return None


def fetch_one(feed_cfg: dict) -> list[Item]:
    parsed = feedparser.parse(feed_cfg["url"])
    # feedparser swallows transport errors into `bozo` and HTTP failures into
    # `status`; surface a feed that returned nothing as an error, not a quiet zero.
    status = parsed.get("status")
    if not parsed.entries and isinstance(status, int) and status >= 400:
        raise RuntimeError(f"HTTP {status}")
    if not parsed.entries and getattr(parsed, "bozo", False):
        exc = getattr(parsed, "bozo_exception", None)
        raise RuntimeError(f"{type(exc).__name__}: {exc}" if exc else "feed returned no entries")
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


def _feed_error(cfg: dict, message: str) -> dict:
    return {"source": cfg["source"], "section": cfg["section"], "url": cfg["url"], "error": message}


def fetch_all(feeds: Iterable[dict], timeout_s: int = 20) -> tuple[list[Item], list[dict]]:
    """Pull every feed in parallel. A hung feed becomes an error, never a hang.

    Two layers: the socket default timeout bounds each request feedparser makes,
    and an overall deadline bounds the whole batch. Feeds still outstanding at
    the deadline are recorded as errors and abandoned; the executor is shut
    down without waiting on them.
    """
    feeds = list(feeds)
    items: list[Item] = []
    errors: list[dict] = []
    if not feeds:
        return items, errors

    # feedparser has no timeout argument; it honours the socket default.
    socket.setdefaulttimeout(timeout_s)

    rounds = -(-len(feeds) // MAX_WORKERS)  # ceil
    deadline = time.monotonic() + timeout_s * (rounds + 1)

    pool = ThreadPoolExecutor(max_workers=MAX_WORKERS)
    futures = {pool.submit(fetch_one, f): f for f in feeds}
    pending = set(futures)
    try:
        while pending:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            done, pending = wait(pending, timeout=remaining, return_when=FIRST_COMPLETED)
            for future in done:
                cfg = futures[future]
                try:
                    items.extend(future.result())
                except Exception as exc:  # noqa: BLE001 - one bad feed must not kill the run
                    errors.append(_feed_error(cfg, f"{type(exc).__name__}: {exc}"))
        for future in pending:
            future.cancel()
            errors.append(_feed_error(futures[future], f"TimeoutError: no response within {timeout_s}s"))
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    return items, errors


def normalize_title(title: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace, for near-dup detection."""
    t = re.sub(r"[^\w\s]", " ", title.lower())
    return WHITESPACE_RE.sub(" ", t).strip()


def dedupe(items: list[Item], priority: dict[str, int]) -> list[Item]:
    """Remove exact-URL dupes and near-duplicate titles.

    When the same story appears in multiple outlets, keep the source that
    ranks highest in the feed list's `priority` order.
    """
    def rank(it: Item) -> int:
        return priority.get(it.source, 99)

    by_url: dict[str, Item] = {}
    for it in items:
        if it.url not in by_url or rank(it) < rank(by_url[it.url]):
            by_url[it.url] = it

    by_title: dict[str, Item] = {}
    for it in by_url.values():
        key = normalize_title(it.title)
        if not key:
            continue
        existing = by_title.get(key)
        if existing is None or rank(it) < rank(existing):
            by_title[key] = it
    return sorted(by_title.values(), key=lambda x: (rank(x), x.published_utc or ""))


def filter_recent(items: list[Item], hours: int) -> tuple[list[Item], int]:
    """Keep items published within the window.

    Items with no parseable date are kept regardless, and counted, because
    dropping them would silently blank any feed that omits dates. The cost is
    that such a feed re-surfaces its backlog every run; the count in the output
    is how you notice.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    kept: list[Item] = []
    undated = 0
    for it in items:
        if it.published_utc is None:
            kept.append(it)
            undated += 1
            continue
        try:
            pub = datetime.fromisoformat(it.published_utc)
        except ValueError:
            kept.append(it)
            undated += 1
            continue
        if pub >= cutoff:
            kept.append(it)
    return kept, undated


def main() -> int:
    ap = argparse.ArgumentParser(description="Fetch, dedupe and window the digest's RSS feeds.")
    ap.add_argument("--hours", type=int, default=24, help="Keep items published within N hours (default 24)")
    ap.add_argument("--out", default="-", help="Output path, or - for stdout (default)")
    ap.add_argument("--feeds", default=None,
                    help=f"Feed list YAML (default: {DEFAULT_FEEDS_FILE.name} next to this skill)")
    ap.add_argument("--timeout", type=int, default=20,
                    help="Per-request timeout in seconds; also scales the overall deadline (default 20)")
    ap.add_argument("--no-filter", action="store_true", help="Skip the recency filter")
    args = ap.parse_args()

    feeds_file = Path(args.feeds).expanduser() if args.feeds else DEFAULT_FEEDS_FILE
    feeds, priority = load_feeds(feeds_file)

    start = time.time()
    raw, errors = fetch_all(feeds, timeout_s=args.timeout)
    deduped = dedupe(raw, priority)
    if args.no_filter:
        kept, undated = deduped, sum(1 for it in deduped if it.published_utc is None)
    else:
        kept, undated = filter_recent(deduped, args.hours)

    payload = {
        "fetched_at_utc": datetime.now(timezone.utc).isoformat(),
        "window_hours": None if args.no_filter else args.hours,
        "counts": {
            "feeds": len(feeds),
            "raw": len(raw),
            "after_dedupe": len(deduped),
            "after_recency_filter": len(kept),
            "undated_kept": undated,
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
        print(f"Wrote {len(kept)} items to {args.out} (raw {len(raw)}, deduped {len(deduped)}, "
              f"undated {undated}, errors {len(errors)})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
