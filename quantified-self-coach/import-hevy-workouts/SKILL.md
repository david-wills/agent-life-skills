---
name: import-hevy-workouts
description: Import workouts from Hevy (the workout logging app) into local JSON for ingestion into the knowledge base. Use when the user asks to sync, back up, or import Hevy workout history — full historical pull or incremental delta.
---

# Import Hevy Workouts

## Overview

Fetch workouts from the Hevy API (`https://api.hevyapp.com/v1/workouts`), paginate to completion, and write a single raw JSON export to disk for the ingest pipeline.

## Use the importer

- Use `scripts/import_hevy_workouts.py`.
- Reads the API key from `HEVY_API_KEY` env var, or falls back to `read_secret("HEVY_API_KEY")` (1Password OpenClaw vault → keychain → secrets.json).
- Default mode pulls **all** workouts (full history). Pass `--since <ISO>` to bound the delta.
- Default output, `imports/hevy/` under the current working directory.
- Hevy's `/v1/workouts` endpoint caps `pageSize` at 10; the script handles pagination automatically.

## What it produces

- `hevy-workouts-<timestamp>.json` — raw payload with all workouts as returned by the API plus sync metadata.

## Ingest

`scripts/ingest_hevy.py` reads the import JSON and writes one markdown file per workout at
`knowledge/hevy/<workout_id>.md`, indexed into `knowledge/index.db` under `source: "hevy"`.
Weights are converted kg → lb here, not in the importer. Idempotent.

```bash
cd ~/.openclaw/workspace
LATEST=$(ls -1t imports/hevy/hevy-workouts-*.json | head -1)
python3 skills/import-hevy-workouts/scripts/ingest_hevy.py "$LATEST"
```

The `hevy-daily-sync` cron (5:11 AM PT) runs this and then
`structured-metrics/scripts/ingest_metrics.py --source hevy --days 14`, which populates the
`workouts` and `exercise_sets` tables the workout coach reads. The second step is gated on
this one printing `ingested=` — see `structured-metrics/SKILL.md` for that contract.

## Notes

- Hevy returns weights in `weight_kg` regardless of the user's display preference. Conversion to lb happens in the ingest step, not here.
- Workouts include `start_time` / `end_time` / `updated_at`; use `--since` against `updated_at` for incremental syncs.
- Trainer days (Fridays) often show up as nearly-empty workouts (the workout is started on Apple Watch but no exercises are logged). The ingest step handles this gracefully.
