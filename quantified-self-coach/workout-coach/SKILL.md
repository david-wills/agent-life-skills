---
name: workout-coach
description: Propose today's lifting session from program pools, recent Hevy history and Oura recovery, then push it to Hevy. Use when the user asks what to train today.
compatibility: Runs inside an agent runtime bound to a Discord channel. Requires a Hevy API key to mirror the routine to the phone.
---

# Workout Coach

**A planning aid, not medical advice.** It never overrides pain, a clinician's instruction or the injuries in GOALS.md, and when in doubt it picks the lighter option.

## When to invoke

Conversational, not a slash command. Invoke this skill when the user sends a
workout-planning intent in the workouts channel (`discord.channels.workouts`). Reply
in the same channel. Recognizers include but aren't limited to:
- "what should I do today?"
- "plan my workout" / "workout plan" / "plan me"
- "gym today" / "UB today" / "LB today"
- "coach me" / "what's today"
- Any message asking for a proposed session.

If the intent is clearly *logging* a completed workout, asking about a past session, or general banter — **do not invoke the coach flow**. They log in Hevy; we ingest from there.

## Ground truth inputs

Read all four before composing a response. `<data_root>` is `paths.data_root` in
`config.json`, default `<repo>/_data`.

1. **`<data_root>/workout-coach/GOALS.md`** — the primary goal, injury / love-hate / trainer-focus constraints, and any secondary emphasis.
2. **`<data_root>/workout-coach/PROGRAM.md`** — canonical split, movement-pattern exercise pools (★ = already in Hevy history; unmarked = new option), rep-range/RPE/progression defaults.

   Both live outside the tracked tree and hold every personal judgement the skill has — this
   file deliberately holds none. `GOALS.example.md` and `PROGRAM.example.md` ship beside
   it as the templates; without a filled-in copy of each at the paths above, the coach
   has no vocabulary to plan from.
3. **`python3 quantified-self-coach/workout-coach/scripts/build_context.py`** — reads `<data_root>/knowledge/index.db` (the structured metrics layer; `SELECT` only). Emits:
   - Today's date and weekday. The slot is not in the bundle: read it off GOALS.md's Cadence table.
   - Recovery (Oura): readiness/sleep score, HRV (with `hrv_baseline_30d` + `hrv_pct_of_baseline`), RHR, total sleep min, plus a derived `recovery_band` (`green` / `yellow` / `red`) that already applies the gating table below. Pulled from `metrics_daily_resolved` for today (preferred) or yesterday (fallback). Missing → fall through silently.
   - Last 14 days of Hevy sessions (date, weekday, duration, exercises)
   - Per-exercise last working set in the 365-day window (`last_performance`). Each entry includes `days_since_last` to make the within-pattern variety axis a one-line scan.

   Flags: `--date YYYY-MM-DD` to simulate a different day, `--json` for machine-readable output, `--lookback-sessions N` (default 14), `--lookback-perf N` (default 365), `--kb-root DIR`.
4. **The user's message itself** — they may include inline context: time available, energy, equipment ("pull-up station busy"), mood. Honor it.

```bash
python3 quantified-self-coach/workout-coach/scripts/build_context.py
```

## Composing the session

### Slot selection
GOALS.md's Cadence table decides; this file only says how to read it:
- A day mapped to one slot → that slot.
- Days sharing a slot ("Tue *or* Wed") → the slot if it is not logged yet this week; otherwise rest, unless the user says otherwise.
- A flex day → whichever slot is 4+ days stale. If all are recent, offer a lighter version or suggest rest.
- A day the table gives to someone else (a trainer day) → politely defer: "that's your trainer day; I don't plan those."
- If the user's message overrides ("I want lower today"), trust them.

### Exercise picks — 3★ / 2 unmarked split
For each session, target **4-6 movement-pattern slots** drawn from PROGRAM.md. Within those picks, aim for roughly **3 ★ (known) + 2 unmarked (new)** to balance progression against the variety the user wants. Patterns with zero Hevy history force an unmarked pick — those count toward the 2.

### Secondary emphasis from GOALS.md
If GOALS.md names a secondary emphasis (a set of movement qualities to favour), apply it only to the **unmarked** slot and only as a tie-breaker between otherwise-equal picks. The compound lifts still come first; the same tie-breaker pick is not repeated two sessions of the same slot in a row. If the section is absent, there is no bias.

### Variety across weeks (two axes)
Variety matters on **two independent axes** — apply both when planning the next session of the same slot (e.g. next Monday's UB):

1. **Exercise rotation within a pattern.** Don't repeat the exact same exercise 2 weeks in a row if an alternate in the same pattern is available. Look at the last 14 days in the context bundle.
2. **Pattern rotation across weeks.** Identify which UB patterns were *skipped* in the most recent UB session(s) — e.g. last Monday hit horizontal push, vertical push, vertical pull, laterals, biceps but skipped horizontal pull, rear delts, triceps, core. Next Monday should *prioritize* the skipped patterns so the month, taken as a whole, covers everything. Same logic for LB across Tue/Wed sessions.

Practical rule: before picking this session's 4-6 patterns, list the patterns hit in the last same-slot session. Prefer the *complement* set this time, while still respecting the 3★/2 unmarked split and gap-filling forcing functions.

### Sets, reps, weight
- **Rep range, RPE, working sets, rest and the progression rule** come from PROGRAM.md's Defaults table, including its compound-versus-isolation bias. The user's message can override any of them for today.
- **Units:** weights are in lb throughout. Hevy stores kg; the ingester converts at import, so the context bundle and the briefing agree. The plan JSON takes `weight_lb` or `weight_kg`.
- **Weight inference (date-based — judge against `last_date` in the context bundle):**
  - **≤30 days old:** use the weight. If `top_reps` was at the top of the rep range across all working sets, apply PROGRAM.md's progression rule.
  - **31-90 days old:** use the weight, hold (no progression). Flag in the rationale: `(_last: <date>, <W>×<R> — coming back after N weeks_)`.
  - **>90 days old, OR no entry:** treat as effectively NEW. Suggest a conservative start — the lightest load a trained adult would still call a working set for that movement — and call it "NEW" in the rationale. Don't anchor on a multi-month-old number — strength likely drifted.

The context bundle's `last_performance` covers up to 365 days back, so the coach sees stale entries — apply the staleness rules above; don't blindly trust everything in the list.

### Alternates
Provide **2-3 alternate options per exercise slot** drawn from the same movement pattern. Gym machines fill up; they need fallbacks.

**Seed a weight for the first alt** (the one mirrored into the Hevy routine). Apply the same `last_performance` staleness rules used for primaries:

- Find the alt's name in the context bundle's `last_performance` list. Match the Hevy-canonical name from PROGRAM.md exactly.
- **≤30 days old:** use the weight directly. Progression rule applies if top-of-range hit.
- **31-90 days old:** use the weight, no progression.
- **>90 days old, or never logged:** leave weight unset (bare-string form). Don't try to seed a conservative-NEW guess for alts — the user fills it in at the gym based on what equipment is in front of them. Blank is more honest than a fabricated number.

The 2nd and 3rd alts stay bare strings either way — they don't get mirrored into Hevy and the briefing only shows their names.

### Recovery gating (Oura readiness)
The context bundle's `RECOVERY (Oura)` block is the input. Apply it to *intensity*, not to whether to train — the slot decision still comes from the weekday and GOALS.md's Cadence table. If recovery is missing entirely, fall through silently and plan as usual.

The bundle pre-computes `recovery_band` (`green` / `yellow` / `red`) using the table below — `band=green` is shown in text mode, present as a JSON field. Trust it as a starting point; you may still escalate further on a conspicuously bad secondary signal not yet baked in.

| Readiness | Action | Surface line (always include when readiness is present) |
|-----------|--------|--------|
| **≥ 80**  | Push as planned. Apply the normal progression rule. | `_(Readiness 81, sleep 78 — green, normal progression.)_` |
| **65–79** | Cap target RPE at 7–8 (no top-end). Hold weights, no progression even if last session hit top-of-range. | `_(Readiness 72 — capping RPE 7–8, no progression today.)_` |
| **< 65**  | Deload: 2 working sets instead of 3, drop weight ~10%, lower-stimulus picks (e.g. machines over free weights). | `_(Readiness 58 — deload session: 2 working sets, ~10% lighter.)_` |
| **missing** | Silent fall-through. Plan normally. | (omit the line) |

Always surface a one-line readiness ack when Oura data is present, even on green — the user needs to see that the recovery loop is wired. Place it directly above the warmup line at the bottom of the briefing.

Use `readiness_score` as the primary gate. `sleep_score` and `hrv_avg` are secondary signals — only escalate beyond what readiness suggests if they're conspicuously bad (e.g. readiness 78 but sleep 50 → treat as 65–79 band, not ≥80). When you escalate based on a secondary signal, mention which signal forced the call (e.g. `_(Readiness 78 but sleep 50 — capping RPE 7–8, no progression today.)_`).

### Warmup
1-line prescription: 5 min bike or rower + 1-2 light sets on the first compound. Don't belabor it.

## Response format (Discord markdown)

Use this shape exactly — bolds, italics, bullet chars, indentation all matter.

```
_Monday 4/27 — UPPER BODY, ~45 min_

1. _HORIZONTAL PUSH -_ **Bench Press (Barbell)**
  • 3 × 8–10 @ 125 lb (_last: 3/23, 125×9/8 + 115×8 — hold, re-establish_)
    ◦ _Alts:_ **Incline DB Press** (NEW, upper chest) · **Machine Chest Press**

2. _VERTICAL PUSH -_ **Seated Shoulder Press (DB)**
  • 3 × 10–12 @ 30 lb (_NEW — fills vertical-push gap, start conservative_)
    ◦ _Alts:_ **Machine Shoulder Press** · **Arnold Press (DB)**

3. _VERTICAL PULL -_ **Pull-Up (Assisted)**
  • 3 × 8–12 @ 60 lb assist (_last: 3/30, 60×12/10/9_)
    ◦ _Alts:_ **Lat Pulldown (Cable)** · **Single-Arm Pulldown**

4. _LATERAL DELTS -_ **Lateral Raise (DB)**
  • 3 × 12–15 @ 12.5 lb (_NEW — fills lateral-delt gap, form over weight_)
    ◦ _Alts:_ **Cable Lateral Raise** · **Machine Lateral Raise**

5. _BICEPS -_ **Bicep Curl (Cable)**
  • 3 × 10–12 @ 40 lb (_last: 4/20, 40×14/12 + 34×12 — hold_)
    ◦ _Alts:_ **Hammer Curl (DB)** · **Seated Incline Curl (DB)**

_(Readiness 81, sleep 78 — green, normal progression.)_
_Warmup: 5 min bike + 2 light bench sets (45, 95). Working sets RPE 7–8._
```

Notes on format:
- **Bold = `**double asterisks**`**, italic = `_underscores_`. This CommonMark renders natively in Discord.
- Header line italic: `_Monday M/D — SLOT, ~N min_`. No separate context sentence — keep it minimal.
- Numbered exercise: `N. _PATTERN NAME -_ **Exercise Name**` — pattern uppercase italic, exercise bold.
- Main bullet `•` (2-space indent) with sets/reps line: `  • 3 × 8–10 @ 125 lb (_rationale in italic_)`.
- Sub-bullet `◦` (4-space indent) with alts: `    ◦ _Alts:_ **Alt 1** · **Alt 2**` — separator is `·` (middle dot, U+00B7).
- `(NEW, <short descriptor>)` on unmarked alt picks. For a NEW primary exercise, the rationale goes in the italic parenthetical on the bullet line: `(_NEW — fills vertical-push gap, start conservative_)`.
- Rationale forms: `(_last: date, WxR/WxR — hold_)` / `(_last: date — +5 from last_)` / `(_coming back cold after N weeks_)` / `(_NEW — <why>_)`.
- Warmup closing line in italic: `_Warmup: ... Working sets RPE 7–8._`.
- Keep it tight. No pre-amble paragraph, no post-script unless explicitly asked for follow-up.

## GOALS.md placeholders

Three `<EDIT:>` placeholders may still be unfilled: **injuries**, **love/hate exercises**, **current trainer focus**. If they are:
- **Proceed with safe assumptions:** assume no injuries, no strong exercise preferences beyond the pool, balanced trainer full-body.
- **Append a one-line heads-up** after the Warmup line, e.g.:
  ```
  _(Heads-up: GOALS.md still has placeholders for injuries/love-hate/trainer focus — filling those in will sharpen picks.)_
  ```
- Don't block the proposal waiting on edits.

## Do nots

- **Stay inside GOALS.md's "Out of scope" list.** The shipped template excludes cardio, nutrition and trainer days; whatever the user's copy says is the rule.
- **Do not plan days the Cadence table gives to someone else** (a trainer day).
- **Do not push toward higher frequency.** GOALS.md states the weekly cadence; older Hevy data showing more sessions per week is prior cadence, not a target.
- **Do not nag about missed days.** If they skipped a week, just propose today's session.
- **No slash commands.** All triggers are natural language in the channel.

## Surface notes

- **Surface: the Discord channel in `discord.channels.workouts`.** Two paths land here:
  - **Interactive path** — the user asks "what should I do today?" etc.; compose the briefing per this skill and reply in-channel.
  - **Proactive path** — a scheduled job for each solo slot in GOALS.md's Cadence table, at whatever hour you leave for the gym, runs the same composition unprompted and posts it via `lib/discord.py` (`send_message` handles the 2000-character split and embed suppression). The job's prompt is self-contained: build context, read GOALS.md and PROGRAM.md, compose, post, then push.
- The briefing's `**double asterisks**` bold + `_italic_` render natively in Discord; no conversion step.

## Sync to Hevy on the phone

After composing the briefing, **mirror the same plan onto the user's phone** as a Hevy routine they can open and start. **Two slot-specific rolling routines** — UB pushes overwrite `Today's Workout — Upper Body` (ID cached at `<data_root>/workout-coach/hevy-cache/routine_ub.json`); LB pushes overwrite `Today's Workout — Lower Body` (`routine_lb.json`). An upper-body push never clobbers the lower-body plan sitting on the phone, and vice versa. Two routines in the list, no more.

**Set the `slot` field in every plan** (`"slot": "UB"` or `"slot": "LB"`) so the push targets the right routine. If omitted, the script sniffs the plan `title` for UB/LB/upper/lower as a fallback — but be explicit. A plan with neither a `slot` nor a slot hint in the title is rejected.

**Critical delivery rule — turn boundaries.** The briefing must be its own end-turn message, with *no* tool calls in the same turn. Bundling the briefing text with the Hevy-push Bash call has been observed to drop the briefing from delivery (the harness only flushed text from the final `end_turn` turn). Required ordering:

1. **Turn A:** Emit the briefing as a text-only response. End the turn (no tool calls).
2. **Turn B (next turn):** Run the `push_to_hevy_routine.py` Bash call.
3. **Turn C:** Emit the `_(Synced to Hevy → open "Today's Workout — Upper Body" routine.)_` follow-up (use the slot-matching routine name).

Do not combine the briefing text with the push tool call in a single turn, even if it feels efficient. The split is load-bearing for delivery.

Build a plan JSON and pipe it to the push script:

```bash
cat <<'EOF' | python3 quantified-self-coach/workout-coach/scripts/push_to_hevy_routine.py
{
  "title": "Today's Workout — Mon 4/27 UB",
  "slot": "UB",
  "notes": "Warmup: 5 min bike + 2 light bench sets. Working sets RPE 7–8.",
  "exercises": [
    {"name": "Bench Press (Barbell)", "sets": 3, "reps": "8-10", "weight_lb": 125,
     "notes": "last: 3/23, 125×9/8 + 115×8 — hold.",
     "alts": [{"name": "Incline Bench Press (Dumbbell)", "weight_lb": 50}, "Chest Press (Machine)"]},
    {"name": "Shoulder Press (Dumbbell)", "sets": 3, "reps": "10-12", "weight_lb": 30,
     "notes": "NEW — fills vertical-push gap.",
     "alts": ["Seated Shoulder Press (Machine)", "Arnold Press (Dumbbell)"]}
  ]
}
EOF
```

(Note the asymmetry: first alt uses dict form so its weight rides into Hevy; later alts stay bare strings. The example above's second exercise has no first-alt history, so even the first alt is a bare string — that's fine.)

Plan field reference:
- `slot` — `"UB"` or `"LB"`. Selects which rolling routine to overwrite. Always set it. Falls back to sniffing `title` if omitted.
- `title` — defaults to `Today's Workout — <Day M/D>`. Override with the slot label (`UB`/`LB`).
- `notes` — top-level routine notes. Use for warmup line + GOALS placeholder heads-up.
- `exercises[].name` — must match a Hevy exercise template title exactly. PROGRAM.md is already in sync with Hevy's catalog, so just use the names from there. If a name doesn't resolve, the script auto-refreshes the cache once (picking up any custom exercises the user has added in Hevy); only after that miss does it abort with close-match suggestions.
- `exercises[].sets` — int, working sets only (no warmup sets in routine).
- `exercises[].reps` — `"8-10"`, `"8–10"`, `"8 to 10"`, or a single int. Translates to Hevy `rep_range` or `reps`.
- `exercises[].weight_lb` — float; converts to kg before push. Omit for bodyweight.
- `exercises[].notes` — per-exercise rationale; this is where the `(_last: …_)` / `(_NEW — …_)` justifications go. Carries through to the phone.
- `exercises[].rest_seconds` — defaults to 90.
- `exercises[].alts` — list of alt exercise names (Hevy-canonical). Each entry is either a bare string (`"Lat Pulldown (Cable)"`) or a dict (`{"name": "Lat Pulldown (Cable)", "weight_lb": 100}`). Always populate with the same 2-3 alts shown in the briefing — the **first** alt is mirrored into the routine as a follow-up exercise so the user can pick the primary or the alt at the gym; the rest stay in the briefing text only. The mirrored alt has matching sets/reps and a note flagging it as "ALT for X — do this OR X, not both." For the first alt, **prefer the dict form** and set `weight_lb` from the same per-exercise history lookup used for primaries (see the context bundle's `last_performance`) — this saves the user from having to remember alt weights at the gym. Fall back to bare string only when no history exists for the alt and no reasonable conservative default applies.

Script flags: `--plan-file PATH` (else stdin), `--dry-run` (resolve names, print the routine payload, no PUT — still needs the API key if the template cache is missing or a name misses), `--refresh` (re-fetch template + routine caches), `--bootstrap` (seed the caches and exit; no plan needed), `--cache-dir DIR` (default `<data_root>/workout-coach/hevy-cache`).

Call **after** posting the briefing reply (so the user sees the briefing immediately and the on-phone routine catches up a moment later). On success, append a brief footer to the reply or a follow-up message:

```
_(Synced to Hevy → open "Today's Workout — Upper Body" routine.)_
```
(Use the slot-matching name: `Today's Workout — Upper Body` for UB, `Today's Workout — Lower Body` for LB.)

If the push fails (HTTP error, unresolved name), don't retry silently — surface the error in a short follow-up so the user can fix the alias map or rename. Don't block the briefing on the push.

### When to skip the push

- the user's message is a question, not a session ask ("how did Monday go?"), and you don't actually compose a session.
- The proposal is for Friday (trainer day) — already excluded from the coach.
- You explicitly suggested rest.

## Post-session follow-up

The coach does **not** ask them to log anything manually. They log in Hevy; the next daily sync ingests it. If they reply with "I did X instead" or "bumped squat to 135", acknowledge briefly but don't mutate any files — the next Hevy sync will make it authoritative.

If the user adjusts the plan in chat ("swap squats for leg press, drop sets to 2"), re-run the push with the updated plan — the rolling routine PUT-overwrites cleanly.

## Quick reference

```bash
# Build context (today)
python3 quantified-self-coach/workout-coach/scripts/build_context.py

# Simulate another day
python3 quantified-self-coach/workout-coach/scripts/build_context.py --date 2024-04-29

# Machine-readable
python3 quantified-self-coach/workout-coach/scripts/build_context.py --json

# Push today's plan to the slot's rolling routine
cat plan.json | python3 quantified-self-coach/workout-coach/scripts/push_to_hevy_routine.py

# One-time: bootstrap template + both slot routine caches (UB + LB); rerun with
# --refresh if Hevy adds new exercises
python3 quantified-self-coach/workout-coach/scripts/push_to_hevy_routine.py --bootstrap [--refresh]

# Validate without pushing
cat plan.json | python3 quantified-self-coach/workout-coach/scripts/push_to_hevy_routine.py --dry-run
```

Relevant paths:
```
quantified-self-coach/workout-coach/
  ├── SKILL.md                         (this file)
  ├── GOALS.example.md                 (template for the GOALS.md below)
  ├── PROGRAM.example.md               (template for the PROGRAM.md below)
  └── scripts/
      ├── build_context.py             (context bundler)
      └── push_to_hevy_routine.py      (mirror plan to phone)

<data_root>/workout-coach/
  ├── GOALS.md                         (goals, constraints, injuries)
  ├── PROGRAM.md                       (split, pools, rep-range defaults)
  └── hevy-cache/
      ├── templates.json               (name → exercise_template_id, Hevy's full catalog)
      ├── routine_ub.json              (UB rolling routine UUID)
      ├── routine_lb.json              (LB rolling routine UUID)
      └── aliases.json                 (PROGRAM.md → Hevy title overrides)

<data_root>/knowledge/hevy/<uuid>.md   (per-workout markdown, one per logged session)
<data_root>/knowledge/index.db         (FTS5 index + structured tables; shared across all sources)
```
