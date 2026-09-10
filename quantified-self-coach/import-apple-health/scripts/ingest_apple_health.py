#!/usr/bin/env python3
"""Ingest Apple Health daily Markdown files into the FTS index.

Reads the per-day files written by ``parse_hae_payload.py`` under
``<data_root>/knowledge/apple-health/YYYY-MM-DD.md`` and upserts each into
``<data_root>/knowledge/index.db`` at id ``apple-health:<YYYY-MM-DD>``,
source ``apple-health``, source_type ``daily``.

Idempotent — re-running for the same dates updates rows in place.

For *structured* value lookup, downstream consumers should read the sidecar
JSON at ``knowledge/apple-health/.sidecar/<date>.json`` (written by the
parser) — this ingester only feeds full-text search.
"""

from __future__ import annotations

import argparse
import os
import datetime as dt
import re
import sqlite3
import sys
from pathlib import Path

# Shared helpers live in lib/ at the repo root; walk up so this works from any cwd.
_LIB = next((p / "lib" for p in Path(__file__).resolve().parents if (p / "lib" / "skill_config.py").is_file()), None)
if _LIB is None:
    raise SystemExit("cannot find the repo-root lib/ directory; run from a clone of the repo, not a copied file")
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))
from fts_schema import ENTRIES_DDL as FTS_SCHEMA  # noqa: E402

SOURCE_NAME = "apple-health"

WEEKDAYS = [
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
]
DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")



def _iso_utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _ensure_db(db_path: Path | None) -> sqlite3.Connection:
    if db_path is None:
        conn = sqlite3.connect(":memory:")
    else:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(FTS_SCHEMA)
    return conn


def _upsert(conn: sqlite3.Connection, row: dict) -> None:
    conn.execute("DELETE FROM entries WHERE id = ?", (row["id"],))
    conn.execute(
        """
        INSERT INTO entries
            (id, source, source_type, title, author, url, readwise_url,
             captured_at, ingested_at, status, tags, note, body, path)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            row["id"],
            row["source"],
            row["source_type"],
            row["title"] or "",
            row["author"] or "",
            row["url"] or "",
            row["readwise_url"] or "",
            row["captured_at"] or "",
            row["ingested_at"],
            row["status"],
            " ".join(row["tags"]),
            row["note"] or "",
            row["body"] or "",
            str(row["path"]),
        ),
    )


def _split_frontmatter(text: str) -> tuple[str, str]:
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.DOTALL)
    if not m:
        return "", text
    return m.group(1), m.group(2).lstrip()


def _date_from_filename(path: Path) -> dt.date | None:
    m = DATE_RE.match(path.stem)
    if not m:
        return None
    try:
        return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def _captured_at(d: dt.date) -> str:
    """Noon on that day in the machine's local timezone, whatever it is."""
    local_tz = dt.datetime.now().astimezone().tzinfo
    return dt.datetime(d.year, d.month, d.day, 12, tzinfo=local_tz).isoformat()


def ingest_files(
    kb_root: Path,
    since: dt.date | None = None,
    only: list[str] | None = None,
    default_status: str = "confirmed",
    dry_run: bool = False,
) -> tuple[int, int]:
    src_dir = kb_root / "apple-health"
    if not src_dir.is_dir():
        return 0, 0

    only_set = set(only) if only else None
    candidates: list[tuple[dt.date, Path]] = []
    for f in src_dir.glob("*.md"):
        d = _date_from_filename(f)
        if d is None:
            continue
        if since and d < since:
            continue
        if only_set and f.stem not in only_set:
            continue
        candidates.append((d, f))
    candidates.sort()

    conn = _ensure_db(None if dry_run else kb_root / "index.db")
    ingested_at = _iso_utc_now()
    written = 0
    skipped = 0

    try:
        for d, f in candidates:
            try:
                text = f.read_text(encoding="utf-8")
            except OSError:
                skipped += 1
                continue
            _, body = _split_frontmatter(text)
            if not body.strip():
                skipped += 1
                continue
            date_str = d.isoformat()
            weekday = WEEKDAYS[d.weekday()]
            title = f"Apple Health — {date_str} ({weekday})"

            row = {
                "id": f"{SOURCE_NAME}:{date_str}",
                "source": SOURCE_NAME,
                "source_type": "daily",
                "title": title,
                "author": "",
                "url": "",
                "readwise_url": "",
                "captured_at": _captured_at(d),
                "ingested_at": ingested_at,
                "status": default_status,
                "tags": [],
                "note": "",
                "body": body,
                "path": f,
            }
            if dry_run:
                print(f"would ingest {row['id']} <- {f.name}")
            _upsert(conn, row)
            written += 1
        conn.commit()
    finally:
        conn.close()

    return written, skipped


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--kb-root", default=None, help="Index root. Default: <data_root>/knowledge")
    p.add_argument(
        "--since", help="Only process dates on or after YYYY-MM-DD."
    )
    p.add_argument(
        "--days",
        type=int,
        help="Only process the last N days (overrides --since).",
    )
    p.add_argument(
        "--date",
        action="append",
        default=[],
        help="Process only this date (YYYY-MM-DD). May be repeated.",
    )
    p.add_argument(
        "--all",
        action="store_true",
        help="Process every file in knowledge/apple-health/ (backfill mode).",
    )
    p.add_argument("--dry-run", action="store_true", help="List what would be ingested; write nothing.")
    return p.parse_args()


def main() -> int:
    os.umask(0o077)  # health data: every file this run creates is owner-only
    args = parse_args()
    if args.kb_root:
        kb_root = Path(args.kb_root).expanduser()
    else:
        from skill_config import data_root
        kb_root = data_root() / "knowledge"
    since: dt.date | None = None

    if args.days is not None:
        since = dt.date.today() - dt.timedelta(days=args.days)
    elif args.since:
        try:
            since = dt.date.fromisoformat(args.since)
        except ValueError:
            print(
                f"--since must be YYYY-MM-DD, got {args.since!r}", file=sys.stderr
            )
            return 2
    elif not args.all and not args.date:
        # Default for cron use: last 7 days.
        since = dt.date.today() - dt.timedelta(days=7)

    only = args.date or None
    written, skipped = ingest_files(kb_root, since=since, only=only, dry_run=args.dry_run)
    prefix = "dry-run " if args.dry_run else ""
    print(
        f"{prefix}apple-health: ingested={written} skipped={skipped} kb_root={kb_root}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
