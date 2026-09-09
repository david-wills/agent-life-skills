# Quantified self coach

Five skills that turn three wearables into one thing worth having: a coach that
knows what you lifted last month and how you slept last night, and can say so
before you leave the house.

Three of them are importers and do nothing clever. The interesting part is what
sits between them and the coach — a structured layer that takes the *same* files
the importers already wrote and re-derives them as SQL, so that the question
"was my average HRV lower in the weeks I trained fasted" has somewhere to be
asked. Full-text search over a markdown rendering of a sleep score cannot answer
it.

## The loop

```
Oura ring        Hevy app        Apple Health
    │                │                │
    │ OAuth2 pull    │ REST pull      │ HAE app POSTs over Tailscale
    ▼                ▼                ▼
import-oura-data  import-hevy-    import-apple-health
   5:15 AM         workouts          nightly + 9 AM gap check
                   5:11 AM
    │                │                │
    └────────────────┼────────────────┘
                     ▼
        canonical markdown on disk
     knowledge/{oura,hevy,apple-health}/
                     │
        ┌────────────┴────────────┐
        ▼                         ▼
   FTS5 index              structured-metrics
   (prose path)            runs as step 2 of each
                           importer's own cron
                                  │
                                  ▼
                    metrics_daily · workouts · exercise_sets
                    sleep_sessions · metrics_daily_resolved
                                  │
                                  │  SELECT only
                                  ▼
                           workout-coach
              GOALS.md + PROGRAM.md + last 14 days + today's readiness
                                  │
                 ┌────────────────┴────────────────┐
                 ▼                                 ▼
        briefing in Discord                a routine on your phone
        Mon & Wed 1:30 PM,                 you open it at the gym,
        or whenever you ask                do it, and Hevy logs it
                 │                                 │
                 └──────────► next morning's sync ◄┘
```

The loop closes on its own. The coach writes a plan to the phone; the phone logs
what actually happened; the next morning's sync makes the log authoritative. At no
point does anyone type a number into a second place.

## The pieces

| Skill | Runs | Does |
| --- | --- | --- |
| [`import-oura-data`](import-oura-data/) | 5:15 AM | Pulls workouts, daily readiness, daily sleep and detailed sleep sessions from the Oura v2 API. Writes one file per event and one per day. |
| [`import-hevy-workouts`](import-hevy-workouts/) | 5:11 AM | Pulls full or delta workout history from Hevy, one markdown file per workout. Converts kg → lb at ingest, not at import. |
| [`import-apple-health`](import-apple-health/) | Nightly | A stdlib HTTP server the Health Auto Export iPhone app POSTs to over Tailscale. Parses each payload into per-day markdown. A separate 9 AM job detects missing dates and pages you while HealthKit's buffer can still be re-sent. |
| [`structured-metrics`](structured-metrics/) | Step 2 of each importer's cron | The numbers path. Re-derives four tables and a resolution view from the canonical files. |
| [`workout-coach`](workout-coach/) | Mon & Wed 1:30 PM, or on demand | Proposes today's session — warmup plus 4-6 exercises with sets, reps and weight — from your program pools, your last fortnight of training, and this morning's recovery. Mirrors it to the phone as a Hevy routine. |

## Ideas worth stealing

**One set of canonical files, two derived paths.** The importers write markdown.
That markdown feeds a full-text index *and* a SQLite schema, and neither is
upstream of the other — both are regenerable from the files. The rule that makes
this safe is one line long: *never edit a table to fix a number.* Fix the source
file and re-ingest. Anything reconstructable only from the database is a bug, and
treating the database as disposable is what lets the schema change without a
migration.

**Keep every source's answer; resolve at read time.** Three devices measure
resting heart rate and they disagree. The obvious design picks a winner at write
time and stores one row. `metrics_daily` stores all three, source-tagged, and a
view collapses them with a fixed priority — the ring measures directly what the
phone infers, so Oura beats Apple Health beats Hevy. Changing your mind about
priority is then a view definition, not a backfill. The cost is one sharp edge
worth knowing: consumers must read the view, because reading the table gives
duplicate rows on every metric more than one device records.

**A writer does not live inside its reader.** `structured-metrics` is a skill of
its own rather than part of the coach, because three importers invoke it and the
coach only ever `SELECT`s from it. Filing it under its one consumer would have put
the writer inside the reader and made "who fills this table" unanswerable from
the table's own directory. Same reasoning kept it out of the importers: it has
three producers, so it belongs beside none of them.

**Gate step two on step one's output, not its exit code.** Each importer's cron
runs the ingest, then the structured re-derive — but only if the first step's
stdout contained `ingested=`. A silent no-op that exits 0 is the failure mode that
actually happens, and gating on the printed count means a broken path surfaces at
the next fire instead of a week later as quietly missing rows. The tradeoff is
that a log line is now an interface: reformatting it breaks the pipeline.

**Overlapping windows instead of cursors.** Every run re-ingests the last 7 days
(14 for Hevy) and upserts. There is no high-water mark to get wrong, and a missed
night heals itself on the next run without anyone noticing it was missed.

**Recovery gates intensity, not attendance.** Low readiness never cancels the
session — the weekday still decides the slot. It caps the target RPE, holds the
weights, or drops to two sets at 10% lighter. A recovery signal that can talk you
out of training is a recovery signal you will eventually stop collecting.

**Show the gate even when it passes.** The briefing always carries a one-line
readiness acknowledgement, including on a green day when it changes nothing.
Otherwise the only evidence the recovery loop is wired is a bad day, and you will
assume it is broken long before you get one.

**Blank is more honest than a fabricated number.** A weight from 90+ days ago is
not evidence about today. For a primary lift the coach suggests a conservative
start and labels it NEW; for an alternate it leaves the weight unset entirely,
because you can read the rack in front of you and it cannot.

**Separate the engine from the opinion.** `SKILL.md` describes how a session is
composed and holds no view about what you should be training. `GOALS.md` and
`PROGRAM.md` hold all of it — the split, the exercise pools, the injuries, the
things you refuse to do. Both live outside the repo, with `.example.md` templates
shipped in their place, for the same reason as any other personal file: an opinion
that leaks into the prompt is one a clone inherits and cannot find to change.

**Unfilled placeholders proceed with a heads-up.** The three fields most likely to
be left blank — injuries, love/hate lifts, current trainer focus — are named
`<EDIT:>` markers. The coach plans anyway on safe assumptions and appends one line
saying what filling them in would sharpen. Blocking on configuration is how a
daily habit dies in week one.

**A briefing must be its own turn.** Bundling the message with the tool call that
pushes the routine to the phone was observed to drop the message entirely — the
harness only flushed text from the final turn. So the contract is: send the
briefing and end the turn, push on the next one, confirm on the one after. It
feels wasteful and it is load-bearing.

## Running it

You need an Oura account with an OAuth2 app registered, a Hevy account with API
access, an iPhone running Health Auto Export, Python 3.11+, and macOS for the
`launchd` scheduling.

```bash
cp config.example.json config.json
cp workout-coach/GOALS.example.md    <workspace>/workout-coach/GOALS.md
cp workout-coach/PROGRAM.example.md  <workspace>/workout-coach/PROGRAM.md
python3 import-oura-data/scripts/authorize_oura.py      # one-time OAuth2 dance
```

Then rewrite the program file. It is the coach's entire exercise vocabulary — it
will not propose a lift that is not in there — and its `★` marks are what drive
the balance between progressing lifts you know and rotating in ones you do not.
Names must match your logging app's exercise titles exactly; the push script
aborts with close-match suggestions rather than guessing.

Tokens are read at runtime and never from config: `HEVY_API_KEY`,
`HAE_REST_TOKEN`, `DISCORD_BOT_TOKEN`, plus the Oura pair used once during
authorization. Configuration resolves env var → `config.local.json` →
`config.json`, with no baked-in fallback — a fresh clone fails loudly rather than
running against someone else's channels.

Each skill's `SKILL.md` carries its own flags, invariants and failure modes. Start
with `--dry-run`; every stage that writes has one.

## Honest limits

- **The coach is a prompt, not a program.** `workout-coach` is mostly `SKILL.md`:
  the two scripts bundle context and push a routine, and an agent does the
  composing in between. Without a capable model driving it you have a context
  bundler and a Hevy client.
- **Scheduling is macOS `launchd`, and the crons are not in this repo.** The
  `.plist.template` files carry `{{HOME}}` and need rendering. The three daily
  sync jobs are agent-gateway cron definitions living in a SQLite store, not
  files — the ordering contract in `structured-metrics/SKILL.md` describes them,
  but you will be recreating them by hand.
- **Apple Health arrives over Tailscale or not at all.** The server listens on a
  tailnet interface and the iPhone app pushes to it. There is no polling fallback,
  which is exactly why the gap check exists — this is the leg that breaks, and it
  breaks silently.
- **The Hevy integration is Hevy-shaped.** Exercise-template ids, rep ranges,
  routine overwrite semantics: the push script speaks one app's API and there is
  no abstraction to swap.
- **Three sources, one opinion about which wins.** The resolution priority is a
  fixed order in a view, not a per-metric judgement. It is right for heart-rate
  metrics and merely defensible for the rest.
