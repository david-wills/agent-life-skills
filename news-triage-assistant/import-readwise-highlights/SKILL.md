---
name: import-readwise-highlights
description: Import the last day of Readwise highlights into local files for later processing. Use when the user asks to sync, back up, or import recent Readwise highlights, especially a rolling 24-hour window or another `updatedAfter` delta.
---

# Import Readwise Highlights

## Overview

Fetch highlights from the Readwise Export API, defaulting to the last 24 hours, and write both a raw JSON export and a markdown digest to disk.

## Use the importer

- Use `scripts/import_readwise_highlights.py`.
- Read the Readwise token from `READWISE_TOKEN` or `READWISE_API_TOKEN`.
- Default window, last 24 hours.
- Default output, `imports/readwise/` under the current working directory.

## What it produces

- `*.json`, raw sync payload with books and flattened highlights.
- `*.md`, human-readable digest grouped by book.

## Ingest

`scripts/ingest_readwise.py` reads the import JSON and writes normalized per-highlight
markdown under `knowledge/readwise/<book_slug>/<highlight_id>.md`, then indexes each into
the FTS5 table in `knowledge/index.db` under `source: "readwise"`. Idempotent — re-ingesting
a highlight updates the existing row and file in place.

```bash
cd ~/.openclaw/workspace
LATEST=$(ls -1t imports/readwise/readwise-highlights-*.json | head -1)
python3 skills/import-readwise-highlights/scripts/ingest_readwise.py "$LATEST"
```

Full backfill: run the importer with `--since 2000-01-01` first, then ingest that file.
Promoting an entry to `status: confirmed` means editing the frontmatter and re-running
the ingester. The frontmatter contract lives in `knowledge-base/SKILL.md`.

Run by the `readwise-daily-sync` cron (5:07 AM PT), which ingests Reader in the same job.

## Notes

- Use `updatedAfter` for incremental syncs.
- Follow pagination via `pageCursor` until no cursor remains.
- Keep the raw JSON, even if you plan to process the highlights later.
