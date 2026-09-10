# Field notes: video-tools

The operational history behind `name-videos`: what the first cut said, what changed, and
why. Kept out of `SKILL.md` so the instructions stay short. Checked against the current
modules in `video-tools/name-videos/lib/`; where the originals were silent, so is this.

## How it runs

- Editors export into one drop folder on a shared, cloud-synced drive; cutdowns go into
  its `CUTDOWNS` subfolder. `scan.py` takes folders on the command line, or reads
  `paths.video_drop` from config and adds `CUTDOWNS` itself. A positional folder wins.
- Prefix rules. `CUTDOWNS` is always `SPOTLIGHT`, whatever the export said. Elsewhere,
  `HOSTED` when the probe returns `talking_head: yes`, otherwise no prefix. Series tags
  (`WH`, `NS`, `IIWW`...) survive only if the export already carried one; they are
  editorial and not on screen, so the agent never adds one.
- One batch: `scan.py --json` lists the to-do. `probe.py --outdir <scratch>/clips` decodes
  the first 20 s of each file once and emits `title_bands`, `talking_head`, `clip_frame`
  and `filename_hint`. The agent writes `plan.json`. `rename.py plan.json` prints the
  dry-run table; after a yes, `rename.py plan.json --apply` renames and writes
  `<data_root>/name-videos/logs/rename-<stamp>.json`. `undo.py <log>` replays that
  newest-first; `--dry-run` previews it.
- Timing: the drive streams each file, so 10-30 s per video, most of it transfer rather
  than OCR. Pass every path in one `probe.py` call, or use `--from-file`.
- Batch size: the working unit is a week of output, about 28 videos. Extracted text is
  roughly 200 tokens a video; a clip still adds about 600. A week costs cents.
- Already-named files never reach the to-do. `classify.py` decides: an underscore, a codec
  tag, a `v2`, a `(2)` marker, a five-plus-digit stamp, or an internal `WH014`-style code
  in the body means the name is still raw. `MEME` codenames are always finished.

## What broke, and what it taught

- Renaming onto an existing name. On a shared drive that silently replaces a colleague's
  file. `rename.py` skips any move whose target exists and reports it as `target exists`.
- Two plan items resolving to the same name. The per-move skip only catches the second
  after the first has applied, leaving a half-done batch. The plan is now checked whole
  before anything moves: any duplicate target (compared case-insensitively) and nothing is
  renamed, exit 1, collisions listed.
- Case-only renames. The drive is case-insensitive, so `top five.mp4` to `TOP Five.mp4`
  looked like "target exists" and was skipped, and a direct `os.rename` would be a no-op.
  `rename.py` recognises a same-file case change and goes through a `.case-tmp` name.
- Filesystem-hostile characters. Title cards say `$150K` and `...By AI?`; `rename.py`
  strips `$ ? / : * " < > |` and trailing dots and spaces. Apostrophes and commas stay.
- The watermark is the most persistent text in every video. `WATERMARK` in `probe.py`
  and the `SKIPLINE` pattern drop it, plus `source:` and `@handle` lines, before clustering.
- OCR jitters frame to frame. The same band returns with different characters, so exact
  counting under-ranks the real title. Lines cluster by `SequenceMatcher` ratio >= 0.75
  and the most common variant is the reported text.
- Lower-thirds outrank title bands if you only count frames: a name caption can persist as
  long as the title. Score weights early persistence 3x and text height (capped at 0.12)
  8x, so big early text wins. `first_seen` is kept so the lower-third still serves as the
  first-clip suffix. Wrong film title is worse than none; omit rather than guess.
- Cutdowns have no title card. They open mid-segment, so the top band is `Part 6. ...` and
  reads like a title. Inside `CUTDOWNS` the agent uses `filename_hint` instead.
- The hint leaves run-together words (`Fakevacationphotos`). No regex fixes that reliably;
  the agent splits them and fixes obvious export typos by hand.
- Talking-head false positives on montages. A close-up fills the frame; a montage loses the
  face between cuts. `yes` needs a face on >= 90% of frames at median area <= 15% of the
  frame; `maybe` is 75% / 20%; anything else is `no`.
- Export junk reaches the drive: `_v2`, `H264`, `FINAL`, `(2)`, `.p4` broken extensions,
  six-digit date stamps. `classify.py` treats each as a reason the file is unnamed and
  `hint.py` strips the same tokens; one list in `classify.PREFIXES` feeds both.
- Notes prepended to finished names: `(DO NOT RECYCLE)`, `(TOO LONG)`, `(OVER ...)`.
  `classify.py` strips them before judging, so an annotated finished name is not re-proposed.
- Memes. An opening that shows only `source: @handle` has no title to read; those get
  hand-written codenames, so the agent flags them rather than naming them.
- `undo.py` used to skip silently when a target had moved or the original name was taken.
  Each skip now prints its reason, and `--dry-run` shows the reverts first.

## Things tried and dropped

- Drop folders hardcoded in `scan.py`: a drive-root config key plus a fixed path under a
  shared-drive mount whose name embedded an account identifier, resolved at import time, so
  `--help` and explicit folders needed unrelated config. Replaced by `paths.video_drop`
  and command-line folders.
- Undo logs in a gitignored folder inside the skill directory. Now under
  `<data_root>/name-videos/logs`, with everything else the repo writes.
- Two ffmpeg passes per video (720 px OCR frames for 20 s, then 480 px stills for 6 s).
  Now one decode with two outputs.
- Module-scope `import Vision, Quartz`. Now lazy, so `--help` works off-Mac.
- `hint.py` carrying its own copy of the series-tag list. It imports `classify.PREFIXES`.
- Cloud OCR or a vision model reading frames: the originals never record trying one; the
  design was on-device from the first cut. What they do record is the price that keeps it
  so: a still costs about three times the extracted text, so the still is opt-in and only
  for the suffix.

## Decisions that look odd

- `lib/` not `scripts/`: `probe` imports `hint`, and `hint` and `scan` import `classify`.
  Splitting CLIs from library code would need a package layout or a `sys.path` dance.
- The model only sees text. Nothing leaves the machine and the token bill stays tiny; the
  agent composes from a few lines of OCR, and the still is an opt-in extra for the suffix.
- Dry run is the default. A rename on a shared drive is visible to everyone and breaks
  links; `--apply` is the exception that needs a human yes.
- The plan is JSON the agent writes, not an automatic rename. Composition (reassembling
  split lines, dropping `TRIGGER WARNING:`, picking the first clip, not inventing series
  tags) is judgement, and a plan file is the review artefact. `rename.py` validates and
  applies; it never composes.
- Title bands are ranked by time on screen. The title card stays up through most of the
  opening; captions and subtitles blink. Vision returns a per-line confidence and `probe.py`
  records it, but the ranking ignores it: persistence is the better signal.

## Numbers worth knowing

| Constant | Value | Where |
| --- | --- | --- |
| Seconds probed for OCR | 20 (`SECS`) | `lib/probe.py` |
| Frame interval | 1 fps (`FPS`) | `lib/probe.py` |
| OCR frame width / clip still width | 720 px / 480 px | `lib/probe.py` |
| Clip stills taken; which is kept | 6 s (`CLIP_SECS`); the third, ~3 s in | `lib/probe.py` |
| "Early" window for `early_persist` | first 10 frames (`EARLY`) | `lib/probe.py` |
| Cluster match threshold | `SequenceMatcher` ratio >= 0.75 | `lib/probe.py` |
| Shortest line clustered | 6 normalised characters | `lib/probe.py` |
| Band score | `early_persist*3 + persist*1 + min(height, 0.12)*8` | `lib/probe.py` |
| Bands returned per video | top 8 | `lib/probe.py` |
| Talking head `yes` / `maybe` | cover >= 0.90 & median area <= 0.15 / >= 0.75 & <= 0.20 | `lib/probe.py` |
| Raw-name tells | `\d{5,}` stamp, `[A-Z]{2,4}\d{2,}` code, `v\d+`, `(\d)`, `.p\d` | `lib/classify.py` |
| Minimum words for a finished name | 2, or 1 after a known prefix; all-caps under 4 words is a codename | `lib/classify.py` |
| Plan confidence levels | `high` / `low`, informational | `SKILL.md`, `lib/rename.py` |
| New-name length cap | none; only illegal characters and edge dots/spaces are removed | `lib/rename.py` |
| Cost per video | ~200 tokens text, ~600 more per still | `SKILL.md` |
