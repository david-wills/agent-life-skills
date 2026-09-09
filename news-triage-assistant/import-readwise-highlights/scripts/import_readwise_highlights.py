#!/usr/bin/env python3
"""Import recent Readwise highlights into local JSON and markdown files.

Defaults to a rolling 24-hour sync using Readwise's export API.

The importer and the ingester (`ingest_readwise.py`) stay two scripts on
purpose: a failed ingest never costs you the API pull.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path
from typing import Any

# Shared helpers live in lib/ at the repo root; walk up so this works from any cwd.
_LIB = next((p / "lib" for p in Path(__file__).resolve().parents if (p / "lib" / "skill_config.py").is_file()), None)
if _LIB is None:
    raise SystemExit("cannot find the repo-root lib/ directory; run from a clone of the repo, not a copied file")
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))
from skill_config import data_root  # noqa: E402

API_URL = "https://readwise.io/api/v2/export/"


def default_output_dir() -> Path:
    return data_root() / "imports" / "readwise"


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _iso_utc(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_iso(value: str) -> dt.datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = dt.datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _slug(value: str) -> str:
    cleaned = []
    for ch in value.lower():
        cleaned.append(ch if ch.isalnum() else "-")
    slug = "".join(cleaned).strip("-")
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug or "readwise-import"


def _token_from_env() -> str | None:
    return os.getenv("READWISE_TOKEN") or os.getenv("READWISE_API_TOKEN")


def _request_json(token: str, updated_after: str | None, page_cursor: str | None) -> dict[str, Any]:
    params: dict[str, str] = {}
    if updated_after:
        params["updatedAfter"] = updated_after
    if page_cursor:
        params["pageCursor"] = page_cursor
    url = API_URL + ("?" + urllib.parse.urlencode(params) if params else "")
    req = urllib.request.Request(url, headers={"Authorization": f"Token {token}"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"Readwise request failed: HTTP {exc.code} {exc.reason}\n{detail}") from exc


def fetch_books(token: str, updated_after: str | None) -> list[dict[str, Any]]:
    books: list[dict[str, Any]] = []
    page_cursor: str | None = None
    while True:
        payload = _request_json(token, updated_after, page_cursor)
        books.extend(payload.get("results", []))
        page_cursor = payload.get("nextPageCursor")
        if not page_cursor:
            break
    return books


def flatten_highlights(books: list[dict[str, Any]]) -> list[dict[str, Any]]:
    highlights: list[dict[str, Any]] = []
    for book in books:
        book_meta = {
            "user_book_id": book.get("user_book_id"),
            "book_id": book.get("user_book_id"),
            "title": book.get("title"),
            "author": book.get("author"),
            "category": book.get("category"),
            "source": book.get("source"),
            "source_url": book.get("source_url"),
            "readwise_url": book.get("readwise_url"),
            "cover_image_url": book.get("cover_image_url"),
            "external_id": book.get("external_id"),
            "document_note": book.get("document_note"),
            "summary": book.get("summary"),
        }
        for highlight in book.get("highlights", []):
            item = dict(book_meta)
            item.update(
                {
                    "highlight_id": highlight.get("id"),
                    "text": highlight.get("text"),
                    "note": highlight.get("note"),
                    "color": highlight.get("color"),
                    "location": highlight.get("location"),
                    "location_type": highlight.get("location_type"),
                    "highlighted_at": highlight.get("highlighted_at"),
                    "created_at": highlight.get("created_at"),
                    "updated_at": highlight.get("updated_at"),
                    "highlight_url": highlight.get("readwise_url"),
                    "is_deleted": highlight.get("is_deleted"),
                    "is_favorite": highlight.get("is_favorite"),
                    "is_discard": highlight.get("is_discard"),
                    "tags": highlight.get("tags", []),
                    "url": highlight.get("url"),
                    "external_highlight_id": highlight.get("external_id"),
                }
            )
            highlights.append(item)
    return highlights


def render_markdown(updated_after: str, synced_at: str, books: list[dict[str, Any]], highlights: list[dict[str, Any]]) -> str:
    books_by_key: dict[tuple[Any, Any], list[dict[str, Any]]] = defaultdict(list)
    book_order: list[tuple[Any, Any]] = []

    for highlight in highlights:
        key = (highlight.get("user_book_id"), highlight.get("title"))
        if key not in books_by_key:
            book_order.append(key)
        books_by_key[key].append(highlight)

    lines: list[str] = []
    lines.append("# Readwise highlights import")
    lines.append("")
    lines.append(f"- Synced at: {synced_at}")
    lines.append(f"- Updated after: {updated_after}")
    lines.append(f"- Books: {len(books)}")
    lines.append(f"- Highlights: {len(highlights)}")
    lines.append("")

    for key in book_order:
        group = books_by_key[key]
        first = group[0]
        title = first.get("title") or "Untitled"
        author = first.get("author")
        header = title if not author else f"{title} — {author}"
        lines.append(f"## {header}")
        if first.get("source_url"):
            lines.append(f"Source: {first['source_url']}")
        if first.get("readwise_url"):
            lines.append(f"Readwise: {first['readwise_url']}")
        lines.append("")
        for item in group:
            # `text` is null for some highlight kinds (e.g. image-only); never crash on it.
            lines.append(f"- {(item.get('text') or '').strip()}")
            meta_bits: list[str] = []
            if item.get("highlighted_at"):
                meta_bits.append(f"highlighted {item['highlighted_at']}")
            if item.get("note"):
                meta_bits.append(f"note: {item['note']}")
            if item.get("tags"):
                meta_bits.append(f"tags: {', '.join(map(str, item['tags']))}")
            if meta_bits:
                lines.append(f"  - {' | '.join(meta_bits)}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Import recent Readwise highlights into local files.")
    parser.add_argument("--hours", type=float, default=24.0, help="Rolling window to import, in hours. Default: 24.")
    parser.add_argument("--since", help="ISO 8601 timestamp for updatedAfter. Overrides --hours.")
    parser.add_argument("--output-dir", default=None,
                        help="Directory to write imported files into (default: <data_root>/imports/readwise).")
    parser.add_argument("--token", help="Readwise token. Defaults to READWISE_TOKEN or READWISE_API_TOKEN.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    token = args.token or _token_from_env()
    if not token:
        print("Missing Readwise token. Set READWISE_TOKEN or READWISE_API_TOKEN, or pass --token.", file=sys.stderr)
        return 2

    if args.since:
        updated_after = _iso_utc(_parse_iso(args.since))
    else:
        updated_after = _iso_utc(_utc_now() - dt.timedelta(hours=args.hours))

    synced_at = _iso_utc(_utc_now())
    books = fetch_books(token, updated_after)
    highlights = flatten_highlights(books)

    output_dir = Path(args.output_dir).expanduser() if args.output_dir else default_output_dir()
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = _slug(synced_at)
    json_path = output_dir / f"readwise-highlights-{stamp}.json"
    md_path = output_dir / f"readwise-highlights-{stamp}.md"

    payload = {
        "synced_at": synced_at,
        "updated_after": updated_after,
        "book_count": len(books),
        "highlight_count": len(highlights),
        "books": books,
        "highlights": highlights,
    }
    json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    md_path.write_text(render_markdown(updated_after, synced_at, books, highlights), encoding="utf-8")

    print(f"Wrote {json_path}")
    print(f"Wrote {md_path}")
    print(f"Imported {len(highlights)} highlights from {len(books)} books")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
