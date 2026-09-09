---
name: token-tracker
description: Track Claude token usage, API-equivalent cost, and subscription quota burn per day, per workflow (cron job / Discord channel / terminal session), and per model. Answers "what used up my weekly quota", "what does the newsfeed cron cost", and "where should I switch models". Use when the user asks about token usage, model costs, quota, rate limits, or which workflows are expensive.
---

# Token Tracker

Answers, for any day or quota window: **how many tokens went where, what that
would have cost on the API, and what percentage of the weekly Claude subscription
quota it burned** — broken out by workflow and by model.

Output goes to Discord **#token-tracker** (`discord.channels.token_tracker`
in the repo-root `config.json`). This machine's rows are labelled with
`token_tracker.host` (default `this-mac`).

## Where the numbers come from

| What | Source | Why this one |
|---|---|---|
| Token counts (input / output / cache read / cache write 5m+1h / thinking) | `~/.claude/projects/**/*.jsonl` | Every assistant message carries a full `usage` block. An agent harness that drives Claude Code (OpenClaw's `claude-cli` provider, for one) writes here too, so agent runs and the user's own terminal sessions are both captured. |
| Workflow attribution | OpenClaw's own state: `~/.openclaw/state/openclaw.sqlite` (`cron_run_logs` + `cron_jobs`), transcript `chat_id`, `~/.openclaw/agents/*/sessions/sessions.json` | Names each session: cron job, Discord channel, or terminal. **Optional** — without OpenClaw every session resolves to `terminal:<project>` or `unattributed`. |
| Real quota utilization | `GET https://api.anthropic.com/api/oauth/usage` | The endpoint `/usage` uses. The **only** source of true quota burn — Anthropic does not publish the weekly limit as a token or dollar figure. Needs the Claude Code subscription login in the macOS keychain. |
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
   (`sdk-cli` means a harness drove it).

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
warns when they disagree by more than 1.5x. In one observed week the two segments
differed 2.4x, because an Opus-heavy stretch and a Haiku-heavy one were being
averaged into one figure.

So: treat `≈X% wk` as an order of magnitude, not a measurement. The dollar
figures are exact; the quota conversion is an estimate with a wide band. Early in
a fresh window the percentage is also small and integer-rounded, which widens it
further.

**Why API-equivalent dwarfs the subscription price.** A day whose API-equivalent
spend is a multiple of the whole monthly plan is not a pricing error — most of
that spend is cache reads and writes, i.e. re-reading the same context on every
turn of an agentic loop. The API bills per read; a subscription bills capacity
and enforces rate limits instead. "API-equivalent" is a counterfactual — what
this traffic *would* have cost at list prices — not a discount being realised,
and not a number anyone would actually pay, because on metered billing the work
would be restructured.

## Off-machine usage (the important caveat)

**Utilization is account-wide; transcripts are per machine.** Claude used on any
other machine lands in the denominator but not the numerator, so `$ per 1%`
reads **low** and every workflow's quota share reads **high**. Dollar costs stay
exact either way — only the quota percentages skew.

`offmachine.py` sizes the gap without needing access to the other machine:

- **Scoped buckets.** The `limits` array in the usage response carries per-model
  weekly buckets. A model with quota burn and zero local messages can only have
  run elsewhere — and it also shows that the top-level `seven_day_opus` field
  being null does *not* mean per-model limits are absent.
- **Drift regression.** Across consecutive samples inside one 5-hour window, fit
  `d_utilization = k·d_local_spend + r·d_hours`. `k` gives the local dollar→quota
  conversion; `r` is burn local spend can't explain, i.e. the other machines.
  Needs ~8 intervals (4h of sampling) before it reports.

While any unseen model is burning quota, the report labels its quota percentages
as upper bounds rather than presenting them as settled.

### Closing the gap: counts-only export

On the other machine, run the standalone exporter (stdlib only, no clone needed;
copy the one file across):

```bash
python3 export_aggregates.py --host laptop --out ~/Desktop/laptop-tokens.json
```

It emits per (day, model, project) token totals for Claude and Codex — no
prompts, responses, code, or session ids. Bring the file here and import it:

```bash
python3 token-tracker/token-tracker/scripts/import_aggregates.py ~/Desktop/laptop-tokens.json
```

An import replaces that host's rows entirely, so re-importing an updated export
overwrites rather than double-counts. `--host` must differ from this machine's
`token_tracker.host`; the importer refuses its own label.

**To automate it**, set `token_tracker.inbox` to any folder both machines can
reach (a synced drive, a network share). `sample.py` then keeps a current copy of
`export_aggregates.py` in that folder, so the other machine never runs a stale
exporter, and imports anything whose mtime changed. Schedule the exporter on the
other machine to write into the same folder. A missing folder is a silent skip.

**Rejected: an SSH key.** The first version rsynced the other machine's raw
transcripts. An `authorized_keys` entry grants full shell access as that user —
the script would only *choose* to run a filtered rsync, and nothing would
enforce it — and an unattended key has to sit unencrypted on this machine. That
is far more access than a usage tracker needs, and a transcript sync also copies
code and conversations. The counts-only export does the same job with no access
at all; do not reintroduce a key without saying plainly what it grants.

Attribution is host-aware: cron-window and channel joins are **skipped for
imported hosts**, since the gateway state only describes this machine; those
sessions resolve to `terminal:<project>`.

## Commands

```bash
S=token-tracker/token-tracker/scripts       # from the repo root

python3 $S/sample.py                     # the half-hourly heartbeat (see Schedule)
python3 $S/report.py                     # today so far → print + post to Discord
python3 $S/report.py --day YYYY-MM-DD    # a specific day
python3 $S/report.py --yesterday         # previous day (what the morning schedule uses)
python3 $S/report.py --week              # the whole current quota window
python3 $S/report.py --no-post           # print only, don't post
python3 $S/report.py --no-collect        # skip ingest + quota sample; report stored data
python3 $S/collect.py                    # ingest only (fast, incremental)
python3 $S/collect.py --reattribute      # rebuild all labels after changing rules
python3 $S/collect.py --quiet            # no progress lines
python3 $S/quota.py                      # take one quota sample (exits 1 without a subscription login)
python3 $S/collect_codex.py              # ingest Codex/OpenAI usage + its quota
python3 $S/import_aggregates.py FILE     # import one counts-only export
python3 $S/import_aggregates.py --watch  # import anything new in token_tracker.inbox
python3 $S/offmachine.py                 # how much quota burn isn't from this machine

# on the OTHER machine (standalone file, stdlib only):
python3 export_aggregates.py --host laptop --out ~/Desktop/laptop-tokens.json
#   --projects DIR   transcript root (default ~/.claude/projects)
#   --no-codex       skip Codex rollouts
```

For ad-hoc questions, query the DB directly — it is plain SQLite at
`<data_root>/token-tracker/tokens.db`:

```sql
-- most expensive workflows over the last 7 days
SELECT a.label, a.kind, ROUND(SUM(m.cost_usd),2) usd
FROM messages m JOIN attribution a USING(session_id)
WHERE m.day >= date('now','-7 days') GROUP BY 1,2 ORDER BY usd DESC;

-- would this cron be cheaper on a smaller model?
SELECT model, SUM(cache_read), SUM(output_tokens), ROUND(SUM(cost_usd),2)
FROM messages m JOIN attribution a USING(session_id)
WHERE a.label='<a cron label>' GROUP BY model;
```

## Schedule (zero tokens)

Two jobs, both plain Python with no model calls, under launchd or cron — not an
agent scheduler, deliberately: a cost tracker that burns frontier-model tokens
to report on token burn is self-defeating.

- `sample.py` every 30 minutes: ingest, attribute, Codex, inbox import, quota
  sample, out-of-band-reset alert.
- `report.py --yesterday` once a day, early: posts the *previous* day's report.
  In the small hours "today" is minutes old, so the morning run has to look
  backwards to say anything.

## Other providers

The report keeps each provider in its own section with its own limits. Merging
them would be meaningless -- these are three different economic models.

**Codex (OpenAI) — tracked.** `collect_codex.py` reads Codex rollout files, which
conveniently carry *both* halves: per-turn `last_token_usage` counts and a
`rate_limits` block with 5-hour and weekly `used_percent`. Two roots, because
Codex driven by the OpenClaw gateway does not share the CLI's home:

- `~/.codex/sessions/` — the user's own terminal Codex
- `~/.openclaw/agents/*/agent/codex-home/` — each agent's ACP sessions (absent
  without OpenClaw)

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
`provider: ollama` and the model id but no usage; and the gateway's `sessions.json`
totals are internally inconsistent (one agent session reported far more
`inputTokens` than `totalTokens`). Tracking it properly means logging at the
Ollama server itself, which is a separate piece of work -- and it would produce a
throughput chart, not a cost line.

## Gotchas

- **Claude Code prunes transcripts after ~30 days.** `tokens.db` is the durable
  archive; history before first collection is unrecoverable. Do not delete the
  DB to "rebuild" — old transcripts are gone.
- **Every INSERT names its columns.** This schema gains columns over time, and a
  positional insert breaks silently the moment one is added — it cost hours of
  lost quota samples when `provider` was added to `quota_samples`, and before
  that wrote `kind` into `label` when `host` was added to `attribution`.
- **Reported utilization is not monotonic.** Weekly usage has been seen to fall
  from double digits to near zero with `resets_at` unchanged (an out-of-band
  credit or plan change). Calibration therefore runs over the latest monotonic
  segment only; dividing the full window's spend by the post-reset percentage
  would overstate the rate by an order of magnitude. The report prints `n/a`
  rather than a number it cannot stand behind, and `sample.py` posts a one-time
  alert when it sees such a drop.
- **An aggregate import replaces its host entirely.** Snapshots are full history,
  so rows whose provider or key changed between exports would otherwise linger as
  orphans — that is how the `qwen3.6` reclassification took effect without a
  re-export.
- **The `SCHEMA` string and the migration list must agree.** `provider` was added
  to `quota_samples` in the migration only, so existing databases worked while a
  freshly created one lacked the column — invisible until a test built one.
- Ingest is incremental by byte offset, and `INSERT OR IGNORE` on message uuid,
  so re-running is safe and cheap (a second or two over thousands of files).
- A partial trailing line in a live transcript is skipped, and the stored offset
  stops at the *start* of that line, so it gets picked up whole on the next pass.
  Resuming mid-file also seeds the requestId set from the database, so a call
  whose first content block landed on the previous pass is not counted twice.
- Sub-agent turns (`isSidechain`) are counted against the parent session — that
  is correct for cost, since they bill to the same account.
