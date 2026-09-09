# Video tools

One skill: name finished short-form videos from what is on the screen. Editors
export files called `HOSTED_KARAOKE.mp4` and `WH_Biosphere_SPOTLIGHT.mov`; the
team needs `HOSTED Famous Song Lyrics You Definitely Get Wrong.mp4`. The title
is right there in the first few seconds of video, so read it off.

Everything that looks at pixels runs on-device. ffmpeg pulls frames, Apple's
Vision framework does the OCR and face detection, and the only thing the model
ever sees is a few lines of extracted text and, optionally, one small still.

## The loop

```
a drop folder of exports
      │
      │  scan.py — which files still carry raw export names?
      ▼
   the to-do list                 (already-named files are never touched)
      │
      │  probe.py — ffmpeg frames at 1fps ▸ Vision OCR ▸ face coverage
      ▼
   per video:
     title_bands   OCR text clustered across frames, ranked by persistence
     talking_head  yes / maybe / no
     clip_frame    a 480px still from ~3s in
     filename_hint the old name with export junk stripped
      │
      │  the agent composes  [PREFIX ]Title[ - First Clip]
      ▼
   plan.json ─── rename.py (dry run) ─── you confirm ─── rename.py --apply
                                                              │
                                                     undo log written;
                                                     undo.py reverts it
```

## The pieces

| Script | Does |
| --- | --- |
| `scan.py` | Lists files in the drop folders that still need naming, and how many were skipped as already named. |
| `classify.py` | Decides whether a filename already follows the house convention. |
| `probe.py` | Frame extraction, OCR, title-band clustering, talking-head detection, a still for the first clip. Batch several files per call. |
| `hint.py` | Turns `WHR_Mayan_Life_SPOTLIGHT.mov` into `Mayan Life`. |
| `rename.py` | Applies a JSON plan. Dry run by default, `--apply` to execute, strips filesystem-hostile characters, never overwrites. |
| `undo.py` | Replays an undo log backwards. |

## Ideas worth stealing

**Persistence is the signal.** Text that stays on screen through most of the
opening is the title card. Text in one or two frames is a caption or a
subtitle. `early_persist` ranks the bands; the agent reads the top one, not the
longest.

**The suffix is the *first* clip, not the most persistent one.** Lower-thirds
and film cards name the subject of the opening clip for free. Pick the
earliest-appearing name or brand. When the text does not settle it, look at the
still. When the still does not either, omit the suffix. A wrong film title is
worse than none.

**Do not invent what cannot be read.** Series tags (`WH`, `NS`, `IIWW`) are
editorial and are not on the video. Keep one if the old filename carried it;
never add one. Talking-head detection sets the `HOSTED` prefix because that
*is* visible.

**Flag instead of naming.** An opening that shows only `source: @handle` is a
meme, and memes get hand-written codenames. Report it and move on.

**Renames on a shared drive are visible to everyone and break links.** So the
plan is always shown as a dry-run table first, the apply step needs an explicit
confirmation, and every apply writes an undo log.

## Running it

- macOS only. `brew install ffmpeg`, then the pyobjc frameworks in
  `requirements.txt`.
- Point `scan.py` at your drop folders: the two paths at the top of the file,
  or pass folders on the command line. The `CUTDOWNS` subfolder rule (always
  `SPOTLIGHT`) is a house convention; change it or delete it.
- Probing a file on a cloud-synced drive streams it, so expect 10-30 seconds
  per video. Pass several paths in one call.

## Honest limits

- **Tuned to one naming convention.** `classify.py` and the prefix rules encode
  a specific house style. The OCR and face-detection plumbing is general; the
  composition rules are not.
- **Cutdowns rarely have a title card.** They open mid-segment, so the most
  persistent band is a segment header. The skill falls back to the filename
  hint for those, which is only as good as the export name.
- **The model still does the composing.** OCR is free; reading its output
  costs a couple of hundred tokens per video, a few cents for a week's batch.
