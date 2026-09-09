---
name: import-reader-archive
description: Import Readwise Reader documents from the Archive location (the read/triaged pile) into local files for ingestion into the knowledge base. Use when the user asks to sync Reader archive into the brain, backfill the archive, or pull a delta of newly-archived Reader documents.
---

# Import Readwise Reader Archive

## Overview

Fetch documents from the Readwise Reader API filtered to `location=archive` (the "I have read this" pile) and write a raw JSON dump to disk. The companion ingester `scripts/ingest_reader.py` consumes the JSON and turns each doc into a knowledge-base entry. Both live here as of 2026-09-08 — the fetch and the ingest of the same source belong together — but they stay two scripts on purpose: a failed ingest never costs you the API pull.

Archive-only is deliberate: only documents you have actually read belong in the archive, not items still queued in inbox/later/shortlist.

## Use the importer

- Use `scripts/import_reader_archive.py`.
- Reads the Readwise token from `READWISE_TOKEN` or `READWISE_API_TOKEN` (one token covers both Reader and the legacy Highlights export).
- Default: full archive backfill with HTML content (so the ingester can generate summaries for docs Reader didn't auto-summarize).
- Default output: `imports/reader/` under the current working directory.

## Flags

- `--updated-after <ISO>` — incremental sync; only docs updated since this timestamp.
- `--hours N` — convenience for `--updated-after now-Nh`.
- `--no-html` — omit `withHtmlContent` to keep payload small (use for daily deltas).
- `--output-dir <path>` — override default `imports/reader/`.
- `--token <token>` — explicit token override.

## What it produces

- `reader-archive-<ts>.json` — raw payload with metadata + (by default) HTML content per doc.

## API notes

- Endpoint: `GET https://readwise.io/api/v3/list/`
- Required filter: `location=archive`.
- Pagination: follow `nextPageCursor` until empty.
- HTML content: pass `withHtmlContent=true` to embed each doc's HTML in the response. Larger payload but avoids per-doc fetches downstream.
- Rate limit: ~20 requests/minute per token; the importer sleeps 250ms between pages.
