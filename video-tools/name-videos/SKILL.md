---
name: name-videos
description: Auto-name finished short-form videos from their on-screen title card. Scans the configured drop folder and its CUTDOWNS subfolder for files that still have raw export names (underscores, codec tags, version numbers), reads the title band off the first seconds of video with local OCR, and proposes "PREFIX Title - First Clip" filenames for review before renaming. Use when the user asks to name videos, rename reels, clean up export filenames, or says "/name-videos".
---

# Naming finished reels

Turns raw export names (`HOSTED_DENTIST_v2.mp4`, `WH_Coral_Reefs_SPOTLIGHT.mov`) into the
house convention (`HOSTED Things Your Dentist Wishes You Knew.mp4`).

**All video analysis is local** — ffmpeg + Apple's Vision framework do the frame
extraction, OCR and face detection on-device. Nothing is uploaded. The only thing that
reaches the model is a few lines of extracted text (and optionally one small still per
video for identifying the first clip).

The CLIs live in `video-tools/name-videos/lib/` rather than `scripts/` because they also
import each other. Run every command below from the repo root.

## Drop folders

| Folder | Prefix rule |
|---|---|
| the drop folder | `HOSTED` if talking-head, else no prefix |
| `<drop folder>/CUTDOWNS` | **always** `SPOTLIGHT` |

Which folder: pass it to `scan.py` on the command line (no config needed), or set the
optional config key `paths.video_drop` in the repo-root `config.json` to its absolute
path and run `scan.py` bare — that scans the folder and its `CUTDOWNS` subfolder. A
positional folder always wins over the config key.

## Workflow

### 1. Scan
```bash
python3 video-tools/name-videos/lib/scan.py                      # paths.video_drop + CUTDOWNS
python3 video-tools/name-videos/lib/scan.py /path/to/drop        # explicit folder(s)
python3 video-tools/name-videos/lib/scan.py --json               # {"todo": [...], "skipped": [...]}
```
Lists files that still need naming and how many were skipped as already-named. Each
`todo` record carries `path`, `filename`, `folder`, `reason` and `forced_prefix`
(`SPOTLIGHT` inside `CUTDOWNS`, else null). Never propose a name for a file the scanner
classified as already named.

### 2. Probe
```bash
python3 video-tools/name-videos/lib/probe.py --outdir <scratch>/clips "<file>" ["<file>" ...]
python3 video-tools/name-videos/lib/probe.py --outdir <scratch>/clips --from-file paths.txt
```
`--outdir` (required) is where the first-clip stills go; `--from-file` reads one path per
line and adds them to the positionals. Exits with one line if `ffmpeg` is not on PATH or
the pyobjc frameworks are missing. Per video this returns:
- `title_bands` — OCR text clustered across frames, ranked. **`early_persist` is the
  signal that matters**: text on screen through most of the opening is the title card;
  text appearing in one or two frames is a caption or subtitle.
- `talking_head` — `yes` / `maybe` / `no` from face coverage and size stability.
- `clip_frame` — path to a 480px still from ~3s in, for identifying the first clip.
- `filename_hint` — the existing filename with export junk stripped and title-cased.

Probing streams video from a cloud-synced drive, so expect ~10-30s per file. Batch them
in one call rather than one at a time; each file is decoded once.

To check what the hint alone would give, `hint.py` takes filenames as arguments or one
per line on stdin:
```bash
python3 video-tools/name-videos/lib/hint.py WH_Coral_Reefs_SPOTLIGHT.mov
ls /path/to/drop | python3 video-tools/name-videos/lib/hint.py
```

### 3. Compose names

Format: `[PREFIX ]Title[ - First Clip]`

**Prefix**
- In `CUTDOWNS` → always `SPOTLIGHT`, whatever the old name said.
- Otherwise `HOSTED` when `talking_head` is `yes` (a single person addressing camera).
- Otherwise **no prefix.** Do not invent `WH` / `RW` / `NS` / `IIWW` / `NBK` — those are
  editorial series tags that can't be read off the video. If the old filename already
  carried one, keep it. The list lives in `lib/classify.py` (`PREFIXES`) and is a house
  convention; edit or empty it for another.

**Title** — prefer the persistent title band; fall back to `filename_hint`.

Cutdowns usually have **no title card** — they open mid-segment (`Part 6. The Reef
Fights Back`), so a high-`early_persist` band there is a segment header, not the video's
title. For those use `filename_hint`: `WH_Coral_Reefs_SPOTLIGHT.mov` -> `SPOTLIGHT Coral
Reefs`. Split any run-together words the hint leaves behind (`Fakevacationphotos` ->
`Fake Vacation Photos`) and fix obvious export typos.

When a title band *is* present, use it and Title Case it. Reassemble bands split across
lines (`THINGS YOUR DENTIST` + `WISHES YOU KNEW`). Drop the channel watermark
(`WATERMARK` in `lib/probe.py`), a leading `TRIGGER WARNING:`, and countdown counters
(`10.`). Keep the wording as written; don't editorialize.

**Suffix — first clip** (optional, best-effort). The subject of the opening clip, so the
featured film or person isn't lost: `- The Lighthouse Keeper`, `- Dana Reyes Spring Tide`,
`- Officer Pug`. `first_seen` on each band is its frame index at 1fps — i.e. roughly the
second it appears. **The suffix is the first clip, so pick the earliest-appearing
name/brand, not the most persistent one.**

Often OCR already supplies it free — on-screen lower-thirds name the person
(`Dana Reyes`) or the film card names the movie (`SPRING TIDE`). Check `title_bands`
first; only read `clip_frame` when the text doesn't settle it. **Omit the suffix rather
than guess** — a wrong film title is worse than none.

**Filesystem** — `rename.py` strips `$ ? / : * " < > |`. Keep apostrophes and commas.
`$150K` becomes `150K`, `...By AI?` becomes `...By AI`.

**Flag instead of naming** when the opening shows only `source: @handle` (a meme — those
use hand-written codenames), or when no title band is found and the clip frame doesn't
settle it. Report those to the user rather than inventing a title.

### 4. Review, then rename

Write a plan as JSON — `[{"path": ..., "new_name": ..., "confidence": "high|low", "reason": ...}]`
— then:
```bash
python3 video-tools/name-videos/lib/rename.py plan.json          # dry run
python3 video-tools/name-videos/lib/rename.py plan.json --apply  # execute
```
`new_name` carries no extension; the original one is preserved.

**Always show the dry-run table and get the user's confirmation before `--apply`.**
These files live on a shared team drive — a rename is visible to everyone and breaks
links. `--apply` writes an undo log to `<data_root>/name-videos/logs/`; revert with:
```bash
python3 video-tools/name-videos/lib/undo.py <data_root>/name-videos/logs/rename-<stamp>.json --dry-run
python3 video-tools/name-videos/lib/undo.py <data_root>/name-videos/logs/rename-<stamp>.json
```
`undo.py` replays the log newest-first and only reverts a move whose target still exists
and whose original name is free; the rest are reported and left alone.

Renames are skipped automatically when the target name already exists. A plan in which
two items resolve to the same name (compared case-insensitively, since the shared drive
is) is **refused as a whole** with the collisions listed — fix the plan and re-run. A
rename that only changes letter case is allowed; `rename.py` goes through a temporary
name so it works on a case-insensitive volume.

## Cost

OCR and face detection are free. Reading the extracted text costs roughly 200 tokens per
video; adding the clip still for the suffix costs about 600 more. A 28-video week is a
few cents. If the user wants it cheaper, skip the clip frames and drop the suffix.
