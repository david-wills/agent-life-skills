# Field notes: quantified-self-coach

The operational history behind the package: what runs when, what broke, what was
tried and thrown away, and why some choices look strange. It is kept out of the
`SKILL.md` files on purpose so the instructions an agent reads stay short. Nothing
here is needed to run the thing; all of it is useful for judging the design.

## How it runs

- Three sync jobs fire within about half an hour of each other in the early
  morning, Hevy first, then Oura, then Apple Health. Each is a short command
  sequence in whatever scheduler you have: importer, full-text ingester, then
  `structured-metrics/scripts/ingest_metrics.py --source <x>`. The third step
  runs only if the second printed `ingested=`.
- Apple Health has no pull step. The phone app POSTs to
  `import-apple-health/scripts/server.py` whenever it feels like it, usually
  overnight; the morning job just indexes whatever landed. A separate gap check
  runs mid-morning (the shipped template says 09:00), after the phone has had
  its chance.
- The coach posts unprompted on each solo lifting day, a few hours before the
  gym, into the Discord channel in `discord.channels.workouts`. The same
  composition runs on demand whenever someone asks in that channel. Each
  proactive post is followed by a push to the phone as a Hevy routine.
- Nothing is long-running except the ingest server, which launchd keeps alive.
  The importers are a handful of HTTP calls and finish in seconds; a full Hevy
  history pull is the slow one because the API caps pages at 10 workouts. The
  structured re-derive is a local SQLite pass over a 7- or 14-day window and is
  not worth timing.
- The proactive post goes through `lib/discord.py`, which splits at the
  platform's message limit and suppresses link embeds so a briefing is text, not
  a card.
- Verification is by artifact, not exit code: `SELECT source, MAX(date) FROM
  metrics_daily GROUP BY source`. A stale max date for one source points
  upstream at that source's importer (phone stopped syncing, token expired),
  almost never at the structured layer.
- Ordering is the only cross-skill contract. `structured-metrics` reads files
  the importers wrote seconds earlier, and
  `workout-coach/scripts/build_context.py` reads tables `structured-metrics`
  wrote hours earlier. Neither knows the other's schedule; the job that calls
  them does.

## What broke, and what it taught

- **Apple Health stops arriving, silently.** The phone app skips a night,
  HealthKit's recent-data buffer rotates, and the day is gone. Nothing exits
  non-zero because nothing ran. `gap_check.py` walks the last 14 fully-elapsed
  days, posts the missing dates to the errors channel, and suppresses repeats of
  the same missing set for 6 hours. Early on it saved its cooldown state before
  the post succeeded, so a failed alert muted itself; state is now written only
  after a successful post.
- **Two POSTs in the same second.** The phone app sends metrics and workouts as
  separate payloads that can land together, and both merge into the same per-day
  file. `parse_hae_payload.py` serialises merges behind a process lock. It also
  recovers a day from its markdown if the JSON sidecar is missing, rather than
  dropping the day; `.sidecar/` looks like a cache and is not one, and deleting
  it should cost detail, not days.
- **An evening session logged as tomorrow.** Hevy stores `start_time` in UTC; a
  6 PM session west of Greenwich is already the next day in UTC. The markdown
  used the local day and the `workouts` table used the UTC day, so "did I train
  Tuesday" had two answers and the coach's 14-day window was off by one at the
  edges. Both now use the local calendar day of `start_time` (`local_day()` in
  `ingest_metrics.py`).
- **Cascade deletes emptied the sets table.** `exercise_sets` references
  `workouts` with `ON DELETE CASCADE`. The workouts upsert was `INSERT OR
  REPLACE`, which is a delete plus insert, so every re-ingest cascaded away the
  sets before re-deriving them, and sets were cleared for every workout in the
  window whether or not its raw JSON was still on disk. Prune old exports and a
  routine morning run silently zeroed a month of volume. The upsert is now `ON
  CONFLICT DO UPDATE`, and sets are cleared and rewritten only for workouts
  present in a file under `<data_root>/imports/hevy/`.
- **The briefing vanished.** Composing the message and running the Hevy push in
  the same agent turn dropped the message: the harness only flushed text from
  the final turn. The fix is procedural, not code. Turn A is the briefing with
  no tool calls, turn B is the push, turn C is the one-line confirmation. It
  reads as wasteful and it is load-bearing.
- **Exercise names that almost match.** Hevy resolves exercises by template id,
  and the push script maps names to ids from a cached catalog. A name from
  PROGRAM.md that differs by a parenthesis from the Hevy title used to abort
  every push. Now an unresolved name triggers one cache refresh (custom
  exercises the user added in the app), then aborts with up to five close
  matches; persistent renames go in
  `<data_root>/workout-coach/hevy-cache/aliases.json`. The rule that survived:
  the program file must use Hevy's exact titles.
- **Nearly-empty workouts.** A session started on a watch and never filled in (a
  trainer day, typically) arrives with no exercises. Skipping it would make the
  day look untrained. `ingest_hevy.py` renders it as an empty session so the day
  still counts.
- **A silent no-op that exits 0.** An importer that finds nothing, or points at
  an empty folder, is indistinguishable from success by exit code. The daily
  jobs gate step three on step two's `ingested=` line. The cost is that a log
  line is now an interface; the summary format in the ingesters is not free to
  change.
- **Deriving paths from `__file__`.** In the private repo the skills were
  reached through a symlink, so resolving a script's own location landed in the
  repo instead of the data tree, and one sibling skill wrote to the wrong place
  for a while. Every path is now derived from `data_root()` in
  `lib/skill_config.py`; no script anchors on itself.
- **Oura retired personal tokens.** The API is OAuth2-only, and the client-side
  flow needs re-authorising every 30 days. `authorize_oura.py` uses the
  server-side flow to get a refresh token, which rotates on every refresh and is
  persisted back. Lose the token file and you redo the browser dance; nothing
  else recovers it. Transient 429/5xx responses are retried with short backoff
  before the run fails.
- **The scale went quiet.** A body-weight source stopped syncing and stayed
  silent for months. The tiering in `import-apple-health/SKILL.md` says "surface
  when present, do not error when absent" for anything only one device provides:
  a device going quiet is normal, not a fault.
- **Edited old sessions never came back.** The Hevy delta pull filters on
  `start_time`, so a workout edited after the cutoff but started before it is
  not re-pulled. The importer stops paging at the first workout older than the
  cutoff on purpose; the fix is an occasional full pull, documented in
  `import-hevy-workouts/SKILL.md` rather than coded around.
- **Server hygiene.** The first cut bound all interfaces, compared the bearer
  token with `==`, and returned the traceback in the 500 body. It now binds
  loopback unless told otherwise, compares in constant time, and returns a bare
  `ingest_failed`; the trace goes to the log.

## Things tried and dropped

- **A Dropbox-specific backfill.** The first historical import read the Health
  Auto Export app's Dropbox folder by its fixed path. That script was deleted in
  favour of `backfill_from_folder.py`, which replays any folder of archived
  exports through the same merge as the live server.
- **One rolling routine.** The phone originally carried a single "Today's
  Workout" that every push overwrote. An upper-body push would clobber a
  lower-body plan still sitting on the phone undone. It became two slot-specific
  routines, and the legacy no-slot fallback was removed: a plan with neither a
  `slot` nor a slot hint in its title is rejected.
- **A trainer-specific bias baked into the prompt.** For a while `SKILL.md`
  carried a section naming particular exercises (carries, anti-rotation core,
  paused squats) to favour in the unmarked slot, with its own recency rule,
  because that was the current trainer's emphasis. It was a personal judgement
  in a shared instruction file. It became the generic "Secondary emphasis"
  section of `GOALS.md`: whatever is written there applies only to the unmarked
  slot, only as a tie-breaker, never twice in a row.
- **Restating the program in the prompt.** Rep ranges, RPE, working sets, rest,
  the progression rule and the weekly cadence were all repeated in `SKILL.md`
  and drifted from `PROGRAM.md`. They now live in the two personal files'
  Defaults and Cadence tables and the prompt only says how to read them.
- **Slack as a second surface.** The coach once answered in a Slack channel too,
  with a CommonMark-to-mrkdwn conversion and a ban on slash commands because
  Slack ate the leading slash. Discord is the only surface now. The "no slash
  commands" rule survived on its own merits: triggers are natural language.
- **Job definitions in the agent gateway's store.** The three daily jobs used to
  be prompts in a SQLite store, invisible to `grep`, and every path change meant
  editing a job nobody could diff. The repo now ships the commands and leaves
  the scheduler to you.
- **Environment-variable server config.** Bind host and port came from env vars
  and defaulted to all interfaces. They are flags now, with loopback as the
  default and a `{{BIND}}` placeholder in the launchd template.
- **A markdown-walking context bundler.** The first `build_context.py` parsed
  the per-workout markdown files directly. It was replaced by the SQL-backed one
  once `structured-metrics` existed; the markdown walker had no way to compute a
  30-day HRV baseline.
- **Oura's activity collection.** `daily_activity` (steps, calories) was
  considered and left out of the import on purpose: low signal for a lifting
  coach, and Apple Health already carries steps. Four collections, not five.
- **Six copies of the FTS5 DDL.** Each ingester carried its own byte-identical
  `CREATE VIRTUAL TABLE`. It was deferred out of the split that created
  `structured-metrics` to keep that diff reviewable, and later collected into
  `lib/fts_schema.py`.

## Decisions that look odd

- **The coach is a prompt, not a program.** The two scripts bundle context and
  push a routine; the picking happens in an agent reading `SKILL.md`. A rules
  engine would need every judgement written down in advance, and the judgements
  change weekly. The cost is real: without a capable model you have a context
  bundler and a Hevy client.
- **GOALS.md and PROGRAM.md live outside the tree.** They hold every personal
  opinion the coach has: injuries, cadence, the exercise vocabulary. An opinion
  that leaks into `SKILL.md` is one a clone inherits and cannot find to change.
  The `.example.md` templates ship in their place.
- **`structured-metrics` is its own skill.** Three importers write to it and one
  consumer reads from it. Filing it under the coach puts the writer inside its
  reader; filing it under one importer hides the other two producers. The split
  also makes "who fills this table" answerable from the directory.
- **Sets are rewritten only when raw JSON exists.** The markdown carries enough
  to refresh a `workouts` row but not enough to rebuild per-set rows. Rewriting
  sets from a partial source would replace good data with less; leaving them
  alone keeps the table honest after exports are pruned.
- **kg to lb at ingest, not at import.** The raw export stays exactly what the
  API returned, which is what a backfill replays. Everything downstream
  (markdown, tables, context bundle, briefing) agrees on one unit, and
  `push_to_hevy_routine.py` converts back for the API. Convert at the edges,
  never in the middle.
- **Every source's answer is kept.** `metrics_daily` stores each device's
  version of a metric and `metrics_daily_resolved` picks one with a fixed
  priority (Oura, then Apple Health, then Hevy). Changing your mind is a view
  definition, not a backfill. The sharp edge is that consumers must read the
  view.
- **Overlapping windows instead of cursors.** Every run re-ingests the last 7
  days (14 for Hevy) and upserts. The Hevy importer's watermark is the one
  cursor, and even it subtracts an hour so consecutive runs overlap.
- **Recovery gates intensity, never attendance.** Low readiness caps RPE, holds
  weights or drops to two lighter sets; the weekday still decides the slot. A
  signal that can talk you out of training is one you will stop collecting. The
  one-line readiness acknowledgement is printed even on green so the loop is
  visibly wired.
- **Blank beats a fabricated weight.** A lift not seen for 90 days gets a
  conservative labelled "NEW" start if it is the primary, and no weight at all
  if it is an alternate. The person at the rack can see the rack.
- **Apple Health workouts share the `workouts` table, but the coach ignores
  them.** Ring-logged cardio and phone-logged walks land beside Hevy sessions,
  source-tagged; `exercise_sets` is Hevy-only. The context bundle's session list
  is filtered to Hevy on purpose, because a walk is not a training day for slot
  selection.
- **The Hevy integration is Hevy-shaped.** Template ids, rep ranges, routine
  overwrite semantics: the push script speaks one app's API and there is no
  abstraction to swap. One logging app was the whole point; an abstraction over
  one implementation is a guess about the second.
- **No polling fallback for Apple Health.** The only way out of HealthKit is the
  phone pushing. Rather than pretend otherwise, the design accepts that this leg
  breaks and spends its effort on noticing quickly.
- **Health files are owner-only.** Every writer sets a 077 umask, so new
  markdown, JSON and the index land as 0600. Existing files keep their mode.

## Numbers worth knowing

| Constant | Value | Lives in |
| --- | --- | --- |
| Structured re-derive window | 7 days (Oura, Apple Health), 14 days (Hevy); default 7 with no flag | `structured-metrics/scripts/ingest_metrics.py`, `structured-metrics/SKILL.md` |
| Hevy watermark overlap | `last_synced_at` minus 1 hour | `import-hevy-workouts/scripts/import_hevy_workouts.py` |
| Hevy page size | 10 (API maximum) | `import-hevy-workouts/scripts/import_hevy_workouts.py` |
| Oura default backfill | 90 days; access token refreshed within 5 minutes of expiry; transient errors retried with 2 s then 5 s backoff | `import-oura-data/scripts/import_oura_data.py` |
| Gap check | 14-day window, 6-hour cooldown, template schedules 09:00 | `import-apple-health/scripts/gap_check.py`, `import-apple-health/launchd/` |
| Ingest server | binds `127.0.0.1:8787`; `/status` keeps the last 5 ingests | `import-apple-health/scripts/server.py` |
| Readiness bands | green at 80 and above, yellow 65 to 79, red below 65 | `workout-coach/scripts/build_context.py`, `workout-coach/SKILL.md` |
| Secondary escalation | green becomes yellow if sleep score is below 60 or HRV is below 70% of the 30-day baseline | `workout-coach/scripts/build_context.py` |
| Context lookback | 14 days of sessions, 365 days of per-exercise last working set | `workout-coach/scripts/build_context.py` |
| Weight staleness | up to 30 days: use and progress; 31 to 90: use and hold; over 90 or absent: NEW | `workout-coach/SKILL.md` |
| Flex-day threshold | a slot is due when 4 or more days stale | `workout-coach/GOALS.example.md` |
| Rest between sets | 90 s default per exercise; 60 s on the placeholder set when a routine is first created | `workout-coach/scripts/push_to_hevy_routine.py` |
| Name resolution | one cache refresh, then up to 5 close matches at cutoff 0.5 | `workout-coach/scripts/push_to_hevy_routine.py` |
| Discord message split | 2,000 characters | `lib/discord.py` |
