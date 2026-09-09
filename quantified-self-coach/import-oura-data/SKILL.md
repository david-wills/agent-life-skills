---
name: import-oura-data
description: Import workout, readiness, and sleep data from the Oura Ring v2 API into local JSON for ingestion into the knowledge base. Use when the user asks to sync, back up, or import Oura history — full historical pull or incremental delta. Powers recovery-aware suggestions in the workout coach.
---

# Import Oura Data

## Overview

Pulls four collections from the Oura API v2 and writes them to a single raw JSON export for the ingest pipeline:

- `workout` — cardio / runs / walks / cycling / strength sessions logged by the ring (cardio gap that Hevy doesn't cover).
- `daily_readiness` — daily readiness score (the recovery signal the workout coach gates on).
- `daily_sleep` — daily sleep score.
- `sleep` — detailed sleep sessions (HRV, RHR, total sleep, REM, deep).

`daily_activity` (steps/calories) is intentionally skipped — low signal for the workout coach, Hevy already covers training load.

## Authentication

Oura killed Personal Access Tokens; v2 is OAuth2-only. We use the **server-side flow** to get a refresh token (avoiding the client-side flow's 30-day re-auth treadmill).

**One-time setup:**

1. Register an OAuth2 application at <https://cloud.ouraring.com/oauth/applications>. Set the redirect URI to **exactly** `http://localhost:8080/callback`.
2. Save `OURA_CLIENT_ID` and `OURA_CLIENT_SECRET` to the OpenClaw 1Password vault.
3. Run `scripts/authorize_oura.py`. It opens a browser, captures the redirect, exchanges the code, and writes both `access_token` and `refresh_token` to `~/.openclaw/oura_tokens.json` (mode 0600).

That file is the long-lived credential. The importer auto-refreshes the access token using the refresh token and persists rotated tokens back. If `~/.openclaw/oura_tokens.json` is lost or the refresh token is invalidated, re-run `authorize_oura.py`.

Scopes requested: `personal daily heartrate workout`.

## Use the importer

```
python3 scripts/import_oura_data.py                      # last 90 days (initial backfill)
python3 scripts/import_oura_data.py --since 2026-04-20   # delta from a date
python3 scripts/import_oura_data.py --days 14            # last N days
```

- Reads tokens from `~/.openclaw/oura_tokens.json`. Refreshes the access token automatically when expired, persists rotated refresh tokens back to the same file.
- Default output dir: `imports/oura/` under the current working directory.
- Oura API v2 collections accept `start_date` / `end_date` (YYYY-MM-DD); the script pulls all four collections for the same window.

## What it produces

- `imports/oura/oura-data-<timestamp>.json` — single file with all four collections plus sync metadata. Schema:

  ```json
  {
    "synced_at": "2026-04-27T...Z",
    "start_date": "2026-04-13",
    "end_date": "2026-04-27",
    "workout": {"data": [...], "count": N},
    "daily_readiness": {"data": [...], "count": N},
    "daily_sleep": {"data": [...], "count": N},
    "sleep": {"data": [...], "count": N}
  }
  ```

## Ingest

`scripts/ingest_oura.py` (in this skill) reads the import JSON and writes:

- `knowledge/oura/workout-<id>.md` — per-event, mirroring the Hevy convention (`knowledge/hevy/<id>.md`).
- `knowledge/oura/daily-<YYYY-MM-DD>.md` — per-day, combining readiness + daily sleep score + main sleep session (HRV, RHR, total sleep, REM, deep) into a single searchable record.

Both file types are upserted into the FTS5 index (`knowledge/index.db`) under `source: "oura"`.

## Notes

- Oura uses ISO 8601 timestamps. Workouts have `start_datetime` / `end_datetime` (with TZ); daily metrics have a `day` field (YYYY-MM-DD, ring's local TZ).
- Refresh tokens rotate on every refresh. Re-authorize if the local token file is wiped.
- `--since` is inclusive on `start_date`. `--days N` means "today minus N days through today".
