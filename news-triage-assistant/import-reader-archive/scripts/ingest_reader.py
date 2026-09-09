#!/usr/bin/env python3
"""Ingest a raw Readwise Reader archive JSON dump into the knowledge base.

Reads the JSON produced by `skills/import-reader-archive/scripts/import_reader_archive.py`,
writes one markdown file per archived document under `knowledge/reader/`, and upserts
rows into the FTS5 index at `knowledge/index.db`.

Summary source per doc:
  - "reader"   — Reader's built-in summary (preferred when available).
  - "claude-haiku-4-5" — generated via the Claude CLI from html_content when Reader had none.
  - "existing" — reused from a prior generation to avoid re-spending tokens.

Idempotent: re-ingesting the same doc updates the file and FTS row in place,
and reuses any previously-generated summary unless --regenerate is passed.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sqlite3
import subprocess
import sys
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from pathlib import Path as _P  # noqa: E402
_LIB = next(p / "lib" for p in _P(__file__).resolve().parents if (p / "lib" / "read_secret.py").is_file())
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))
import engagement


DEFAULT_KB_ROOT = Path.home() / ".openclaw" / "workspace" / "knowledge"

CLAUDE_CLI = "claude"
CLAUDE_MODEL = "claude-haiku-4-5"
CLAUDE_TIMEOUT_SECONDS = 180

SUMMARY_PROMPT = (
    "Write a 2-3 sentence neutral abstract of the following article. "
    "Capture the central claim or finding plus what kind of evidence/argument supports it. "
    "Output ONLY the abstract — no preamble, no quotes, no bullet points."
)

MAX_HTML_CHARS_FOR_LLM = 60_000  # ~15k tokens, plenty for a 2-3 sentence abstract.


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


# html_to_text now lives in the repo-root lib/ so the reading-list skill can use it
# without importing across a skill boundary (CONVENTIONS.md 2).
# Shared helpers live in the repo-root lib/ (CONVENTIONS.md 2).
from html_text import html_to_text  # noqa: E402


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
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            lines.append(f"{key}: {value}")
        elif value is None or value == "":
            lines.append(f"{key}: \"\"")
        else:
            lines.append(f"{key}: {_yaml_scalar(value)}")
    lines.append("---")
    return "\n".join(lines)


def _parse_existing_md(path: Path) -> tuple[dict[str, str], str]:
    """Crude frontmatter parser: returns (meta_dict, body)."""
    if not path.is_file():
        return {}, ""
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end == -1:
        return {}, text
    fm_block = text[4:end]
    body = text[end + 5 :].strip()
    meta: dict[str, str] = {}
    for line in fm_block.splitlines():
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        val = val.strip()
        if val.startswith('"') and val.endswith('"'):
            val = val[1:-1].replace('\\"', '"').replace("\\\\", "\\").replace("\\n", "\n")
        meta[key.strip()] = val
    return meta, body


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


def _generate_summary(text: str) -> str:
    if not text.strip():
        raise RuntimeError("empty content for summary generation")
    truncated = text[:MAX_HTML_CHARS_FOR_LLM]
    full_prompt = SUMMARY_PROMPT + "\n\n---\n" + truncated
    proc = subprocess.run(
        [
            CLAUDE_CLI,
            "--print",
            "--permission-mode", "bypassPermissions",
            "--model", CLAUDE_MODEL,
        ],
        input=full_prompt,
        capture_output=True,
        text=True,
        timeout=CLAUDE_TIMEOUT_SECONDS,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"claude CLI failed (rc={proc.returncode}): {proc.stderr.strip()}")
    out = proc.stdout.strip()
    if not out:
        raise RuntimeError("claude CLI returned empty output")
    return out


def ingest_dump(
    dump_path: Path,
    kb_root: Path = DEFAULT_KB_ROOT,
    default_status: str = "provisional",
    regenerate: bool = False,
) -> dict[str, int]:
    payload = json.loads(dump_path.read_text(encoding="utf-8"))
    docs = payload.get("docs") or []
    ingested_at = _iso_utc_now()
    conn = _ensure_db(kb_root / "index.db")
    engagement.ensure_engagement_table(conn)

    counts = {
        "written": 0,
        "skipped_no_id": 0,
        "skipped_no_summary_source": 0,
        "summary_reader": 0,
        "summary_generated": 0,
        "summary_existing": 0,
        "generation_failures": 0,
    }

    try:
        for doc in docs:
            doc_id = doc.get("id")
            if not doc_id:
                counts["skipped_no_id"] += 1
                continue

            title = doc.get("title") or "Untitled"
            slug = f"{_slug(title)}-{doc_id}"
            md_path = kb_root / "reader" / f"{slug}.md"
            md_path.parent.mkdir(parents=True, exist_ok=True)

            existing_meta, existing_body = _parse_existing_md(md_path)
            existing_source = existing_meta.get("summary_source") or ""

            summary: str = ""
            summary_source: str = ""

            reader_summary = (doc.get("summary") or "").strip()

            if not regenerate and existing_body and existing_source in {"reader", "claude-haiku-4-5", "existing"}:
                # Refresh from Reader if Reader now has a summary and we previously generated one,
                # otherwise reuse what we have to avoid re-spending tokens.
                if existing_source == "claude-haiku-4-5" and reader_summary:
                    summary = reader_summary
                    summary_source = "reader"
                else:
                    summary = existing_body
                    summary_source = "existing"
            elif reader_summary:
                summary = reader_summary
                summary_source = "reader"
            else:
                html_content = doc.get("html_content") or ""
                text_content = html_to_text(html_content)
                if not text_content:
                    counts["skipped_no_summary_source"] += 1
                    continue
                try:
                    summary = _generate_summary(text_content)
                    summary_source = "claude-haiku-4-5"
                except Exception as exc:
                    counts["generation_failures"] += 1
                    print(f"WARN: summary gen failed for {doc_id} ({title}): {exc}", file=sys.stderr)
                    continue

            if summary_source == "reader":
                counts["summary_reader"] += 1
            elif summary_source == "claude-haiku-4-5":
                counts["summary_generated"] += 1
            else:
                counts["summary_existing"] += 1

            tags_field = doc.get("tags") or {}
            if isinstance(tags_field, dict):
                tags = list(tags_field.keys())
            elif isinstance(tags_field, list):
                tags = [t if isinstance(t, str) else (t.get("name") or "") for t in tags_field if t]
                tags = [t for t in tags if t]
            else:
                tags = []

            captured_at = (
                doc.get("created_at")
                or doc.get("published_date")
                or doc.get("updated_at")
                or ""
            )
            archived_at = doc.get("last_status_update") or doc.get("updated_at") or ""

            category = (doc.get("category") or "document").lower()

            level = engagement.infer_for_reader(doc)

            meta = {
                "id": f"reader:{doc_id}",
                "source": "reader",
                "source_type": category,
                "title": title,
                "author": doc.get("author") or "",
                "url": doc.get("source_url") or doc.get("url") or "",
                "readwise_url": doc.get("url") or "",
                "captured_at": captured_at,
                "archived_at": archived_at,
                "ingested_at": ingested_at,
                "status": default_status,
                "summary_source": summary_source,
                "engagement_level": level,
                "word_count": int(doc.get("word_count") or 0),
                "reading_progress": float(doc.get("reading_progress") or 0.0),
                "tags": tags,
            }

            doc_parts = [_frontmatter(meta), "", summary.strip()]
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
                "note": "",
                "body": summary,
                "path": md_path,
            })
            engagement.upsert_engagement(conn, meta["id"], level, ingested_at)
            counts["written"] += 1
        conn.commit()
    finally:
        conn.close()

    return counts


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("dump", nargs="+", help="One or more raw Reader archive JSON dumps.")
    p.add_argument("--kb-root", default=str(DEFAULT_KB_ROOT))
    p.add_argument(
        "--status",
        default="provisional",
        choices=["provisional", "confirmed"],
    )
    p.add_argument(
        "--regenerate",
        action="store_true",
        help="Force regeneration of any existing summaries (ignores cache).",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()
    kb_root = Path(args.kb_root).expanduser()
    grand_total: dict[str, int] = {}
    for dump in args.dump:
        path = Path(dump).expanduser()
        if not path.is_file():
            print(f"skip: {path} is not a file", file=sys.stderr)
            continue
        counts = ingest_dump(path, kb_root=kb_root, default_status=args.status, regenerate=args.regenerate)
        print(f"{path}: " + " ".join(f"{k}={v}" for k, v in counts.items()))
        for k, v in counts.items():
            grand_total[k] = grand_total.get(k, 0) + v
    print("Total: " + " ".join(f"{k}={v}" for k, v in grand_total.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
