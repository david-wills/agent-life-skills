---
name: import-apple-health
description: Receive Apple Health data via Health Auto Export's REST integration, persist it as per-day Markdown plus a JSON sidecar, index it, and detect sync gaps. A stdlib HTTP server the HAE iPhone app POSTs to nightly; a daily gap check that pages you while HealthKit can still re-send. A backfill script re-plays archived exports from any folder.
---

# Import Apple Health

## Overview

A small stdlib HTTP server receives JSON POSTs from the Health Auto Export
(HAE) iPhone app, merges each payload into per-day Markdown under
`<data_root>/knowledge/apple-health/YYYY-MM-DD.md` (with the unrendered points
in `.sidecar/YYYY-MM-DD.json`), and archives the raw JSON under
`<data_root>/imports/apple-health/raw/`.

A daily job (`gap_check.py`) detects missing dates and posts to the Discord
errors channel so you can trigger a re-sync from the phone before HealthKit's
recent-data buffer rotates. This is the leg of the pipeline that breaks, and it
breaks silently; the gap check is the whole reason it is tolerable.

`<data_root>` is `paths.data_root` in `config.json`, default `<repo>/_data`.

## Components

| Script | Does | Invocation |
| --- | --- | --- |
| `scripts/server.py` | HTTP ingest server; bearer auth via `HAE_REST_TOKEN` | `python3 scripts/server.py --bind 127.0.0.1 --port 8787` |
| `scripts/parse_hae_payload.py` | JSON → per-day Markdown + sidecar; also a one-file CLI | `python3 scripts/parse_hae_payload.py export.json` |
| `scripts/ingest_apple_health.py` | per-day Markdown → `index.db` (full-text) | `python3 scripts/ingest_apple_health.py` |
| `scripts/gap_check.py` | missing-date detector; posts to `discord.channels.errors` | `python3 scripts/gap_check.py` |
| `scripts/backfill_from_folder.py` | re-play archived HAE JSON exports from any folder | `python3 scripts/backfill_from_folder.py ~/hae-exports` |
| `scripts/render_launchd.py` | fill and install the two launchd templates | `python3 scripts/render_launchd.py --install --all` |
| `launchd/*.plist.template` | server (KeepAlive) and gap check (daily 09:00); placeholders `{{HOME}}` `{{REPO}}` `{{PYTHON}}` `{{BIND}}` | rendered by the script above |

All flags:

- `server.py`: `--bind` (default `127.0.0.1`), `--port` (default `8787`), `--data-root` (override `paths.data_root`).
- `parse_hae_payload.py INPUT`: `--out-dir`, `--raw-archive`, `--no-archive`.
- `ingest_apple_health.py`: `--kb-root`, `--since YYYY-MM-DD`, `--days N`, `--date YYYY-MM-DD` (repeatable), `--all`, `--dry-run`. With none of the window flags it does the last 7 days.
- `gap_check.py`: `--out-dir`, `--state-path`, `--window-days` (default 14), `--cooldown-hours` (default 6), `--dry-run`.
- `backfill_from_folder.py FOLDER`: `--out-dir`, `--raw-archive`, `--limit N`, `--progress-every N`, `--dry-run`.
- `render_launchd.py [NAME...]`: one of `--dry-run` / `--install`; `--all`, `--load`, `--python PATH`, `--bind IP`.

## Setup (one-time)

1. **Token.** Generate a long random string and store it as `HAE_REST_TOKEN`
   where `lib/read_secret.py` can find it (env var, 1Password, keychain, or
   `secrets.json`). The server reads it at start; the phone sends it as a header.
2. **Server.** Bind the interface the phone can reach. Loopback is only useful
   for testing; tailnet users bind their tailnet IP.
   ```sh
   python3 scripts/render_launchd.py --install apple-health-server --bind <tailnet IP> --load
   ```
3. **Gap check** (daily at 09:00 local):
   ```sh
   python3 scripts/render_launchd.py --install apple-health-gap-check --load
   ```
   Both templates log to `<repo>/_data/logs/`. Pass `--dry-run` instead of
   `--install` to see the rendered plist first.
4. **Backfill** any historical exports you have lying around:
   ```sh
   python3 scripts/backfill_from_folder.py ~/path/to/old-hae-exports
   ```
5. **HAE iPhone config** — in HAE → Automations → REST API:
   - URL: `http://<host the server binds>:8787/ingest`
   - Method: POST
   - Header: `Authorization: Bearer <token>`
   - Schedule: Automatic (default daily) or whatever cadence you prefer.

## Endpoints

- `POST /ingest` — accepts HAE JSON; bearer auth required. Returns `{ok, ts, dates, metric_points, workout_count}`. Non-object JSON is a 400; a merge failure is a bare `{"error": "ingest_failed"}` with the traceback in the server log, never in the response.
- `GET  /healthz` — liveness; no auth.
- `GET  /status` — last 5 ingests; bearer auth required.

## Merge semantics

The sidecar is the record; the markdown is a rendering of it. For a date that
already has data, any metric *name* in the new payload replaces the old points
for that name (latest sync wins); workouts are keyed by id (or start+name) and
replaced one at a time; device sources are unioned. Merges are serialised
inside the process, because HAE sends metrics and workouts as separate POSTs
that can land in the same second.

If a per-day `.md` exists with no sidecar, the merge recovers what it can from
the markdown (one point per metric line, the headline numbers per workout)
rather than dropping the day. That recovered state is written to the sidecar
on the same merge and the sidecar is authoritative from then on. Everything the
renderer summarised away is gone; that is the price of having deleted `.sidecar/`.

## Surfacing priority tiers

Downstream consumers (workout-coach context, a morning summary, full-text search)
should respect this order. For overlapping metrics, **Oura is the primary source**;
Apple Health is kept for confirmation or fallback when Oura is missing.

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
- `weight_body_mass`, `body_fat_percentage` — surface when present, do not error when absent (a scale that stops syncing is normal)
- `mindful_minutes` — surface when present, do not error when absent

**Tier 3 — passive monitoring**
- `apple_stand_hour`, `apple_stand_time`

**Tier 4 — lowest priority**
- `environmental_audio_exposure`, `headphone_audio_exposure` — threshold-alert use only

**Skip (do not surface):** sleep (use Oura), blood oxygen (use Oura), walking heart rate average, flights climbed, walking gait set (asymmetry / double-support / speed / step length), stair speed, physical effort, basal energy burned, six-minute walking test.

The `CORE_METRICS` list in `scripts/parse_hae_payload.py` mirrors this order; everything skipped lands in the per-day file's "Other metrics" section but should not be surfaced to the user.

## Ingest

`scripts/ingest_apple_health.py` reads the per-day markdown written by the
server and upserts each into `<data_root>/knowledge/index.db` at id
`apple-health:<YYYY-MM-DD>`. Defaults to the last 7 days for the daily job;
`--all` backfills, `--date YYYY-MM-DD` does one day. Entries are stamped noon
in the machine's local timezone. Idempotent.

```bash
python3 quantified-self-coach/import-apple-health/scripts/ingest_apple_health.py
```

**Structured value lookups should read the sidecar JSON** at
`knowledge/apple-health/.sidecar/<date>.json`, not parse full-text bodies.
`structured-metrics` does exactly that.

The daily job is two commands: this ingester, then
`structured-metrics/scripts/ingest_metrics.py --source apple-health --days 7`,
gated on this step printing `ingested=`. See `structured-metrics/SKILL.md`.

## Tweakable knobs

- Core-metric whitelist + display labels → `scripts/parse_hae_payload.py:CORE_METRICS` / `CORE_LABELS`.
- Bind host + port → `server.py --bind` / `--port` (and `{{BIND}}` via `render_launchd.py --bind`).
- Gap-check window + cooldown → `gap_check.py --window-days` / `--cooldown-hours` (defaults in `DEFAULT_WINDOW_DAYS` / `ALERT_COOLDOWN_HOURS`).
- Errors channel → `config.json` → `discord.channels.errors`.

## Security model

- The server binds loopback by default. Bind a tailnet interface to receive from the phone; never a public one. There is no TLS because the intended transport is already an encrypted tunnel.
- The bearer token is the application-layer guardrail for any compromised device on the same network. It is compared in constant time.
- Error responses carry no paths and no tracebacks; those go to the log.
