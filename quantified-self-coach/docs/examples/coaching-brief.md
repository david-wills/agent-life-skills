# Example: one coaching brief, end to end

What the workout coach produces on a Monday, from a fixture index. Nothing here
is a real person's training log: the sessions are invented round numbers, the
Oura files are a synthetic month, and the goals and program are the shipped
`GOALS.example.md` and `PROGRAM.example.md`.

How it was made:

```bash
# fixture: four Hevy sessions in the fortnight before Mon 2025-09-08, one stale
# session months earlier, 31 Oura daily files -> <tmp>/knowledge and <tmp>/imports
python3 quantified-self-coach/structured-metrics/scripts/ingest_metrics.py --kb-root <tmp>/knowledge --imports-root <tmp>/imports --all
python3 quantified-self-coach/workout-coach/scripts/build_context.py --date 2025-09-08 --kb-root <tmp>/knowledge
```

## 1. The context bundle

What `build_context.py` prints. The coach reads this, `GOALS.md` and
`PROGRAM.md`, and nothing else. Note what is absent: no slot recommendation
(that is GOALS.md's Cadence table) and no exercise opinions (PROGRAM.md's
pools). Weights are in lb; the ingester converted them from Hevy's kg.

```text
=== WORKOUT COACH CONTEXT ===
Today: Monday 2025-09-08
Slot: from the Cadence table in GOALS.md (this bundle holds no cadence)

=== RECOVERY (Oura) ===
  Source: today | readiness 78 | sleep 74 | HRV 46 ms (30d base 50.1, 92% of base) | RHR 51 | sleep 400 min | band=yellow

=== LAST 14 DAYS (newest first) ===
  2025-09-03 Wed — 52min — Hack Squat (Machine) / Hip Thrust (Barbell) / Standing Calf Raise (Machine)
  2025-09-01 Mon — 61min — Bench Press (Barbell) / Seated Cable Row / Lateral Raise (Dumbbell) / Bicep Curl (Cable)
  2025-08-27 Wed — 55min — Squat (Barbell) / Romanian Deadlift (Barbell) / Seated Leg Curl (Machine)
  2025-08-25 Mon — 58min — Bench Press (Barbell) / Seated Cable Row / Pull Up (Assisted) / Bicep Curl (Cable)

=== EXERCISE LAST WORKING SET (365 day window) ===
  Bench Press (Barbell) — 2025-09-01 (Mon, 7d ago) — top set: 125 lb × 8  [3 working sets]
  Bicep Curl (Cable) — 2025-09-01 (Mon, 7d ago) — top set: 39.9 lb × 14  [3 working sets]
  Hack Squat (Machine) — 2025-09-03 (Wed, 5d ago) — top set: 199.96 lb × 10  [3 working sets]
  Hip Thrust (Barbell) — 2025-09-03 (Wed, 5d ago) — top set: 175.05 lb × 12  [2 working sets]
  Lateral Raise (Dumbbell) — 2025-09-01 (Mon, 7d ago) — top set: 12.57 lb × 15  [3 working sets]
  Pull Up (Assisted) — 2025-08-25 (Mon, 14d ago) — top set: 59.97 lb × 10  [2 working sets]
  Romanian Deadlift (Barbell) — 2025-08-27 (Wed, 12d ago) — top set: 175.05 lb × 10  [3 working sets]
  Seated Cable Row — 2025-09-01 (Mon, 7d ago) — top set: 130.07 lb × 10  [3 working sets]
  Seated Leg Curl (Machine) — 2025-08-27 (Wed, 12d ago) — top set: 89.95 lb × 12  [2 working sets]
  Shoulder Press (Dumbbell) — 2025-05-12 (Mon, 119d ago) — top set: 35.05 lb × 10  [2 working sets]
  Squat (Barbell) — 2025-08-27 (Wed, 12d ago) — top set: 154.98 lb × 8  [3 working sets]
  Standing Calf Raise (Machine) — 2025-09-03 (Wed, 5d ago) — top set: 100.09 lb × 15  [2 working sets]

=== END CONTEXT ===
```

## 2. The briefing

What an agent following `workout-coach/SKILL.md` posts to the workouts channel.
Readiness 78 is the yellow band, so RPE is capped and nothing progresses even
though the cable curl hit the top of its range. Last Monday covered horizontal
push, horizontal pull, lateral delts and biceps; this one takes the complement
(vertical push, vertical pull, rear delts) while keeping the 3★ / 2-unmarked
split from `PROGRAM.example.md`. The shoulder press is treated as NEW because
its last entry is 119 days old.

```text
_Monday 9/8 — UPPER BODY, ~45 min_

1. _HORIZONTAL PUSH -_ **Bench Press (Barbell)**
  • 3 × 8–10 @ 125 lb (_last: 9/1, 125×8/8/7 — hold, readiness cap_)
    ◦ _Alts:_ **Incline Bench Press (Dumbbell)** (NEW, upper chest) · **Chest Press (Machine)**

2. _VERTICAL PUSH -_ **Shoulder Press (Dumbbell)**
  • 3 × 10–12 @ 30 lb (_NEW — last logged 119 days ago, start conservative_)
    ◦ _Alts:_ **Seated Shoulder Press (Machine)** · **Arnold Press (Dumbbell)**

3. _VERTICAL PULL -_ **Pull Up (Assisted)**
  • 3 × 8–12 @ 60 lb assist (_last: 8/25, 60×10/9 — hold_)
    ◦ _Alts:_ **Lat Pulldown (Cable)** · **Single Arm Lat Pulldown (Cable)**

4. _REAR DELTS -_ **Face Pull (Cable)**
  • 3 × 12–15 @ 30 lb (_NEW — fills the rear-delt gap, form over weight_)
    ◦ _Alts:_ **Reverse Fly (Machine)**

5. _BICEPS -_ **Bicep Curl (Cable)**
  • 3 × 10–12 @ 40 lb (_last: 9/1, 40×14/12 + 34×12 — top of range, but no progression today_)
    ◦ _Alts:_ **Hammer Curl (Dumbbell)** · **Seated Incline Curl (Dumbbell)**

_(Readiness 78, sleep 74 — capping RPE 7–8, no progression today.)_
_Warmup: 5 min bike + 2 light bench sets (45, 95). Working sets RPE 7–8._
```

## 3. The plan the coach pipes to the phone

The same session as JSON on stdin to `push_to_hevy_routine.py`. `slot` picks
the rolling routine to overwrite; the first alt of the last exercise uses the
dict form so its weight rides into Hevy. The push itself is not shown here: it
needs a Hevy API key to resolve exercise names against the account's template
list.

```json
{
  "title": "Today's Workout — Mon 9/8 UB",
  "slot": "UB",
  "notes": "Warmup: 5 min bike + 2 light bench sets. Readiness 78: RPE capped 7–8, no progression.",
  "exercises": [
    {
      "name": "Bench Press (Barbell)",
      "sets": 3,
      "reps": "8-10",
      "weight_lb": 125,
      "notes": "last: 9/1, 125×8/8/7 — hold.",
      "alts": [
        "Incline Bench Press (Dumbbell)",
        "Chest Press (Machine)"
      ]
    },
    {
      "name": "Shoulder Press (Dumbbell)",
      "sets": 3,
      "reps": "10-12",
      "weight_lb": 30,
      "notes": "NEW — last logged 119 days ago, conservative start.",
      "alts": [
        "Seated Shoulder Press (Machine)",
        "Arnold Press (Dumbbell)"
      ]
    },
    {
      "name": "Pull Up (Assisted)",
      "sets": 3,
      "reps": "8-12",
      "weight_lb": 60,
      "notes": "last: 8/25, 60×10/9 — hold.",
      "alts": [
        "Lat Pulldown (Cable)",
        "Single Arm Lat Pulldown (Cable)"
      ]
    },
    {
      "name": "Face Pull (Cable)",
      "sets": 3,
      "reps": "12-15",
      "weight_lb": 30,
      "notes": "NEW — rear-delt gap.",
      "alts": [
        "Reverse Fly (Machine)"
      ]
    },
    {
      "name": "Bicep Curl (Cable)",
      "sets": 3,
      "reps": "10-12",
      "weight_lb": 40,
      "notes": "last: 9/1, 40×14/12 + 34×12 — top of range, hold (readiness).",
      "alts": [
        {
          "name": "Hammer Curl (Dumbbell)",
          "weight_lb": 25
        },
        "Seated Incline Curl (Dumbbell)"
      ]
    }
  ]
}
```

```bash
python3 quantified-self-coach/workout-coach/scripts/push_to_hevy_routine.py --dry-run < plan.json   # resolves names, prints the payload, no PUT
```
