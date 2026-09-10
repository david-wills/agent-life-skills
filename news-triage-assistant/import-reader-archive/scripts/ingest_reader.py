#!/usr/bin/env python3
"""Ingest a raw Readwise Reader archive JSON dump into the local full-text index.

Reads the JSON produced by `import_reader_archive.py` in this directory, writes
one markdown file per archived document under `<kb-root>/reader/`, and upserts
rows into the FTS5 index at `<kb-root>/index.db`.

Summary source per doc, recorded as `summary_source` in the frontmatter:
  - "reader"     Reader's built-in summary (preferred when available).
  - "<model>"    generated through the `claude` CLI from html_content when Reader
                 had none (default model: claude-haiku-4-5).
  - "existing"   reused from a prior ingest to avoid re-spending tokens.

Idempotent: re-ingesting the same doc updates the file and FTS row in place,
and reuses any previously-generated summary unless --regenerate is passed.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

# Shared helpers live in lib/ at the repo root; walk up so this works from any cwd.
_LIB = next((p / "lib" for p in Path(__file__).resolve().parents if (p / "lib" / "skill_config.py").is_file()), None)
if _LIB is None:
    raise SystemExit("cannot find the repo-root lib/ directory; run from a clone of the repo, not a copied file")
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))
from fts_schema import ENTRIES_DDL as FTS_SCHEMA  # noqa: E402
import engagement  # noqa: E402
from claude_cli import claude_generate  # noqa: E402
from html_text import html_to_text  # noqa: E402
from skill_config import data_root  # noqa: E402

DEFAULT_MODEL = "claude-haiku-4-5"
CLAUDE_TIMEOUT_SECONDS = 180

SUMMARY_PROMPT = (
    "Write a 2-3 sentence neutral abstract of the following article. "
    "Capture the central claim or finding plus what kind of evidence/argument supports it. "
    "Output ONLY the abstract, with no preamble, no quotes, no bullet points."
)

MAX_HTML_CHARS_FOR_LLM = 60_000  # ~15k tokens, plenty for a 2-3 sentence abstract.

# summary_source values that mean "we already have a usable summary on disk".
REUSABLE_SOURCES = {"reader", "existing"}



def default_kb_root() -> Path:
    return data_root() / "knowledge"


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


def _generate_summary(text: str, model: str) -> str:
    """Shell out to the `claude` CLI (via lib/claude_cli.py) for a short abstract."""
    if not text.strip():
        raise RuntimeError("empty content for summary generation")
    truncated = text[:MAX_HTML_CHARS_FOR_LLM]
    out = claude_generate(SUMMARY_PROMPT + "\n\n---\n" + truncated, model=model,
                          timeout=CLAUDE_TIMEOUT_SECONDS)
    if not out:
        raise RuntimeError("claude CLI returned empty output")
    return out


def ingest_dump(
    dump_path: Path,
    kb_root: Path,
    default_status: str = "provisional",
    regenerate: bool = False,
    model: str = DEFAULT_MODEL,
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
            # Anything that is not Reader's own or a reuse marker was generated.
            existing_was_generated = bool(existing_source) and existing_source not in REUSABLE_SOURCES

            summary: str = ""
            summary_source: str = ""

            reader_summary = (doc.get("summary") or "").strip()

            if not regenerate and existing_body and existing_source:
                # Refresh from Reader if Reader now has a summary and we previously
                # generated one; otherwise reuse what we have to avoid re-spending tokens.
                if existing_was_generated and reader_summary:
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
                    summary = _generate_summary(text_content, model)
                    summary_source = model
                except Exception as exc:  # noqa: BLE001 - one doc must not kill the batch
                    counts["generation_failures"] += 1
                    print(f"WARN: summary gen failed for {doc_id} ({title}): {exc}", file=sys.stderr)
                    continue

            if summary_source == "reader":
                counts["summary_reader"] += 1
            elif summary_source == "existing":
                counts["summary_existing"] += 1
            else:
                counts["summary_generated"] += 1

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
    p = argparse.ArgumentParser(description="Ingest Reader archive JSON dumps into the local full-text index.")
    p.add_argument("dump", nargs="+", help="One or more raw Reader archive JSON dumps.")
    p.add_argument("--kb-root", default=None,
                   help="Index root: holds index.db and reader/*.md (default: <data_root>/knowledge).")
    p.add_argument(
        "--status",
        default="provisional",
        choices=["provisional", "confirmed"],
        help="Default status for newly ingested entries.",
    )
    p.add_argument(
        "--regenerate",
        action="store_true",
        help="Force regeneration of any existing summaries (ignores cache).",
    )
    p.add_argument("--model", default=DEFAULT_MODEL,
                   help=f"Claude CLI model for docs Reader did not summarize (default {DEFAULT_MODEL}).")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    kb_root = Path(args.kb_root).expanduser() if args.kb_root else default_kb_root()
    grand_total: dict[str, int] = {}
    for dump in args.dump:
        path = Path(dump).expanduser()
        if not path.is_file():
            print(f"skip: {path} is not a file", file=sys.stderr)
            continue
        counts = ingest_dump(path, kb_root=kb_root, default_status=args.status,
                             regenerate=args.regenerate, model=args.model)
        print(f"{path}: " + " ".join(f"{k}={v}" for k, v in counts.items()))
        for k, v in counts.items():
            grand_total[k] = grand_total.get(k, 0) + v
    print("Total: " + " ".join(f"{k}={v}" for k, v in grand_total.items()) + f" kb_root={kb_root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
