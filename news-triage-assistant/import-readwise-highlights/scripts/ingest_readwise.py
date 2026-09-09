#!/usr/bin/env python3
"""Ingest a raw Readwise export JSON file into the knowledge base.

Reads the JSON produced by `skills/import-readwise-highlights/scripts/import_readwise_highlights.py`,
writes per-highlight markdown files under `knowledge/readwise/<book_slug>/<highlight_id>.md`,
and upserts rows into the FTS5 index at `knowledge/index.db`.

Idempotent: re-ingesting the same highlight updates the file and index row in place.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

# Shared helpers live in the repo-root lib/ (CONVENTIONS.md 2).
from pathlib import Path as _P  # noqa: E402
_LIB = next(p / "lib" for p in _P(__file__).resolve().parents if (p / "lib" / "read_secret.py").is_file())
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))
import engagement


DEFAULT_KB_ROOT = Path.home() / ".openclaw" / "workspace" / "knowledge"


FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS entries USING fts5(
    id UNINDEXED,
    source UNINDEXED,
    source_type UNINDEXED,
    title,
    author,
    url UNINDEXED,
    readwise_url UNINDEXED,
    captured_at UNINDEXED,
    ingested_at UNINDEXED,
    status UNINDEXED,
    tags,
    note,
    body,
    path UNINDEXED,
    tokenize = 'unicode61 remove_diacritics 2'
);
"""


def _slug(value: str, max_len: int = 80) -> str:
    cleaned: list[str] = []
    for ch in (value or "").lower():
        cleaned.append(ch if ch.isalnum() else "-")
    slug = "".join(cleaned).strip("-")
    while "--" in slug:
        slug = slug.replace("--", "-")
    return (slug or "untitled")[:max_len]


def _iso_utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _yaml_scalar(value: Any) -> str:
    """Serialize a scalar into a safe YAML-quoted string."""
    if value is None:
        return '""'
    text = str(value)
    escaped = text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{escaped}"'


def _yaml_list(items: list[Any]) -> str:
    if not items:
        return "[]"
    parts = [_yaml_scalar(str(item)) for item in items]
    return "[" + ", ".join(parts) + "]"


def _frontmatter(meta: dict[str, Any]) -> str:
    lines = ["---"]
    for key, value in meta.items():
        if isinstance(value, list):
            lines.append(f"{key}: {_yaml_list(value)}")
        elif isinstance(value, bool):
            lines.append(f"{key}: {'true' if value else 'false'}")
        elif value is None or value == "":
            lines.append(f"{key}: \"\"")
        else:
            lines.append(f"{key}: {_yaml_scalar(value)}")
    lines.append("---")
    return "\n".join(lines)


def _tag_names(tags: list[Any]) -> list[str]:
    names: list[str] = []
    for tag in tags or []:
        if isinstance(tag, dict):
            name = tag.get("name") or tag.get("tag") or tag.get("label")
        else:
            name = tag
        if name:
            names.append(str(name))
    return names


def _ensure_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(FTS_SCHEMA)
    return conn


def _upsert(conn: sqlite3.Connection, row: dict[str, Any]) -> None:
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


def ingest_export(
    export_path: Path,
    kb_root: Path = DEFAULT_KB_ROOT,
    default_status: str = "provisional",
) -> tuple[int, int]:
    """Ingest a single Readwise export JSON file. Returns (written_count, skipped_count)."""
    payload = json.loads(export_path.read_text(encoding="utf-8"))
    highlights = payload.get("highlights") or []
    if not highlights and "books" in payload:
        # legacy shape: flatten on the fly
        flat: list[dict[str, Any]] = []
        for book in payload["books"]:
            book_meta = {k: book.get(k) for k in (
                "user_book_id", "title", "author", "category", "source",
                "source_url", "readwise_url",
            )}
            for highlight in book.get("highlights", []):
                item = dict(book_meta)
                item.update({
                    "highlight_id": highlight.get("id"),
                    "text": highlight.get("text"),
                    "note": highlight.get("note"),
                    "highlighted_at": highlight.get("highlighted_at"),
                    "created_at": highlight.get("created_at"),
                    "updated_at": highlight.get("updated_at"),
                    "highlight_url": highlight.get("readwise_url"),
                    "is_favorite": highlight.get("is_favorite"),
                    "tags": highlight.get("tags") or [],
                    "url": highlight.get("url"),
                })
                flat.append(item)
        highlights = flat

    ingested_at = _iso_utc_now()
    conn = _ensure_db(kb_root / "index.db")
    engagement.ensure_engagement_table(conn)
    written = 0
    skipped = 0

    try:
        for h in highlights:
            hl_id = h.get("highlight_id") or h.get("id")
            if not hl_id:
                skipped += 1
                continue
            if h.get("is_deleted") or h.get("is_discard"):
                skipped += 1
                continue

            book_title = h.get("title") or "Untitled"
            book_slug = _slug(book_title)
            book_id_suffix = h.get("book_id") or h.get("user_book_id")
            if book_id_suffix:
                book_slug = f"{book_slug}-{book_id_suffix}"

            rel_dir = Path("readwise") / book_slug
            out_dir = kb_root / rel_dir
            out_dir.mkdir(parents=True, exist_ok=True)

            md_path = out_dir / f"{hl_id}.md"
            tags = _tag_names(h.get("tags") or [])

            captured_at = (
                h.get("highlighted_at")
                or h.get("created_at")
                or h.get("updated_at")
                or ""
            )

            source_type = (h.get("category") or "highlights").lower()

            level = engagement.infer_for_readwise_highlight(h)

            meta = {
                "id": f"readwise:{hl_id}",
                "source": "readwise",
                "source_type": source_type,
                "title": book_title,
                "author": h.get("author") or "",
                "url": h.get("source_url") or h.get("url") or "",
                "readwise_url": h.get("highlight_url") or h.get("readwise_url") or "",
                "captured_at": captured_at,
                "ingested_at": ingested_at,
                "status": default_status,
                "engagement_level": level,
                "is_favorite": bool(h.get("is_favorite")),
                "tags": tags,
            }
            if h.get("note"):
                meta["note"] = h["note"]

            body = (h.get("text") or "").strip()
            note = (h.get("note") or "").strip()

            doc_parts = [_frontmatter(meta), "", body]
            if note:
                doc_parts += ["", "---", "", f"**Note:** {note}"]
            md_path.write_text("\n".join(doc_parts).rstrip() + "\n", encoding="utf-8")

            _upsert(conn, {
                "id": meta["id"],
                "source": meta["source"],
                "source_type": meta["source_type"],
                "title": meta["title"],
                "author": meta["author"],
                "url": meta["url"],
                "readwise_url": meta["readwise_url"],
                "captured_at": meta["captured_at"],
                "ingested_at": meta["ingested_at"],
                "status": meta["status"],
                "tags": tags,
                "note": note,
                "body": body,
                "path": md_path,
            })
            engagement.upsert_engagement(conn, meta["id"], level, ingested_at)
            written += 1
        conn.commit()
    finally:
        conn.close()

    return written, skipped


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("export", nargs="+", help="One or more raw Readwise export JSON files.")
    p.add_argument(
        "--kb-root",
        default=str(DEFAULT_KB_ROOT),
        help=f"Knowledge base root directory. Default: {DEFAULT_KB_ROOT}",
    )
    p.add_argument(
        "--status",
        default="provisional",
        choices=["provisional", "confirmed"],
        help="Default status for newly ingested entries.",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()
    kb_root = Path(args.kb_root).expanduser()
    total_written = 0
    total_skipped = 0
    for export in args.export:
        export_path = Path(export).expanduser()
        if not export_path.is_file():
            print(f"skip: {export_path} is not a file", file=sys.stderr)
            continue
        written, skipped = ingest_export(export_path, kb_root=kb_root, default_status=args.status)
        print(f"{export_path}: ingested={written} skipped={skipped}")
        total_written += written
        total_skipped += skipped
    print(f"Total: ingested={total_written} skipped={total_skipped} kb_root={kb_root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
