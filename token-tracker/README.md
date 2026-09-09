# Token tracker

One skill that answers, for any day or quota window: how many tokens went where,
what that would have cost at API list prices, and what share of a Claude
subscription's weekly quota it burned. Broken out by workflow (which cron job,
which chat channel, which terminal session) and by model.

It exists because a subscription hides cost. Nothing tells you that one nightly
cron is eating a third of your week, or that a channel you barely use is on the
most expensive model. This makes both visible without spending a single token to
do it.

## The loop

```
~/.claude/projects/**/*.jsonl              the agent gateway's SQLite store (optional)
   every assistant turn carries            cron run windows, channel bindings
   a full `usage` block                              │
         │                                           │
         └──────────────┬────────────────────────────┘
                        │  sample.py — every 30 min: collect.py (incremental, by
                        │  byte offset) + quota.py (one utilization sample)
                        ▼
                   tokens.db  (SQLite; the durable archive)
                        │
                        │  attribution: cron ▸ channel ▸ channel (durable) ▸ terminal
                        ▼
                   report.py — once a day, `--yesterday`
                        │
                        ▼
                Discord #token-tracker
        per-workflow $ and ≈% of weekly quota, per model, with the
        conversion rate's confidence stated in the report itself
```

## The pieces

| Script | Does |
| --- | --- |
| `sample.py` | The half-hourly heartbeat, and the only thing to schedule: ingest, attribute, Codex, inbox import, quota sample, reset alert. Each step isolated. |
| `collect.py` | Ingests transcripts into SQLite and labels each session with the workflow that produced it. Safe to re-run; `INSERT OR IGNORE` on message id. |
| `quota.py` | Takes one sample of the account's weekly utilization from the usage endpoint the `/usage` command reads. |
| `report.py` | Prints and posts the day, the week, or a given date. `--no-post` to keep it local. |
| `collect_codex.py` | Same for Codex (OpenAI): per-turn counts and the rate-limit block, kept in its own section. |
| `export_aggregates.py` / `import_aggregates.py` | Counts-only transfer from another machine, so the quota denominator matches the numerator without copying transcripts. |
| `offmachine.py` | Estimates how much quota burn is not from this machine at all. |
| `lib.py` | Prices, schema, provider classification, host label, keychain read. |

## Ideas worth stealing

**Attribute by containment, not overlap.** A cron run window that *contains* the
whole model session, on the same model, is the cron that produced it. Overlap
would label a long-lived interactive session as whatever cron happened to fire
during it. Four passes run in order of evidence strength, and what is left is
reported as `unattributed` with its dollar share rather than folded into another
row.

**Derive the quota conversion every run and say how sure you are.** Anthropic
reports utilization as a percentage, not tokens, and the weighting is not
dollars. So the tracker divides spend in the current window by the percentage
the account reports, per monotonic segment, and the report warns when segments
disagree. Treat `≈X% wk` as an order of magnitude. The dollar figures are exact.

**A cost tracker must not cost anything.** Both schedules are plain Python under
launchd or cron. An agent cron that burns a frontier model to report on model
burn is self-defeating.

**Counts, not transcripts, cross machines.** The first design used an SSH key so
one Mac could rsync another's transcripts. That grants a full shell to read a
usage number. It was removed in favour of a counts-only JSON export dropped in a
shared folder. Same output, no access.

**Every INSERT names its columns.** The schema gains columns over time. A
positional insert breaks silently the moment one is added, and did.

**Stop the byte offset before a half-written line.** A live transcript usually
ends mid-record. Storing EOF as the resume point would skip that record forever
once it completed; storing the start of the unparsed line picks it up next pass.

## Running it

- Config keys: `discord.channels.token_tracker`; `token_tracker.host` (the label
  on this machine's rows, default `this-mac`; any other machine's export must
  use a different one); optionally `token_tracker.inbox` (a folder where other
  machines drop counts-only exports) and `token_tracker.openai_prices` (left
  unset, Codex cost shows as zero rather than a guessed number).
- Schedule `sample.py` every 30 minutes and `report.py --yesterday` once a day,
  with launchd or cron. No model calls, so no agent scheduler is needed.
- The database is plain SQLite at `<data_root>/token-tracker/tokens.db`.
  Ad-hoc questions are one query away:

```sql
-- most expensive workflows over the last 7 days
SELECT a.label, a.kind, ROUND(SUM(m.cost_usd),2) usd
FROM messages m JOIN attribution a USING(session_id)
WHERE m.day >= date('now','-7 days') GROUP BY 1,2 ORDER BY usd DESC;
```

## Honest limits

- **Claude Code prunes transcripts after about 30 days.** The database is the
  archive; history before first collection is gone. Never delete it to rebuild.
- **Workflow attribution needs the OpenClaw gateway.** Cron and channel labels
  come from its own state files (`~/.openclaw/state/openclaw.sqlite`,
  `~/.openclaw/agents/*/sessions/sessions.json`). Without it every session is
  `terminal:<project>` or `unattributed`; tokens, costs and quota still work.
- **Quota needs a macOS keychain and a Claude Code subscription login.** The
  utilization sample reads the OAuth token Claude Code stores when you sign in.
  API-key users get costs but no quota percentage; `quota.py` says so in one
  line and `sample.py` carries on without it.
- **Utilization is account-wide; transcripts are per machine.** Use on another
  machine lands in the denominator and not the numerator, so quota shares read
  high until that machine exports its counts.
- **The usage endpoint is not a public API.** It is what the CLI's `/usage`
  reads. It could change without notice.
- **Reported utilization is not monotonic.** Plan changes and credits reset it
  mid-window. Calibration runs on the latest monotonic segment only and prints
  `n/a` rather than a number it cannot stand behind.
- **Local models get volume only.** Ollama returns per-request counts and then
  discards them; nothing persists to read. Zero dollars, zero quota, so they
  cannot join the cost math anyway.
