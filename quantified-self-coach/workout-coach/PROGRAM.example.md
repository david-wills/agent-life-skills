# Workout coach program — example

Copy to `<data_root>/workout-coach/PROGRAM.md` and rewrite for yourself.
`<data_root>` is `paths.data_root` in `config.json`, default `<repo>/_data`,
which is gitignored; this file is the published template.

This file is the exercise vocabulary. The coach picks 4-6 movement-pattern slots per
session and fills each from the pool below — it never invents an exercise that is not
here.

## Two conventions that are load-bearing

- **★ means "already in your logging-app history."** `SKILL.md`'s 3★ / 2-unmarked
  split reads these marks directly: three known lifts for progression, two unmarked
  ones for variety. Marking everything ★ removes the variety axis; marking nothing
  ★ removes progression.
- **Names must match your logging app's exercise titles exactly.** `push_to_hevy_routine.py`
  resolves each name against the cached template list and aborts with close-match
  suggestions on a miss. Where your preferred wording differs from the app's, put the
  app's title here and record your wording in `<data_root>/workout-coach/hevy-cache/aliases.json`.

## Defaults

| Setting | Value |
| --- | --- |
| Rep range | 8-15 — bias 8-10 on compounds, 12-15 on isolation |
| Working sets | 3 |
| RPE | 7-8 (2-3 reps in reserve) |
| Rest | 90s |
| Progression | +5 lb (or the next dumbbell) once every working set hits the top of the range |

## Upper body pools

| Pattern | Options |
| --- | --- |
| Horizontal push | ★ Bench Press (Barbell) · Incline Bench Press (Dumbbell) · Chest Press (Machine) |
| Vertical push | Shoulder Press (Dumbbell) · Seated Shoulder Press (Machine) · Arnold Press (Dumbbell) |
| Horizontal pull | ★ Seated Cable Row · Bent Over Row (Barbell) · Chest Supported Row (Machine) |
| Vertical pull | ★ Pull Up (Assisted) · Lat Pulldown (Cable) · Single Arm Lat Pulldown (Cable) |
| Lateral delts | Lateral Raise (Dumbbell) · Lateral Raise (Cable) · Lateral Raise (Machine) |
| Rear delts | Face Pull (Cable) · Reverse Fly (Machine) |
| Biceps | ★ Bicep Curl (Cable) · Hammer Curl (Dumbbell) · Seated Incline Curl (Dumbbell) |
| Triceps | Triceps Pushdown (Cable) · Skullcrusher (Barbell) · Triceps Extension (Dumbbell) |
| Core / carry | Cable Core Palloff Press · Single Arm Farmer's Walk · Farmers Walk |

## Lower body pools

| Pattern | Options |
| --- | --- |
| Knee-dominant | ★ Squat (Barbell) · Goblet Squat · Front Squat (Barbell) · Hack Squat (Machine) |
| Hip-dominant | ★ Romanian Deadlift (Barbell) · Deadlift (Barbell) · Hip Thrust (Barbell) |
| Unilateral | Bulgarian Split Squat · Walking Lunge (Dumbbell) · Step Up (Dumbbell) |
| Hamstring isolation | Seated Leg Curl (Machine) · Lying Leg Curl (Machine) |
| Quad isolation | Leg Extension (Machine) |
| Calves | Standing Calf Raise (Machine) · Seated Calf Raise (Machine) |
| Full-body finisher | Kettlebell Turkish Get Up |

## Patterns with no history

Vertical push, lateral delts, calves and quad isolation carry no ★ above. `SKILL.md`
treats a zero-history pattern as a forcing function: when one of these comes up in the
rotation, the unmarked pick is mandatory rather than optional. Re-mark them as ★ once
they appear in your logs and that pressure lifts on its own.
