# Example: a daily token report

`report.py` on a fixture database. The transcripts are synthetic: four
projects, three models, two days, twenty sessions, random token counts in
plausible ranges, plus one counts-only export from a second machine and one
quota sample so the calibration lines appear. No agent gateway was present, so
every session is attributed as `terminal:<project>`; in production the same
section also shows cron jobs and chat channels by name.

How it was made:

```bash
# fixture transcripts under <tmp>/projects/<project>/<session>.jsonl
SKILLS_PATHS_DATA_ROOT=<tmp>/data SKILLS_TOKEN_TRACKER_HOST=studio SKILLS_TOKEN_TRACKER_TIMEZONE=America/Los_Angeles \
  python3 -c "import collect, lib, report; ..."   # collect.ingest(root=...), collect.attribute(), one quota_samples row, report.build(day='2025-09-11')
```

The dollar figures are API-equivalent cost at the list prices in `lib.py`
(dated by `PRICES_AS_OF`), not a bill; the point of the report is where a
subscription's weekly allowance goes, and the `≈% wk` column is spend converted
at the rate the quota sample implies.

```text
**Token report — 2025-09-11**

__Claude (subscription quota)__
**Total:** 21.0M tokens · **$41.56** API-equivalent (all machines) · ≈11.8% of a weekly allowance
· in 174.3K · out 445.6K (thinking 247.5K) · cache read 16.9M · cache write 3.4M
· 82% of input was cache reads (billed at 10%)

**Subscription quota (live):** 5-hour **31%** · weekly **23%**
· window spend $81.30 → 1% of weekly quota ≈ $3.53
· **23% is what you have actually consumed** — the ≈% figures below convert spend at the derived rate and do not add up to it
· weekly resets Sun Sep 14 5:00PM

**By machine**
· **studio** — $40.09 (≈11.34% wk) · 19.8M in / 426.7K out · 10 sessions
· **laptop** — $1.47 (≈0.42% wk) · 714.1K in / 18.9K out · 1 sessions

**By model**
· **claude-opus-5** — $39.57 (≈11.20% wk) · in 152.6K · out 385.0K · cache r/w 14.8M/3.0M
· **claude-sonnet-5** — $1.67 (≈0.47% wk) · in 16.3K · out 45.8K · cache r/w 1.5M/300.5K
· **claude-haiku-4-5** — $0.31 (≈0.09% wk) · in 5.4K · out 14.8K · cache r/w 608.6K/119.5K

**By workflow**
⌨️ **terminal:Users-you-code-agent-life-skills** — $20.17 (≈5.71% wk) · 9.8M in / 207.1K out · 3 runs
⌨️ **terminal:Users-you-code-webapp** — $18.29 (≈5.17% wk) · 7.8M in / 169.4K out · 3 runs
⌨️ **terminal:Users-you-code-newsfeed** — $1.57 (≈0.44% wk) · 2.0M in / 47.7K out · 3 runs
⌨️ **terminal:Users-you-code-webapp** _(laptop)_ — $1.47 (≈0.42% wk) · 714.1K in / 18.9K out · 1 run
⌨️ **terminal:Users-you-notes** — $0.06 (≈0.02% wk) · 126.8K in / 2.5K out · 1 run
```

Had one of the models been missing from the price table, a line
`· _unpriced: <model> — costed at the Opus tier ..._` would follow the totals.
