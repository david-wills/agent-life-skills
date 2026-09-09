---
name: structured-metrics
description: The structured layer over the quantified-self data — populates the metrics_daily, workouts, exercise_sets and sleep_sessions tables plus the metrics_daily_resolved view in knowledge/index.db from the canonical Apple Health, Oura and Hevy files. Use when adding a metric or source, changing cross-source resolution priority, backfilling the tables, or debugging why the workout coach sees stale or missing numbers.
---

# Structured metrics

## Why it exists

The knowledge base indexes prose. Three of its sources — Apple Health, Oura, Hevy —
are not prose: they are numbers that want to be compared across days and joined
across sources. Full-text search over a markdown rendering of a sleep score cannot
answer "was my average HRV lower in the weeks I trained fasted."

So the same canonical files feed a second, structured path. This skill is that path.

It has three producers (`import-apple-health`, `import-oura-data`,
`import-hevy-workouts`) and one consumer (`workout-coach`), so it belongs beside
none of them. It lived inside `knowledge-base` until 2026-09-08, which put a writer
serving three importers inside a skill that mostly does full-text search. Moving it
to `workout-coach` was considered and rejected for the mirror-image reason: that
would put the writer inside its own reader.

**This skill is the writer. `workout-coach/scripts/build_context.py` is the reader.**
That split is the whole reason the skill exists — `build_context.py` opens
`knowledge/index.db` and does `SELECT` only. If you are changing a table's shape,
you are changing this skill's output *and* the coach's input, and both need to move
together.

## Pieces

| Piece | Path |
| --- | --- |
| The ingester | `scripts/ingest_metrics.py` |
| Output | four tables + one view in `knowledge/index.db` |
| Sources | `knowledge/apple-health/.sidecar/*.json`, `knowledge/oura/daily-*.md`, `knowledge/hevy/*.md` + `imports/hevy/*.json` |
| Reader | `workout-coach/scripts/build_context.py` |
| Crons | none of its own — invoked as a second step by three importer crons (see **The ordering contract**) |

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

## The ordering contract

This is the constraint most likely to bite, and it is invisible from inside either
skill.

Three crons each run an importer's ingester and *then* this one, as sequential steps
in a single job:

| Cron | Step: FTS ingest | Step: structured ingest |
| --- | --- | --- |
| `hevy-daily-sync` (5:11 PT) | `import-hevy-workouts/scripts/ingest_hevy.py` | `ingest_metrics.py --source hevy --days 14` |
| `oura-daily-sync` (5:15 PT) | `import-oura-data/scripts/ingest_oura.py` | `ingest_metrics.py --source oura --days 7` |
| `apple-health-kb-ingest-daily` (5:35 PT) | `import-apple-health/scripts/ingest_apple_health.py` | `ingest_metrics.py --source apple-health --days 7` |

The second step is **gated on the first**: the cron prompt only proceeds if the
ingester's stdout contains `ingested=`. That gate is the failure detection for the
whole daily pipeline — a broken path surfaces at the next fire rather than days later
as quietly missing rows.

Since the 2026-09-08 dissolution those two steps live in **different skills**, so each
of these three crons is now a cross-skill orchestrator. That is deliberate, but it
means: **editing a path in this skill means editing a cron prompt**, and the cron
prompts are in the gateway's SQLite store, not on disk. `grep` will not find them.
Use `openclaw cron list --json`.

## Invariants

- **Source files are canonical; SQL is regenerable.** Never edit the tables to fix a
  number — fix the source file and re-ingest. Anything reconstructable only from the
  database is a bug.
- **Idempotent.** Re-running over the same window upserts. The overlap windows above
  (14 days for Hevy, 7 for the others) exist so a missed run self-heals.
- **Backfill is safe.** `--all` re-derives everything from source files.
- **Schema changes go in `SCHEMA_DDL`**, then `python3 ingest_metrics.py --all` to
  re-apply and re-backfill.
- **`Path.home() / ".openclaw" / "workspace"`** anchors the data root — never
  `__file__`. The script is reached through the workspace `skills/` symlink, so
  deriving the workspace from its own location lands in the repo instead
  (CONVENTIONS.md 4).

## Operating it

```bash
cd ~/.openclaw/workspace

# what the crons run
python3 skills/structured-metrics/scripts/ingest_metrics.py --source oura --days 7
python3 skills/structured-metrics/scripts/ingest_metrics.py --source hevy --days 14
python3 skills/structured-metrics/scripts/ingest_metrics.py --source apple-health --days 7

# every source, last N days
python3 skills/structured-metrics/scripts/ingest_metrics.py --days 7

# full rebuild from source files
python3 skills/structured-metrics/scripts/ingest_metrics.py --all
```

Also accepts `--since YYYY-MM-DD`, `--kb-root`, `--imports-root`.

Each run prints a per-source line (`oura: metrics=…`, `hevy: workouts=…`) — that
string is what the crons match on, so do not reformat it casually.

Verify by artifact rather than exit code:

```bash
sqlite3 ~/.openclaw/workspace/knowledge/index.db \
  "SELECT source, MAX(date) FROM metrics_daily GROUP BY source;"
```

If a source's max date is stale, the problem is almost always upstream in that
source's importer — the phone stopped syncing, a token expired — not here.

## Known debt

The FTS5 `CREATE VIRTUAL TABLE entries` DDL is duplicated byte-identically across six
ingest scripts, this one included. Extracting it to `lib/fts_schema.py` is a real
cleanup, deliberately deferred out of the dissolution commit so that diff stayed
reviewable. Note that `lib/kb_schema.py` is **already taken** by an unrelated
entity-file contract — so do not put FTS5 DDL there.
