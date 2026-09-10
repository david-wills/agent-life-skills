---
name: structured-metrics
description: Build the metrics_daily, workouts, exercise_sets and sleep_sessions tables from Apple Health, Oura and Hevy. Use to add a metric, backfill or debug numbers.
---

# Structured metrics

## Why it exists

The local index is full-text over prose. Three of its sources — Apple Health,
Oura, Hevy — are not prose: they are numbers that want to be compared across days
and joined across sources. Full-text search over a markdown rendering of a sleep
score cannot answer "was my average HRV lower in the weeks I trained fasted."

So the same canonical files feed a second, structured path. This skill is that path.

It has three producers (`import-apple-health`, `import-oura-data`,
`import-hevy-workouts`) and one consumer (`workout-coach`), so it belongs beside
none of them. Filing it under the coach would put a writer inside its own reader;
filing it under one importer would hide the other two producers.

**This skill is the writer. `workout-coach/scripts/build_context.py` is the reader.**
That split is the whole reason the skill exists — `build_context.py` opens
`index.db` and does `SELECT` only. If you are changing a table's shape, you are
changing this skill's output *and* the coach's input, and both need to move together.

`<data_root>` below is `paths.data_root` in `config.json`, default `<repo>/_data`.

## Pieces

| Piece | Path |
| --- | --- |
| The ingester | `scripts/ingest_metrics.py` |
| Output | four tables + one view in `<data_root>/knowledge/index.db` |
| Sources | `knowledge/apple-health/.sidecar/*.json`, `knowledge/oura/daily-*.md`, `knowledge/hevy/*.md` + `imports/hevy/*.json` |
| Reader | `workout-coach/scripts/build_context.py` |
| Schedule | none of its own — it is step two of each importer's daily job (see **The ordering contract**) |

No secrets. No network. It reads files already on disk and writes SQLite.

## The schema

| Table | Grain |
| --- | --- |
| `metrics_daily` | one row per (date, metric, source) — daily numerics, source-tagged |
| `workouts` | one row per workout — Hevy and Apple Health both land here |
| `exercise_sets` | one row per set — Hevy only |
| `sleep_sessions` | one row per night — Oura only |
| `metrics_daily_resolved` (VIEW) | `metrics_daily` collapsed to one row per (date, metric) |

`metrics_daily` deliberately keeps every source's version of an overlapping metric
rather than picking a winner at write time. The view resolves the overlap at read
time with a fixed priority — **Oura > Apple Health > Hevy** — because the ring
measures directly what the phone infers.

**Consumers read the view, not the table.** Reading `metrics_daily` directly gives
duplicate rows per date on every metric more than one device records.

`workouts.date` is the **local** calendar day of `started_at`, matching the
`Date:` line in the Hevy markdown. Hevy stores UTC; an evening session west of
Greenwich is already tomorrow in UTC, and the day you would say you trained on is
the local one.

## The ordering contract

This is the constraint most likely to bite, and it is invisible from inside either
skill.

Each source's daily job is a short sequence in your scheduler — launchd, cron, an
agent's job runner, whatever you use — that runs the importer's ingester and
*then* this one:

| Source | Step 1: importer | Step 2: full-text ingest | Step 3: structured ingest |
| --- | --- | --- | --- |
| Hevy | `import-hevy-workouts/scripts/import_hevy_workouts.py --watermark-file …` | `import-hevy-workouts/scripts/ingest_hevy.py <newest export>` | `ingest_metrics.py --source hevy --days 14` |
| Oura | `import-oura-data/scripts/import_oura_data.py --days 7` | `import-oura-data/scripts/ingest_oura.py <newest export>` | `ingest_metrics.py --source oura --days 7` |
| Apple Health | (the server receives pushes; nothing to pull) | `import-apple-health/scripts/ingest_apple_health.py` | `ingest_metrics.py --source apple-health --days 7` |

Step 3 is **gated on step 2**: run it only if the ingester's stdout contains
`ingested=`. That gate is the failure detection for the whole daily pipeline — a
silent no-op that exits 0 is the failure that actually happens, and gating on the
printed count means a broken path surfaces at the next fire rather than days later
as quietly missing rows.

These jobs are not in this repo; they are three commands each, and the commands
above are the whole spec. The cost of the gate is that a log line is now an
interface: **editing an ingester's summary line, or a path in this skill, means
editing the job that calls it.**

## Invariants

- **Source files are canonical; SQL is regenerable.** Never edit the tables to fix a
  number — fix the source file and re-ingest. Anything reconstructable only from the
  database is a bug.
- **Idempotent.** Re-running over the same window upserts in place. The overlap
  windows above (14 days for Hevy, 7 for the others) exist so a missed run self-heals.
- **Sets are rewritten only when the raw JSON is present.** A Hevy workout's
  `exercise_sets` rows are cleared and re-derived only if that workout appears in a
  file under `imports/hevy/`. If the raw export has been pruned, the workout row is
  still refreshed from its markdown and the existing sets are left alone. (The
  workouts upsert updates in place rather than delete-and-insert for exactly this
  reason: a delete would cascade into the sets.)
- **Backfill is safe.** `--all` re-derives everything from source files.
- **Schema changes go in `SCHEMA_DDL`**, then `python3 ingest_metrics.py --all` to
  re-apply and re-backfill.
- **Every path derives from `data_root()`.** Override with `--kb-root` and
  `--imports-root` when you must; never edit a path constant.

## Operating it

```bash
# what the daily jobs run
python3 quantified-self-coach/structured-metrics/scripts/ingest_metrics.py --source oura --days 7
python3 quantified-self-coach/structured-metrics/scripts/ingest_metrics.py --source hevy --days 14
python3 quantified-self-coach/structured-metrics/scripts/ingest_metrics.py --source apple-health --days 7

# every source, last N days
python3 quantified-self-coach/structured-metrics/scripts/ingest_metrics.py --days 7

# full rebuild from source files
python3 quantified-self-coach/structured-metrics/scripts/ingest_metrics.py --all

# counts only, nothing written (runs against an in-memory copy of the schema)
python3 quantified-self-coach/structured-metrics/scripts/ingest_metrics.py --all --dry-run
```

Flags: `--source apple-health|oura|hevy|all` (default `all`), `--days N`,
`--since YYYY-MM-DD`, `--all`, `--dry-run`, `--kb-root DIR` (default
`<data_root>/knowledge`), `--imports-root DIR` (default `<data_root>/imports`).
With no window flag it does the last 7 days.

Each run prints one line per source (`oura: metrics=…`, `hevy: workouts=…`). If
your job checks that output, do not reformat it casually.

Verify by artifact rather than exit code:

```bash
sqlite3 <data_root>/knowledge/index.db \
  "SELECT source, MAX(date) FROM metrics_daily GROUP BY source;"
```

If a source's max date is stale, the problem is almost always upstream in that
source's importer — the phone stopped syncing, a token expired — not here.
