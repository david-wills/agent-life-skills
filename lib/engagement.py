"""Engagement level for imported reading material.

Four levels along one axis, from "consumed" to "your own words":

- read         consumed but not actively marked (a Reader archive item)
- highlighted  actively marked (a Readwise highlight, a starred podcast snip)
- noted        highlighted with your own commentary attached
- authored     entirely your own thinking

Inference is recomputed on every ingest, so a star or note added later upgrades
the entry on the next sync. The level lives in the ``entry_engagement`` side
table next to the full-text index; a retrieval layer can weight by it.
"""

from __future__ import annotations

import sqlite3
from typing import Any

READ = "read"
HIGHLIGHTED = "highlighted"
NOTED = "noted"
AUTHORED = "authored"

LEVELS = (READ, HIGHLIGHTED, NOTED, AUTHORED)

# Snipd's Readwise integration writes ".starred" into the highlight note as a
# sentinel. That is not commentary from the reader, so it must not promote to NOTED.
_SNIPD_STAR_NOTE_SENTINEL = ".starred"


def _tag_names(tags: Any) -> list[str]:
    """Tags arrive as list[str], list[dict], or dict depending on the API."""
    if isinstance(tags, dict):
        return [str(k) for k in tags.keys()]
    out: list[str] = []
    for t in tags or []:
        name = (t.get("name") or t.get("tag") or t.get("label")) if isinstance(t, dict) else t
        if name:
            out.append(str(name))
    return out


def _is_snipd_url(url: str) -> bool:
    return "share.snipd.com" in (url or "")


def infer_for_reader(_doc: dict[str, Any]) -> str:
    """Reader archive items are consumed but not highlighted at this layer.
    Highlights made in Reader land in Readwise as separate entries."""
    return READ


def infer_for_readwise_highlight(highlight: dict[str, Any]) -> str:
    """A real note -> NOTED. An unstarred podcast snip with no note -> READ.
    Anything else (a highlight exists at all) -> HIGHLIGHTED."""
    raw_note = (highlight.get("note") or "").strip()
    if raw_note and raw_note != _SNIPD_STAR_NOTE_SENTINEL:
        return NOTED

    tags = _tag_names(highlight.get("tags"))
    starred = "starred" in tags or bool(highlight.get("is_favorite")) or raw_note == _SNIPD_STAR_NOTE_SENTINEL
    url = highlight.get("source_url") or highlight.get("url") or highlight.get("highlight_url") or ""
    if _is_snipd_url(url) and not starred:
        return READ
    return HIGHLIGHTED


_ENGAGEMENT_SCHEMA = """
CREATE TABLE IF NOT EXISTS entry_engagement (
    id TEXT PRIMARY KEY,
    engagement_level TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_entry_engagement_level
    ON entry_engagement(engagement_level);
"""


def ensure_engagement_table(conn: sqlite3.Connection) -> None:
    conn.executescript(_ENGAGEMENT_SCHEMA)


def upsert_engagement(conn: sqlite3.Connection, entry_id: str, level: str, updated_at: str) -> None:
    if level not in LEVELS:
        raise ValueError(f"unknown engagement_level: {level!r}")
    conn.execute(
        """
        INSERT INTO entry_engagement (id, engagement_level, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            engagement_level = excluded.engagement_level,
            updated_at = excluded.updated_at
        """,
        (entry_id, level, updated_at),
    )
