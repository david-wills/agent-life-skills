---
name: import-readwise-highlights
description: Import recent Readwise highlights to JSON and markdown and index them locally. Use to sync, back up or import highlights, by default the last 24 hours.
compatibility: Requires a Readwise account.
---

# Import Readwise Highlights

## Overview

Fetch highlights from the Readwise Export API, defaulting to the last 24 hours, and write both a raw JSON export and a markdown digest to disk. The companion ingester `scripts/ingest_readwise.py` turns the JSON into one markdown file per highlight plus a row in the SQLite FTS5 index. Two scripts on purpose: a failed ingest never costs you the API pull.

## Use the importer

```bash
cd news-triage-assistant/import-readwise-highlights
python3 scripts/import_readwise_highlights.py                      # last 24 hours
python3 scripts/import_readwise_highlights.py --since 2000-01-01   # full backfill
```

- Reads the Readwise token from `READWISE_TOKEN` or `READWISE_API_TOKEN`.
- Default window: last 24 hours.
- Default output: `<data_root>/imports/readwise/` (`paths.data_root` in `config.json`, else `_data/` at the repo root).

Flags:

- `--hours N` — rolling window in hours (default 24).
- `--since <ISO>` — explicit `updatedAfter` timestamp. Overrides `--hours`.
- `--output-dir <path>` — override the default output directory.
- `--token <token>` — explicit token override.

It produces two files per run:

- `readwise-highlights-<ts>.json` — raw sync payload: the books as returned, plus a flattened `highlights` list with the book metadata copied onto each highlight.
- `readwise-highlights-<ts>.md` — human-readable digest grouped by book.

A highlight's `text` can be null (image-only highlights, some podcast snips); both the digest and the ingester treat that as empty rather than crash.

## Use the ingester

```bash
LATEST=$(ls -1t _data/imports/readwise/readwise-highlights-*.json | head -1)   # or wherever --output-dir put it
python3 scripts/ingest_readwise.py "$LATEST"
```

For every highlight it writes `<kb-root>/readwise/<book-slug>-<book-id>/<highlight-id>.md` (frontmatter, the highlight text, and the note if any) and upserts a row into the `entries` FTS5 table of `<kb-root>/index.db` under `source: "readwise"`. Deleted and discarded highlights are skipped and counted.

Each entry also gets an `entry_engagement` row, inferred by `lib/engagement.py`: a highlight with a real note is `noted`; an unstarred podcast snip with no note is `read`; anything else is `highlighted`. Inference is recomputed on every ingest, so a note or star added later upgrades the entry on the next run.

Flags:

- `--kb-root <path>` — index root holding `index.db` and `readwise/**/*.md`. Default `<data_root>/knowledge`, the same file `import-reader-archive` and `reading-list` write to.
- `--status provisional|confirmed` — the `status` frontmatter value for this run (default `provisional`). Nothing in this package reads it yet; it is there for a query layer to weight.

Idempotent: re-ingesting the same highlight updates the file and index row in place. Full backfill: run the importer with `--since 2000-01-01`, then ingest that file.

## Schedule

On demand, or daily if you want the index to track your highlighting. The author runs it once a morning alongside the Reader archive delta; any cron or agent scheduler will do.

## Notes

- Endpoint: `GET https://readwise.io/api/v2/export/`; use `updatedAfter` for incremental syncs and follow `nextPageCursor` until none remains.
- Keep the raw JSON even if you plan to process the highlights later; the ingester also accepts the older books-only shape and flattens it on the fly.
- The small helpers (`_slug`, ISO parsing, the FTS schema, frontmatter rendering) are duplicated between this skill and `import-reader-archive` on purpose, so each skill stands alone.
- There is no query tool over the index in this package; `sqlite3 <kb-root>/index.db` is the interface.
