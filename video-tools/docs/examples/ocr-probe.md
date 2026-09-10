# Example: probe, plan, rename

The whole naming loop on two synthetic clips, so the OCR step can be shown
without a real export. Each clip is twelve seconds: a two-line title card for
five seconds, then two captions of three and a half seconds each, drawn with
AppKit and encoded with ffmpeg. A third, empty file is already named and gets
skipped.

```bash
V=video-tools/name-videos/lib
python3 $V/scan.py <drop>
python3 $V/probe.py --outdir <scratch>/clips <drop>/HOSTED_DENTIST_v2.mp4 <drop>/WH_Coral_Reefs_SPOTLIGHT.mov
python3 $V/hint.py HOSTED_DENTIST_v2.mp4 WH_Coral_Reefs_SPOTLIGHT.mov
python3 $V/rename.py plan.json          # dry run
```

## scan

```text
NEEDS NAMING (2):
  HOSTED_DENTIST_v2.mp4   <- underscore
  WH_Coral_Reefs_SPOTLIGHT.mov   <- underscore

already named, skipped: 1
```

## probe

On-device OCR (Apple Vision) over the first twenty seconds at one frame per
second, plus face detection for the talking-head verdict. Bands are ranked by a
score that favours text seen early and kept on screen; the title card's two
lines come out on top and the captions below them, which is what the agent
needs to compose a name. Both clips took about 3 seconds of wall clock together on an
Apple-silicon Mac.

```json
[
 {
  "path": "<drop>/HOSTED_DENTIST_v2.mp4",
  "filename": "HOSTED_DENTIST_v2.mp4",
  "filename_hint": "Dentist",
  "frames_analyzed": 12,
  "title_bands": [
   {
    "text": "THINGS YOUR DENTIST",
    "first_seen": 0,
    "persist": 0.42,
    "early_persist": 0.5,
    "height": 0.046,
    "y": 0.73,
    "score": 2.29
   },
   {
    "text": "WISHES YOU KNEW",
    "first_seen": 0,
    "persist": 0.42,
    "early_persist": 0.5,
    "height": 0.039,
    "y": 0.68,
    "score": 2.23
   },
   {
    "text": "No. 5 Floss before you brush",
    "first_seen": 5,
    "persist": 0.33,
    "early_persist": 0.4,
    "height": 0.022,
    "y": 0.1,
    "score": 1.71
   },
   {
    "text": "No. 4 Change the brush every 3 months",
    "first_seen": 9,
    "persist": 0.25,
    "early_persist": 0.1,
    "height": 0.02,
    "y": 0.1,
    "score": 0.71
   }
  ],
  "talking_head": {
   "cover": 0.0,
   "verdict": "no"
  },
  "clip_frame": "<scratch>/clips/HOSTED_DENTIST_v2__clip.jpg"
 },
 {
  "path": "<drop>/WH_Coral_Reefs_SPOTLIGHT.mov",
  "filename": "WH_Coral_Reefs_SPOTLIGHT.mov",
  "filename_hint": "Coral Reefs",
  "frames_analyzed": 12,
  "title_bands": [
   {
    "text": "GLOW AT NIGHT",
    "first_seen": 0,
    "persist": 0.42,
    "early_persist": 0.5,
    "height": 0.046,
    "y": 0.67,
    "score": 2.28
   },
   {
    "text": "CORAL REEFS THAT",
    "first_seen": 0,
    "persist": 0.42,
    "early_persist": 0.5,
    "height": 0.037,
    "y": 0.73,
    "score": 2.22
   },
   {
    "text": "Great Barrier Reef, Australia",
    "first_seen": 5,
    "persist": 0.33,
    "early_persist": 0.4,
    "height": 0.023,
    "y": 0.1,
    "score": 1.72
   },
   {
    "text": "Bioluminescent coral, night dive",
    "first_seen": 9,
    "persist": 0.25,
    "early_persist": 0.1,
    "height": 0.023,
    "y": 0.1,
    "score": 0.74
   }
  ],
  "talking_head": {
   "cover": 0.0,
   "verdict": "no"
  },
  "clip_frame": "<scratch>/clips/WH_Coral_Reefs_SPOTLIGHT__clip.jpg"
 }
]
```

## hint

The filename's own contribution, for the case where OCR finds nothing.

```text
HOSTED_DENTIST_v2.mp4                                    -> Dentist
WH_Coral_Reefs_SPOTLIGHT.mov                             -> Coral Reefs
```

## plan and dry run

The agent writes the plan; `rename.py` checks it. The first name keeps the
`HOSTED` prefix from the filename; the second promotes the `SPOTLIGHT` tag to
the front, as the house convention wants. The dry run is the default; `--apply`
performs the renames and writes an undo log under `<data_root>/name-videos/logs/`.

```json
[
  {"path": "<drop>/HOSTED_DENTIST_v2.mp4", "new_name": "HOSTED Things Your Dentist Wishes You Knew - Floss Before You Brush", "confidence": "high", "reason": "two-line title band read at 0s; first clip caption"},
  {"path": "<drop>/WH_Coral_Reefs_SPOTLIGHT.mov", "new_name": "SPOTLIGHT WH Coral Reefs That Glow at Night - Great Barrier Reef", "confidence": "high", "reason": "title band read at 0s; SPOTLIGHT tag from filename; first clip caption"}
]
```

```text
[high  ] HOSTED_DENTIST_v2.mp4
      -> HOSTED Things Your Dentist Wishes You Knew - Floss Before You Brush.mp4
[high  ] WH_Coral_Reefs_SPOTLIGHT.mov
      -> SPOTLIGHT WH Coral Reefs That Glow at Night - Great Barrier Reef.mov

DRY RUN. 2 would be renamed, 0 skipped.
Re-run with --apply to execute.
```
