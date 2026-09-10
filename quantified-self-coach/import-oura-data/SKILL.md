---
name: import-oura-data
description: Import Oura Ring workouts, readiness and sleep to JSON, render markdown and index it. Use to sync or back up Oura history for the workout coach.
compatibility: Requires Oura API client credentials.
---

# Import Oura Data

## Overview

Pulls four collections from the Oura API v2 and writes them to a single raw JSON export for the ingest step:

- `workout` — cardio / runs / walks / cycling / strength sessions logged by the ring (the cardio gap that Hevy doesn't cover).
- `daily_readiness` — daily readiness score (the recovery signal the workout coach gates on).
- `daily_sleep` — daily sleep score.
- `sleep` — detailed sleep sessions (HRV, RHR, total sleep, REM, deep).

`daily_activity` (steps/calories) is intentionally skipped — low signal for the workout coach, and Apple Health already covers steps.

`<data_root>` below is `paths.data_root` in `config.json`, default `<repo>/_data`.

## Scripts

| Script | Does | Invocation |
| --- | --- | --- |
| `scripts/authorize_oura.py` | one-time OAuth2 dance; writes the token file | `python3 scripts/authorize_oura.py [--output PATH]` |
| `scripts/import_oura_data.py` | pull the four collections into one JSON export | `python3 scripts/import_oura_data.py --days 14` |
| `scripts/ingest_oura.py` | export JSON → markdown + `index.db` | `python3 scripts/ingest_oura.py <data_root>/imports/oura/oura-data-*.json` |

## Authentication

Oura retired Personal Access Tokens; v2 is OAuth2-only. This uses the **server-side flow** to get a refresh token (avoiding the client-side flow's 30-day re-auth treadmill).

**One-time setup:**

1. Register an OAuth2 application at <https://cloud.ouraring.com/oauth/applications>. Set the redirect URI to **exactly** `http://localhost:8080/callback`.
2. Store `OURA_CLIENT_ID` and `OURA_CLIENT_SECRET` where `lib/read_secret.py` can find them (env var, 1Password, keychain, or `secrets.json`).
3. Run `scripts/authorize_oura.py`. It opens a browser, captures the redirect, exchanges the code, and writes both `access_token` and `refresh_token` to `<data_root>/oura/oura_tokens.json` (mode 0600). `--output PATH` puts it elsewhere.

That file is the long-lived credential. The importer auto-refreshes the access token using the refresh token and persists rotated tokens back. If the file is lost or the refresh token is invalidated, re-run `authorize_oura.py`.

Scopes requested: `personal daily heartrate workout`.

## Use the importer

```
python3 scripts/import_oura_data.py                      # last 90 days (initial backfill)
python3 scripts/import_oura_data.py --since 2024-04-20   # delta from a date
python3 scripts/import_oura_data.py --days 14            # last N days
```

Flags: `--since YYYY-MM-DD` (inclusive start), `--days N` (today − N through today; exclusive with `--since`), `--end YYYY-MM-DD` (inclusive, default today UTC), `--output-dir DIR` (default `<data_root>/imports/oura`), `--token-file PATH` (default `<data_root>/oura/oura_tokens.json`).

- Refreshes the access token automatically when expired, persists rotated refresh tokens back to the same file.
- Oura API v2 collections accept `start_date` / `end_date` (YYYY-MM-DD); the script pulls all four collections for the same window and retries transient HTTP errors (429/5xx/timeouts) three times.

## What it produces

- `<data_root>/imports/oura/oura-data-<timestamp>.json` — single file with all four collections plus sync metadata. Schema:

  ```json
  {
    "synced_at": "2024-04-27T...Z",
    "start_date": "2024-04-13",
    "end_date": "2024-04-27",
    "workout": {"data": [...], "count": N},
    "daily_readiness": {"data": [...], "count": N},
    "daily_sleep": {"data": [...], "count": N},
    "sleep": {"data": [...], "count": N}
  }
  ```

## Ingest

`scripts/ingest_oura.py` reads one or more export files and writes:

- `<data_root>/knowledge/oura/workout-<id>.md` — per-event, mirroring the Hevy convention (`knowledge/hevy/<id>.md`).
- `<data_root>/knowledge/oura/daily-<YYYY-MM-DD>.md` — per-day, combining readiness + daily sleep score + main sleep session (HRV, RHR, total sleep, REM, deep) into a single searchable record. The frontmatter carries the numbers (`readiness_score`, `sleep_score`, `hrv_avg`, `resting_hr`, `total_sleep_min`, `deep_sleep_min`, `rem_sleep_min`); `structured-metrics` reads them from there.

Both file types are upserted into the FTS5 index (`<data_root>/knowledge/index.db`) under `source: "oura"`. Idempotent.

Flags: `--kb-root DIR` (default `<data_root>/knowledge`), `--status provisional|confirmed`, `--dry-run` (lists the files it would write, touches nothing).

The daily job is three commands: `import_oura_data.py --days 7`, this ingester on the newest export, then
`structured-metrics/scripts/ingest_metrics.py --source oura --days 7`, gated on this step printing `ingested=`. See `structured-metrics/SKILL.md`.

## Notes

- Oura uses ISO 8601 timestamps. Workouts have `start_datetime` / `end_datetime` (with TZ); daily metrics have a `day` field (YYYY-MM-DD, ring's local TZ).
- Refresh tokens rotate on every refresh. Re-authorize if the local token file is wiped.
- `--since` is inclusive on `start_date`. `--days N` means "today minus N days through today".
