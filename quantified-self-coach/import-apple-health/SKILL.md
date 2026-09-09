---
name: import-apple-health
description: Receive Apple Health data via Health Auto Export's REST integration, persist as per-day Markdown for the knowledge base, and detect sync gaps. Local HTTP server on the Mac mini's Tailscale interface; HAE iPhone app POSTs nightly. Backfill script reads the legacy Dropbox folder for historical data.
---

# Import Apple Health

## Overview

A small stdlib HTTP server that receives JSON POSTs from the Health Auto
Export (HAE) iPhone app over Tailscale, parses each payload into per-day
Markdown under `knowledge/apple-health/YYYY-MM-DD.md`, and archives raw JSON
under `imports/apple-health/raw/`.

A daily cron (`gap_check.py`) detects missing dates and posts to
`#errors-and-alerts` so you can manually trigger a re-sync from the iPhone
before HealthKit's recent-data buffer rotates.

## Components

- `scripts/server.py` — stdlib `http.server` on port 8787; bearer-auth via `HAE_REST_TOKEN`.
- `scripts/parse_hae_payload.py` — JSON → per-day Markdown. Handles both metric and workout payload shapes; merges them into one file per date.
- `scripts/backfill_from_dropbox.py` — one-time historical backfill from the legacy Dropbox folder.
- `scripts/gap_check.py` — daily missing-date detector; posts to `#errors-and-alerts` (cooldown 6h).
- `launchd/com.openclaw.apple-health-server.plist.template` — keeps the server running across reboots. Rendered by `scripts/render-launchd.py`; the tracked file holds `{{HOME}}`, not a home path.

## Setup (one-time)

1. **Token (already done)** — `HAE_REST_TOKEN` is in 1P (vault `OpenClaw`) + macOS keychain (`openclaw-HAE_REST_TOKEN`). Resolved via `read_secret("HAE_REST_TOKEN")`.
2. **Server launchd plist** —
   ```sh
   python3 scripts/render-launchd.py --install apple-health-server
   launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.openclaw.apple-health-server.plist
   ```
3. **Gap-check launchd plist (daily at 9am PT)** —
   ```sh
   python3 scripts/render-launchd.py --install apple-health-gap-check
   launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.openclaw.apple-health-gap-check.plist
   ```
4. **Backfill** —
   ```sh
   python3 skills/import-apple-health/scripts/backfill_from_dropbox.py
   ```
5. **HAE iPhone config** — in HAE → Automations → REST API:
   - URL: `http://`net.tailnet_host`:8787/ingest`
   - Method: POST
   - Header: `Authorization: Bearer <token>` (retrieve token from 1Password OpenClaw vault, item `HAE_REST_TOKEN`)
   - Schedule: Automatic (default daily) or whatever cadence you prefer.

## Endpoints

- `POST /ingest` — accepts HAE JSON; bearer-auth required.
- `GET  /healthz` — liveness; no auth.
- `GET  /status` — last 5 ingests; bearer-auth required.

## Surfacing priority tiers

Downstream consumers (workout-coach context, morning summary, KB FTS) should respect this priority order. For overlapping metrics, **Oura is the primary source**; Apple Health is kept for confirmation or fallback when Oura is missing.

**Tier 1 — primary signals**
- `vo2_max` — Apple-only fitness trend (slow-moving)
- `resting_heart_rate` — Oura primary, Apple secondary
- `heart_rate_variability` — Oura primary, Apple secondary
- `respiratory_rate` — Oura primary, Apple secondary
- `apple_exercise_time` — Apple-only daily-move gauge
- `active_energy` — exercise intensity proxy

**Tier 2 — useful daily**
- `step_count`
- `time_in_daylight` (Oura does not currently surface this)
- `weight_body_mass`, `body_fat_percentage` — surface when present, do not error when absent (input source has been silent since 2025-09-08)
- `mindful_minutes` — surface when present, do not error when absent

**Tier 3 — passive monitoring**
- `apple_stand_hour`, `apple_stand_time`

**Tier 4 — lowest priority**
- `environmental_audio_exposure`, `headphone_audio_exposure` — threshold-alert use only

**Skip (do not surface):** sleep (use Oura), blood oxygen (use Oura), walking heart rate average, flights climbed, walking gait set (asymmetry / double-support / speed / step length), stair speed, physical effort, basal energy burned, six-minute walking test.

The `CORE_METRICS` list in `scripts/parse_hae_payload.py` mirrors this order; everything skipped lands in the per-day file's "Other metrics" section but should not be surfaced to the user.

## Ingest

`scripts/ingest_apple_health.py` reads the per-day markdown at
`knowledge/apple-health/YYYY-MM-DD.md` written by the server above and upserts each into
`knowledge/index.db` at id `apple-health:<YYYY-MM-DD>`. Defaults to the last 7 days for the
daily cron; `--all` backfills, `--date YYYY-MM-DD` does one day. Idempotent.

```bash
cd ~/.openclaw/workspace
python3 skills/import-apple-health/scripts/ingest_apple_health.py
```

**Structured value lookups should read the sidecar JSON** at
`knowledge/apple-health/.sidecar/<date>.json`, not parse FTS bodies.

The `apple-health-kb-ingest-daily` cron (5:35 AM PT) runs this and then
`structured-metrics/scripts/ingest_metrics.py --source apple-health --days 7`, gated on this
step printing `ingested=`.

## Tweakable knobs

- Core-metric whitelist + display labels → `scripts/parse_hae_payload.py:CORE_METRICS` / `CORE_LABELS`.
- Server port + bind host → env `HAE_SERVER_PORT` / `HAE_SERVER_HOST` (defaults 8787 / 0.0.0.0).
- Gap-check window + cooldown → `scripts/gap_check.py:DEFAULT_WINDOW_DAYS` / `ALERT_COOLDOWN_HOURS`.
- Errors channel → `scripts/gap_check.py:ERRORS_CHANNEL`.

## Security model

- The server binds 0.0.0.0 but the Mac mini's only inbound network surface for this port is Tailscale (no router forwarding, no Tailscale Funnel). Only authenticated devices on your tailnet can reach it.
- Bearer token is the application-layer guardrail for any compromised tailnet device.
- TLS is not used — traffic is already inside the Tailscale WireGuard tunnel.
