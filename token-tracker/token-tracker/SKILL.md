---
name: token-tracker
description: Track Claude token usage, API-equivalent cost, and subscription quota burn per day, per workflow (cron job / Discord channel / terminal session), and per model. Answers "what used up my weekly quota", "what does the newsfeed cron cost", and "where should I switch models". Use when the user asks about token usage, model costs, quota, rate limits, or which workflows are expensive.
---

# Token Tracker

Answers, for any day or quota window: **how many tokens went where, what that
would have cost on the API, and what percentage of the weekly Claude subscription
quota it burned** — broken out by workflow and by model.

Output goes to Discord **#token-tracker** (`discord.channels.token_tracker`
in the repo-root `config.json`).

## Where the numbers come from

| What | Source | Why this one |
|---|---|---|
| Token counts (input / output / cache read / cache write 5m+1h / thinking) | `~/.claude/projects/**/*.jsonl` | Every assistant message carries a full `usage` block. OpenClaw's `claude-cli` provider writes here too, so agent runs and the user's own terminal sessions are both captured. |
| Workflow attribution | `~/.openclaw/state/openclaw.sqlite` → `cron_run_logs` + `cron_jobs`; transcript `chat_id`; `agents/*/sessions/sessions.json` | Names each session: cron job, Discord channel, or terminal. |
| Real quota utilization | `GET https://api.anthropic.com/api/oauth/usage` | The endpoint `/usage` uses. The **only** source of true quota burn — Anthropic does not publish the weekly limit as a token or dollar figure. |
| Prices | `scripts/lib.py` `PRICES` | Anthropic list prices. Cache write 5m = 1.25×, 1h = 2×, cache read = 0.1× input. |

`OpenClaw sessions ≠ Claude sessions` — the two systems mint different UUIDs and
there is no shared id. Attribution runs in four passes, best evidence first:

1. **cron** — a cron run window from `cron_run_logs` that *contains* the whole
   claude session (start inside the window, finish before the run ended) with a
   matching model. Containment, not overlap, is what stops a long-lived main
   session from being mislabelled as whatever cron fired during it.
2. **channel** — `claudeCliSessionId` in `sessions.json` (only current bindings).
3. **channel (durable)** — the `chat_id` in the transcript's own opening context
   block. Survives rebinding, which is why historical sessions still resolve.
4. **terminal** — `entrypoint: "cli"` means the user drove Claude Code directly
   (`sdk-cli` means OpenClaw drove it).

Anything left over is reported as `unattributed` with its dollar share, rather
than silently folded into another row.

## Quota percentage

Anthropic reports utilization as a percentage, not tokens, so the tracker
**derives the conversion each run**:

```
$ per 1% of weekly quota  =  (API-equivalent spend over the current 7-day window)
                             ÷ (weekly utilization % the account reports)
```

A workflow's quota share is then its own spend divided by that number. This
self-corrects — if Anthropic changes how usage is weighted, the next run
re-derives it.

**The conversion is not stable, and the report says so.** Anthropic's quota is
not denominated in dollars — it is weighted by something closer to compute — so a
dollar-per-percent rate drifts with model mix. `segment_rates()` derives the rate
independently for each monotonic run of utilization in the window and the report
warns when they disagree by more than 1.5x. On 2026-09-04 two segments of the
same week implied **$29.66** and **$69.81** per point, a 2.4x spread, while an
Opus-heavy stretch and a Haiku-heavy one were being averaged into one figure.

So: treat `≈X% wk` as an order of magnitude, not a measurement. The dollar
figures are exact; the quota conversion is an estimate with a wide band. Early in
a fresh window the percentage is also small and integer-rounded, which widens it
further.

**Why API-equivalent dwarfs the subscription price.** A $455 day against a
$200/month plan is not a pricing error — 94% of that spend is cache reads and
writes, i.e. re-reading the same context on every turn of an agentic loop. The
API bills per read; a subscription bills capacity and enforces rate limits
instead. "API-equivalent" is a counterfactual — what this traffic *would* have
cost at list prices — not a discount being realised, and not a number anyone
would actually pay, because on metered billing the work would be restructured.

## Off-machine usage (the important caveat)

**Utilization is account-wide; transcripts are only from this Mac.** Claude used
on the Mac Studio (or anywhere else) lands in the denominator but not the
numerator, so `$ per 1%` reads **low** and every workflow's quota share reads
**high**. Dollar costs stay exact either way — only the quota percentages skew.

`offmachine.py` sizes the gap without needing access to the other machine:

- **Scoped buckets.** The `limits` array in the usage response carries per-model
  weekly buckets. A model with quota burn and zero local messages can only have
  run elsewhere. As of 2026-09-04, **Fable sits at 8% of its weekly bucket with
  no local Fable usage at all** — proof of a second machine, and confirmation
  that the top-level `seven_day_opus` field being null does *not* mean per-model
  limits are absent.
- **Drift regression.** Across consecutive samples inside one 5-hour window, fit
  `d_utilization = k·d_local_spend + r·d_hours`. `k` gives the local dollar→quota
  conversion; `r` is burn local spend can't explain, i.e. the other machines.
  Needs ~8 intervals (4h of sampling) before it reports.

While any unseen model is burning quota, the report labels its quota percentages
as upper bounds rather than presenting them as settled.

### Closing the gap: three options, least access first

Adding another machine is a **security decision, not just a config one**. Ranked
by how much access each costs:

**0. Automatic drop folder (how the Studio is actually wired).** The
`OpenClaw_Shared` SMB share is hosted *on the Mac Studio* and auto-mounts on the
mini at `/Volumes/OpenClaw_Shared`. So the Studio writes its export to a folder
on its own disk and the mini reads it over a mount that already exists — no SSH,
no key, no shell access, in either direction.

- Drop folder: `token_tracker.inbox` (`/Volumes/OpenClaw_Shared/token-tracker`)
- Studio side: run `install-on-studio.sh studio` once, from that folder. It
  schedules a daily 05:00 export to `studio-tokens.json` beside it.
- Mini side: the half-hourly sampler imports anything whose mtime changed, and
  refreshes the shared copy of `export_aggregates.py` so the Studio never runs a
  stale exporter. A missing mount (Studio asleep) is a silent skip.

The exporter and installer are staged in that folder already.

**1. Counts-only export (manual).** The same `export_aggregates.py`, run by hand
on any machine, with the file moved over however you like. Identical output to
the automatic path.

**Rejected: an SSH key.** An `authorized_keys` entry grants full shell access as
that user — the script would only *choose* to run a filtered rsync, and nothing
would enforce it — and an unattended key has to sit unencrypted on this machine.
That is far more access than a usage tracker needs, so the transport was removed
(`sync_hosts.py`, deleted 2026-09-04) and its keypair destroyed. The drop folder
does the same job with no access at all. If it is ever revived, it is in git
history; do not reintroduce it without saying plainly what it grants.

Note that a raw-transcript sync would also copy code and conversations onto this
machine. The counts-only path never does.

### Multi-host mechanics

`sync_hosts.py` rsyncs each remote machine's `~/.claude/projects` into
`token-tracker/hosts/<name>/projects` and ingests it tagged with that host, so
the quota denominator finally matches the numerator. Hosts are configured at
`token_tracker.hosts` in the repo-root `config.json`. It is pull-only and
transcript-only (`--include=*.jsonl`) — nothing is written to the remote machine
and no credentials are copied.

Auth uses a dedicated unattended key at `~/.ssh/token_tracker_hosts`. To
authorize a machine, append `~/.ssh/token_tracker_hosts.pub` to its
`~/.ssh/authorized_keys`. Until then `sync_hosts.py` fails with a message naming
exactly that, and the tracker falls back to the estimator above.

Attribution is host-aware: cron-window and channel joins are **skipped for remote
hosts**, since OpenClaw only runs on the mini and a Studio session that merely
overlapped a local cron window would otherwise be labelled as that cron. Remote
sessions resolve to `terminal:<project>`.

## Commands

```bash
S=~/.openclaw/workspace/skills/token-tracker/scripts

python3 $S/report.py                    # today so far → print + post to Discord
python3 $S/report.py --day 2026-09-03   # a specific day
python3 $S/report.py --yesterday        # previous day (what the 5:25am run uses)
python3 $S/report.py --week             # the whole current quota window
python3 $S/report.py --no-post          # print only, don't post
python3 $S/collect.py                   # ingest only (fast, incremental)
python3 $S/collect.py --reattribute     # rebuild all labels after changing rules
python3 $S/quota.py                     # take one quota sample
python3 $S/collect_codex.py             # ingest Codex/OpenAI usage + its quota
python3 $S/import_aggregates.py --watch # import anything new in the drop folder
python3 $S/offmachine.py                # how much quota burn isn't from this Mac

# adding another machine, counts-only (run the first line ON that machine).
# Covers Claude + Codex in one pass; --no-codex to skip Codex.
python3 export_aggregates.py --host studio --out ~/Desktop/studio-tokens.json
python3 $S/import_aggregates.py ~/Desktop/studio-tokens.json
```

For ad-hoc questions, query the DB directly — it is plain SQLite at
`~/.openclaw/workspace/token-tracker/tokens.db`:

```sql
-- most expensive workflows over the last 7 days
SELECT a.label, a.kind, ROUND(SUM(m.cost_usd),2) usd
FROM messages m JOIN attribution a USING(session_id)
WHERE m.day >= date('now','-7 days') GROUP BY 1,2 ORDER BY usd DESC;

-- would this cron be cheaper on a smaller model?
SELECT model, SUM(cache_read), SUM(output_tokens), ROUND(SUM(cost_usd),2)
FROM messages m JOIN attribution a USING(session_id)
WHERE a.label='morning-news-digest' GROUP BY model;
```

## Schedule (zero tokens)

Both jobs are **launchd**, not OpenClaw crons, deliberately: a cost tracker that
burns Opus tokens to report on token burn is self-defeating. These are plain
Python — no model calls.

- `ai.openclaw.token-tracker.sample` — every 30 min: ingest + quota sample.
- `ai.openclaw.token-tracker.report` — daily **05:25 PT**, run with `--yesterday`:
  posts the *previous* day's report. At 5:25am "today" is minutes old, so the
  morning run has to look backwards to say anything.

Logs: `~/.openclaw/logs/token-tracker.{log,err}`.

## Other providers

The report keeps each provider in its own section with its own limits. Merging
them would be meaningless -- these are three different economic models.

**Codex (OpenAI) — tracked.** `collect_codex.py` reads Codex rollout files, which
conveniently carry *both* halves: per-turn `last_token_usage` counts and a
`rate_limits` block with 5-hour and weekly `used_percent`. Three roots, because
Codex under OpenClaw does not share the CLI's home:

- `~/.codex/sessions/` — the user's own terminal Codex
- `~/.openclaw/agents/*/agent/codex-home/` — each agent's ACP sessions (this is
  where nearly all the volume is)

Auth is a ChatGPT **subscription**, not an API key, so like Claude it burns plan
quota rather than dollars. Cost is left at zero unless
`token_tracker.openai_prices` is set in `config.json` — guessing another vendor's
list prices would put invented numbers in a cost report. Note Codex's
`input_tokens` is *inclusive* of `cached_input_tokens`; the collector subtracts
to avoid double-counting.

**Local models (Ollama / LM Studio) — split out, volume only.** Rollout files
record a model id but no provider, and locally-served models reach Codex-style
harnesses through an OpenAI-compatible endpoint — so `qwen3.6` arrives looking
like an OpenAI model. `lib.classify_provider()` resolves this by name:
positively identify Anthropic and OpenAI, treat everything else as `local`.
Local rows get tokens and sessions but no dollars and no quota, because they
have neither. The importer re-derives provider rather than trusting the file, so
exports written before this split are corrected on import without a re-run.

**Why Ollama can't do better than volume.** It is local
inference: no dollar cost and no quota, so it cannot participate in the cost or
quota math at all; only volume and time would mean anything. Worse, nothing
persists per-request counts. Ollama does return `prompt_eval_count` / `eval_count`
per response, but discards them; the ACP trajectory files record
`provider: ollama` and the model id but no usage; and OpenClaw's `sessions.json`
totals are internally inconsistent (one agent session reports
`inputTokens: 93,906` against `totalTokens: 15,962`). Tracking it properly means
logging at the Ollama server on the Studio, which is a separate piece of work --
and it would produce a throughput chart, not a cost line.

## Gotchas

- **Claude Code prunes transcripts after ~30 days.** `tokens.db` is the durable
  archive; history before first collection (2026-08-04) is unrecoverable. Do not
  delete the DB to "rebuild" — old transcripts are gone.
- **Every INSERT names its columns.** This schema gains columns over time, and a
  positional insert breaks silently the moment one is added — it cost five hours
  of lost quota samples when `provider` was added to `quota_samples`, and before
  that wrote `kind` into `label` when `host` was added to `attribution`.
- **Reported utilization is not monotonic.** On 2026-09-04 weekly usage fell from
  24% to 1% with `resets_at` unchanged (an out-of-band credit or plan change).
  Calibration therefore runs over the latest monotonic segment only; dividing the
  full window's spend by the post-reset percentage would have overstated the rate
  ~23x. The report prints `n/a` rather than a number it cannot stand behind.
- **An aggregate import replaces its host entirely.** Snapshots are full history,
  so rows whose provider or key changed between exports would otherwise linger as
  orphans — that is how the `qwen3.6` reclassification took effect without a
  re-export.
- **The `SCHEMA` string and the migration list must agree.** `provider` was added
  to `quota_samples` in the migration only, so existing databases worked while a
  freshly created one lacked the column — invisible until a test built one.
- Ingest is incremental by byte offset, and `INSERT OR IGNORE` on message uuid,
  so re-running is safe and cheap (~1.5s over 4,000 files).
- A partial trailing line in a live transcript is skipped, and the stored offset
  stops before it, so it gets picked up on the next pass.
- Sub-agent turns (`isSidechain`) are counted against the parent session — that
  is correct for cost, since they bill to the same account.
