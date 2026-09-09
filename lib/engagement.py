"""Engagement-level metadata for KB entries.

Four levels along a credence axis:
- read       — consumed but not actively marked (Reader archive, unstarred Snipd snips,
               aggregated source-level summaries like book-summaries/).
- highlighted — actively marked attention (Readwise highlights, starred Snipd snips,
               Readwise "is_favorite").
- noted      — highlighted with your own commentary attached.
- authored   — entirely your own thinking (Socratic answers, voice-note thoughts,
               Obsidian thoughts, your own book syntheses).

Inference is recomputed on every ingest so late-added stars / notes upgrade an entry on
the next sync (e.g. starring a Snipd snip after it has already been pulled).

Side-table only for stage 1 — no retrieval weighting yet. The level lives on every
markdown frontmatter and in `entry_engagement(id, engagement_level, updated_at)`.
"""

from __future__ import annotations

import sqlite3
from typing import Any

READ = "read"
HIGHLIGHTED = "highlighted"
NOTED = "noted"
AUTHORED = "authored"

LEVELS = (READ, HIGHLIGHTED, NOTED, AUTHORED)

# Stage-2 retrieval multipliers along the credence axis. Tunable here in one place;
# every surface (query, sunday digest, Socratic recall, bridge scan) reads from this.
WEIGHTS: dict[str, float] = {
    READ: 1.0,
    HIGHLIGHTED: 1.5,
    NOTED: 2.5,
    AUTHORED: 3.0,
}
# Hevy/Oura entries have no engagement_level (sensor data, off the credence axis).
# Treat them at baseline so they don't get penalised relative to `read` content.
DEFAULT_WEIGHT: float = 1.0


def weight_for(level: str | None) -> float:
    """Look up the retrieval multiplier for an engagement_level value."""
    if not level:
        return DEFAULT_WEIGHT
    return WEIGHTS.get(level, DEFAULT_WEIGHT)


def weight_case_sql(alias: str = "ee") -> str:
    """Render a SQL CASE expression that resolves to the engagement weight.

    Caller must `LEFT JOIN entry_engagement {alias} ON {alias}.id = <entries>.id`
    so the alias is in scope. Entries without a row in `entry_engagement`
    (Hevy/Oura) hit the ELSE branch and pick up `DEFAULT_WEIGHT`.
    """
    branches = " ".join(f"WHEN '{lvl}' THEN {w}" for lvl, w in WEIGHTS.items())
    return f"(CASE {alias}.engagement_level {branches} ELSE {DEFAULT_WEIGHT} END)"

# Snipd → Readwise integration writes ".starred" into the highlight `note` field as a
# sentinel — that is not commentary from the reader, so it must not promote to NOTED.
_SNIPD_STAR_NOTE_SENTINEL = ".starred"


def _tag_names(tags: Any) -> list[str]:
    """Mirror the de-shape used by ingesters: tags can be list[str], list[dict], or dict."""
    out: list[str] = []
    if isinstance(tags, dict):
        return [str(k) for k in tags.keys()]
    for t in tags or []:
        if isinstance(t, dict):
            name = t.get("name") or t.get("tag") or t.get("label")
        else:
            name = t
        if name:
            out.append(str(name))
    return out


def _is_snipd_url(url: str) -> bool:
    return "share.snipd.com" in (url or "")


def infer_for_reader(_doc: dict[str, Any]) -> str:
    """Reader archive items: by definition consumed but not highlighted at this layer.
    Highlights from Reader docs land in Readwise as separate entries."""
    return READ


def infer_for_readwise_highlight(highlight: dict[str, Any]) -> str:
    """Readwise highlights — including Snipd-routed snips.

    - Real note from the reader -> NOTED.
    - Snipd snip without `starred` tag (and no real note) -> READ.
    - Anything else (a highlight exists at all) -> HIGHLIGHTED.
    """
    raw_note = (highlight.get("note") or "").strip()
    has_real_note = bool(raw_note) and raw_note != _SNIPD_STAR_NOTE_SENTINEL

    if has_real_note:
        return NOTED

    tags = _tag_names(highlight.get("tags"))
    has_star_tag = "starred" in tags
    is_favorite = bool(highlight.get("is_favorite"))

    url = highlight.get("source_url") or highlight.get("url") or highlight.get("highlight_url") or ""
    if _is_snipd_url(url) and not (has_star_tag or is_favorite or raw_note == _SNIPD_STAR_NOTE_SENTINEL):
        return READ

    return HIGHLIGHTED


def infer_for_book_summary() -> str:
    """Generated book-level summary in knowledge/book-summaries/ — represents
    'I read this whole book' with no per-highlight attention markers."""
    return READ


def infer_for_authored() -> str:
    """Anything the user authored top-to-bottom: Socratic answers, voice-note
    thoughts, Obsidian thoughts, their own book syntheses, captured threads where
    they are the sole speaker, etc."""
    return AUTHORED


# -------- side-table persistence ----------------------------------------------

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


def upsert_engagement(
    conn: sqlite3.Connection,
    entry_id: str,
    level: str,
    updated_at: str,
) -> None:
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
