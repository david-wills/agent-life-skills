#!/usr/bin/env python3
"""Import Readwise Reader archive documents into a local JSON dump.

Defaults to a full archive backfill including HTML content so downstream
summary generation can run without additional API calls. Pass --updated-after
or --hours for incremental syncs.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

API_URL = "https://readwise.io/api/v3/list/"
PAGE_SLEEP_SECONDS = 0.25


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
    return slug or "reader-import"


def _token_from_env() -> str | None:
    return os.getenv("READWISE_TOKEN") or os.getenv("READWISE_API_TOKEN")


def _request_json(token: str, params: dict[str, str]) -> dict[str, Any]:
    url = API_URL + ("?" + urllib.parse.urlencode(params) if params else "")
    req = urllib.request.Request(url, headers={"Authorization": f"Token {token}"})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"Reader request failed: HTTP {exc.code} {exc.reason}\n{detail}") from exc


def fetch_archive(
    token: str,
    updated_after: str | None,
    with_html: bool,
) -> list[dict[str, Any]]:
    docs: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        params: dict[str, str] = {"location": "archive"}
        if updated_after:
            params["updatedAfter"] = updated_after
        if with_html:
            params["withHtmlContent"] = "true"
        if cursor:
            params["pageCursor"] = cursor
        payload = _request_json(token, params)
        docs.extend(payload.get("results", []))
        cursor = payload.get("nextPageCursor")
        if not cursor:
            break
        time.sleep(PAGE_SLEEP_SECONDS)
    return docs


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Import Readwise Reader archive into a local JSON dump.")
    parser.add_argument("--hours", type=float, help="Rolling window in hours (e.g. 30 for daily delta).")
    parser.add_argument("--updated-after", help="ISO 8601 timestamp for updatedAfter. Overrides --hours.")
    parser.add_argument("--output-dir", default="imports/reader", help="Directory to write the JSON dump into.")
    parser.add_argument("--no-html", action="store_true", help="Omit withHtmlContent (smaller payload, no fallback summary input).")
    parser.add_argument("--token", help="Readwise token. Defaults to READWISE_TOKEN or READWISE_API_TOKEN.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    token = args.token or _token_from_env()
    if not token:
        print("Missing Readwise token. Set READWISE_TOKEN or READWISE_API_TOKEN, or pass --token.", file=sys.stderr)
        return 2

    updated_after: str | None = None
    if args.updated_after:
        updated_after = _iso_utc(_parse_iso(args.updated_after))
    elif args.hours is not None:
        updated_after = _iso_utc(_utc_now() - dt.timedelta(hours=args.hours))

    synced_at = _iso_utc(_utc_now())
    docs = fetch_archive(token, updated_after, with_html=not args.no_html)

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = _slug(synced_at)
    json_path = output_dir / f"reader-archive-{stamp}.json"

    payload = {
        "synced_at": synced_at,
        "updated_after": updated_after,
        "with_html": not args.no_html,
        "doc_count": len(docs),
        "docs": docs,
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")

    print(f"Wrote {json_path}")
    print(f"Imported {len(docs)} archived Reader docs")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
