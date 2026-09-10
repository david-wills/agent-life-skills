"""The one FTS5 table every ingester writes: ``entries`` in ``<data_root>/knowledge/index.db``.

Five ingesters (Reader archive, Readwise highlights, Apple Health, Oura, Hevy)
share the table so a single full-text query spans every source. The DDL lives
here, and only here, so a column added for one source cannot drift from the
others: ``tests/test_fts_schema.py`` fails if a script defines its own copy.

Columns marked UNINDEXED are stored but not tokenised; ``title``, ``author``,
``tags``, ``note`` and ``body`` are what MATCH searches.
"""

from __future__ import annotations

import sqlite3

ENTRIES_DDL = """
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


def ensure_entries(conn: sqlite3.Connection) -> None:
    """Create the ``entries`` table if the database does not have it yet."""
    conn.executescript(ENTRIES_DDL)
