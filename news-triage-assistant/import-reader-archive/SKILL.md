---
name: import-reader-archive
description: Dump Readwise Reader's archive to JSON and ingest it into the local full-text index. Use to back up, backfill or pull a delta of read documents.
compatibility: Requires a Readwise Reader account. The ingester uses the claude CLI to summarise documents Reader did not.
metadata: {"openclaw": {"requires": {"bins": ["claude"]}}}
---

# Import Readwise Reader Archive

## Overview

Fetch documents from the Readwise Reader API filtered to `location=archive` (the "I have read this" pile) and write a raw JSON dump to disk. The companion ingester `scripts/ingest_reader.py` consumes the JSON and turns each doc into a markdown file plus a row in the SQLite FTS5 index. The fetch and the ingest of the same source belong together, but they stay two scripts on purpose: a failed ingest never costs you the API pull.

Archive-only is deliberate: only documents you have actually read belong in the archive, not items still queued in inbox/later/shortlist.

## Use the importer

```bash
cd news-triage-assistant/import-reader-archive
python3 scripts/import_reader_archive.py                 # full backfill, with HTML
python3 scripts/import_reader_archive.py --hours 30      # daily delta
```

- Reads the Readwise token from `READWISE_TOKEN` or `READWISE_API_TOKEN` (one token covers both Reader and the Highlights export).
- Default: full archive backfill with HTML content, so the ingester can generate summaries for docs Reader didn't auto-summarize.
- Default output: `<data_root>/imports/reader/` (`paths.data_root` in `config.json`, else `_data/` at the repo root).

Flags:

- `--updated-after <ISO>` — incremental sync; only docs updated since this timestamp.
- `--hours N` — convenience for `--updated-after now-Nh`.
- `--no-html` — omit `withHtmlContent` to keep payload small. Fine for daily deltas of docs Reader has already summarized; docs with neither a summary nor html are skipped at ingest.
- `--output-dir <path>` — override the default output directory.
- `--token <token>` — explicit token override.

It produces `reader-archive-<ts>.json`: raw payload with metadata + (by default) HTML content per doc.

## Use the ingester

```bash
LATEST=$(ls -1t _data/imports/reader/reader-archive-*.json | head -1)   # or wherever --output-dir put it
python3 scripts/ingest_reader.py "$LATEST"
```

For every doc in the dump it writes `<kb-root>/reader/<title-slug>-<id>.md` (frontmatter + a short summary) and upserts a row into the `entries` FTS5 table of `<kb-root>/index.db`, plus an `entry_engagement` row at level `read` (see `lib/engagement.py`).

Summary source per doc, recorded as `summary_source` in the frontmatter and counted in the run output:

- `reader` — Reader's own summary, preferred when present.
- `<model>` — generated from `html_content` through the `claude` CLI (`lib/claude_cli.py`) when Reader had none. **This is the one network/LLM dependency of the ingester**: `claude` must be on `PATH`, and each such doc costs one call at ~15k tokens of input. A failed generation is counted in `generation_failures` and the doc is skipped, not half-written.
- `existing` — reused from a previous ingest, so re-runs do not re-spend tokens. If Reader has since produced its own summary for a doc we generated, Reader's wins.

Docs with no summary and no html land in `skipped_no_summary_source`.

Flags:

- `--kb-root <path>` — index root holding `index.db` and `reader/*.md`. Default `<data_root>/knowledge`, the same file the `reading-list` skill and `import-readwise-highlights` write to.
- `--status provisional|confirmed` — the `status` frontmatter value for this run (default `provisional`). Nothing in this package reads it yet; it is there for a query layer to weight.
- `--regenerate` — ignore existing summaries and regenerate every doc that Reader did not summarize.
- `--model <name>` — Claude CLI model for generated summaries (default `claude-haiku-4-5`).

Idempotent: re-ingesting the same doc updates the file and FTS row in place.

## Schedule

On demand, or a daily delta if you want the index to track your reading: `--hours 30` gives a 24-hour cron six hours of slack, then ingest the file it wrote. Any cron or agent scheduler will do.

## Notes

- Endpoint: `GET https://readwise.io/api/v3/list/`, filter `location=archive`, pagination via `nextPageCursor` until empty.
- Rate limit: ~20 requests/minute per token; the importer sleeps 250ms between pages.
- The `reading-list` skill writes its summaries to the Reader document `notes` field, never `summary`, so `summary_source: reader` here always means Reader's own text.
- The small helpers (`_slug`, ISO parsing, the FTS schema, frontmatter rendering) are duplicated between this skill and `import-readwise-highlights` on purpose, so each skill stands alone.
- There is no query tool over the index in this package; `sqlite3 <kb-root>/index.db` is the interface.
