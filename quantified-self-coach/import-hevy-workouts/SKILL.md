---
name: import-hevy-workouts
description: Import Hevy workout history to JSON, render markdown and index it. Use to sync, back up or import workouts, full history or a delta.
compatibility: Requires a Hevy API key.
---

# Import Hevy Workouts

## Overview

Fetch workouts from the Hevy API (`https://api.hevyapp.com/v1/workouts`), paginate to completion, and write a single raw JSON export to disk for the ingest step.

`<data_root>` below is `paths.data_root` in `config.json`, default `<repo>/_data`.

## Use the importer

```bash
python3 scripts/import_hevy_workouts.py                                   # full history
python3 scripts/import_hevy_workouts.py --since 2024-04-01T00:00:00Z      # delta
python3 scripts/import_hevy_workouts.py --watermark-file <data_root>/imports/hevy/.watermark.json
```

- API key: `--token`, else `HEVY_API_KEY` in the environment, else `read_secret("HEVY_API_KEY")` (1Password → keychain → `secrets.json`, see `lib/read_secret.py`). A key that cannot be resolved aborts with the resolver's actual error, not a generic "missing".
- Default mode pulls **all** workouts.
- `--since <ISO 8601>` — Hevy returns workouts newest-first by start time; the pull keeps every workout whose `start_time` is at or after the cutoff and stops at the first one older. A workout *edited* after the cutoff but *started* before it is not re-pulled; do an occasional full pull if you edit old sessions.
- `--watermark-file PATH` — a JSON state file. If it exists, its `last_synced_at` minus one hour is combined (`min`) with `--since` to form the cutoff, and it is rewritten with the new `synced_at` on success. This is how a daily delta gets a self-adjusting window without you computing dates.
- `--output-dir DIR` — default `<data_root>/imports/hevy/`.
- Hevy's `/v1/workouts` endpoint caps `pageSize` at 10; the script paginates.

## What it produces

- `hevy-workouts-<timestamp>.json` — raw payload with all workouts as returned by the API plus sync metadata (`synced_at`, `since`, `workout_count`).

## Ingest

`scripts/ingest_hevy.py` reads one or more import JSON files and writes one markdown file per workout at
`<data_root>/knowledge/hevy/<workout_id>.md`, indexed into `<data_root>/knowledge/index.db` under `source: "hevy"`.
Weights are converted kg → lb here, not in the importer. Idempotent.

```bash
LATEST=$(ls -1t <data_root>/imports/hevy/hevy-workouts-*.json | head -1)
python3 quantified-self-coach/import-hevy-workouts/scripts/ingest_hevy.py "$LATEST"
```

Flags: `--kb-root DIR` (default `<data_root>/knowledge`), `--status provisional|confirmed`, `--dry-run` (lists the files it would write, touches nothing).

The daily job is three commands: the importer with a watermark, this ingester on the newest export, then
`structured-metrics/scripts/ingest_metrics.py --source hevy --days 14`, which populates the
`workouts` and `exercise_sets` tables the workout coach reads. The third step is gated on
this one printing `ingested=` — see `structured-metrics/SKILL.md` for that contract.

## Notes

- Hevy returns weights in `weight_kg` regardless of the user's display preference. Conversion to lb happens in the ingest step, not here.
- The markdown `Date:` and the structured `workouts.date` are both the **local** calendar day of `start_time`. A 6 PM session on the US west coast is 01:00 UTC the next day; the local day is the one you would say you trained on.
- A workout started on a watch but never filled in (a trainer session, say) arrives with no exercises. The ingest step renders it as an empty session rather than skipping it, so the day still counts as trained.
