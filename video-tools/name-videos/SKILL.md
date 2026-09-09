---
name: name-videos
description: Auto-name finished short-form videos from their on-screen title card. Scans the configured drop folder and its CUTDOWNS subfolder for files that still have raw export names (underscores, codec tags, version numbers), reads the title band off the first seconds of video with local OCR, and proposes "PREFIX Title - First Clip" filenames for review before renaming. Use when the user asks to name videos, rename reels, clean up export filenames, or says "/name-videos".
---

# Naming finished reels

Turns raw export names (`HOSTED_KARAOKE.mp4`, `WH_Biosphere_SPOTLIGHT.mov`) into the
house convention (`HOSTED Famous Song Lyrics You Definitely Get Wrong.mp4`).

**All video analysis is local** — ffmpeg + Apple's Vision framework do the frame
extraction, OCR and face detection on-device. Nothing is uploaded. The only thing that
reaches the model is a few lines of extracted text (and optionally one small still per
video for identifying the first clip).

## Drop folders

| Folder | Prefix rule |
|---|---|
| `<drive root>/<drop folder>` (`BASE` in `lib/scan.py`) | `HOSTED` if talking-head, else no prefix |
| `<drop folder>/CUTDOWNS` | **always** `SPOTLIGHT` |

`lib/scan.py` knows both paths; run it with no arguments to use them.

## Workflow

### 1. Scan
```bash
python3 ~/.claude/skills/name-videos/lib/scan.py
```
Lists files that still need naming and how many were skipped as already-named.
Never propose a name for a file the scanner classified as already named.

### 2. Probe
```bash
python3 ~/.claude/skills/name-videos/lib/probe.py --outdir <scratch>/clips "<file>" ["<file>" ...]
```
Per video this returns:
- `title_bands` — OCR text clustered across frames, ranked. **`early_persist` is the
  signal that matters**: text on screen through most of the opening is the title card;
  text appearing in one or two frames is a caption or subtitle.
- `talking_head` — `yes` / `maybe` / `no` from face coverage and size stability.
- `clip_frame` — path to a 480px still from ~3s in, for identifying the first clip.
- `filename_hint` — the existing filename with export junk stripped and title-cased.

Probing streams video from Google Drive, so expect ~10-30s per file. Batch them in one
call rather than one at a time.

### 3. Compose names

Format: `[PREFIX ]Title[ - First Clip]`

**Prefix**
- In `CUTDOWNS` → always `SPOTLIGHT`, whatever the old name said.
- Otherwise `HOSTED` when `talking_head` is `yes` (a single person addressing camera).
- Otherwise **no prefix.** Do not invent `WH` / `RW` / `NS` / `IIWW` / `NBK` — those are
  editorial series tags that can't be read off the video. If the old filename already
  carried one, keep it.

**Title** — prefer the persistent title band; fall back to `filename_hint`.

Cutdowns usually have **no title card** — they open mid-segment (`Part 6. They Took
Their Ball Games Seriously`), so a high-`early_persist` band there is a segment header,
not the video's title. For those use `filename_hint`: `WHR_Mayan_Life_SPOTLIGHT.mov`
-> `SPOTLIGHT Mayan Life`. Split any run-together words the hint leaves behind
(`Celebsfakesocialposts` -> `Celebs Fake Social Posts`) and fix obvious export typos.

When a title band *is* present, use it and Title Case it. Reassemble bands split across lines
(`FAMOUS SONG LYRICS` + `YOU DEFINITELY GET WRONG`). Drop the channel watermark (`WATERMARK` in `lib/probe.py`), a
leading `TRIGGER WARNING:`, and countdown counters (`10.`). Keep the wording as written;
don't editorialize.

**Suffix — first clip** (optional, best-effort). The subject of the opening clip, so the
prioritized movie/actor isn't lost: `- Weapons`, `- Adam Sandler Click`, `- Screech`.
`first_seen` on each band is its frame index at 1fps — i.e. roughly the second it
appears. **The suffix is the first clip, so pick the earliest-appearing name/brand,
not the most persistent one.**

Often OCR already supplies it free — on-screen lower-thirds name the actor
(`Kellan Lutz`) or the film card names the movie (`HEAVENLY CREATURES`). Check
`title_bands` first; only read `clip_frame` when the text doesn't settle it.
**Omit the suffix rather than guess** — a wrong film title is worse than none.

**Filesystem** — `rename.py` strips `$ ? / : * " < > |`. Keep apostrophes and commas.
`$150K` becomes `150K`, `...By AI?` becomes `...By AI`.

**Flag instead of naming** when the opening shows only `source: @handle` (a meme — those
use hand-written codenames), or when no title band is found and the clip frame doesn't
settle it. Report those to the user rather than inventing a title.

### 4. Review, then rename

Write a plan as JSON — `[{"path": ..., "new_name": ..., "confidence": "high|low", "reason": ...}]`
— then:
```bash
python3 ~/.claude/skills/name-videos/lib/rename.py plan.json          # dry run
python3 ~/.claude/skills/name-videos/lib/rename.py plan.json --apply  # execute
```
`new_name` carries no extension; the original one is preserved.

**Always show the dry-run table and get the user's confirmation before `--apply`.**
These files live on a shared team Drive — a rename is visible to everyone and breaks
links. `--apply` writes an undo log to `_state/logs/`; revert with
`python3 lib/undo.py _state/logs/rename-<stamp>.json`.

Renames are skipped automatically when the target name already exists.

## Cost

OCR and face detection are free. Reading the extracted text costs roughly 200 tokens per
video; adding the clip still for the suffix costs about 600 more. A 28-video week is a
few cents. If the user wants it cheaper, skip the clip frames and drop the suffix.
