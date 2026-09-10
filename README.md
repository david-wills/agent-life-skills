# agent-life-skills

Agent skills I run every day, organised by the workflow they belong to rather
than as a pile of scripts. Each folder is a system: a few skills that compose
around state which already exists (a Discord server, a Readwise account, a
calendar, a SQLite file), so any single piece is useful alone and losing one
does not break the others.

Everything here is a working copy of something in production, exported from a
private repo and scrubbed of identifiers, addresses and the odd private-system
vocabulary. Names, ids and paths are placeholders; the code is not.

## The packages

| Package | Skills | What it does |
| --- | --- | --- |
| [`news-triage-assistant`](news-triage-assistant/) | 4 | RSS → a classified morning digest → a Readwise Reader inbox summarised into Discord cards → what you read and highlighted, back out as JSON and a local full-text index. |
| [`quantified-self-coach`](quantified-self-coach/) | 5 | Oura, Hevy and Apple Health into one SQLite resolution view, and a coach that reads it to propose the next session and push it to your phone. |
| [`restaurant-assistant`](restaurant-assistant/) | 1 | Drop a restaurant in a channel; it is resolved and enriched. Midweek an agent finds free evenings and posts picks. A ✅ is the booking intent. |
| [`token-tracker`](token-tracker/) | 1 | Where the Claude subscription quota actually goes: per cron job, per channel, per model, at zero token cost. |
| [`video-tools`](video-tools/) | 1 | Name finished short-form videos from their on-screen title card with on-device OCR. |

Each package README explains the loop, the pieces, the ideas worth stealing,
how to run it, and its honest limits.

## How a skill is shaped

```
<package>/<skill>/
├── SKILL.md          the skill: frontmatter (name, description) + the instructions an agent follows
├── scripts/          plain Python the instructions call; stdlib where possible
├── *.example.*       any personal input (a feed list, a goals file) ships as an example
└── requirements.txt  only where the stdlib is not enough (three skills)
```

`SKILL.md` is the [agent skills](https://docs.anthropic.com/en/docs/agents-and-tools/agent-skills)
format: the same file works as a Claude Code slash command and as a skill in any
agent runtime that reads the format. The scripts do the deterministic work; the
model does the judgement the instructions describe, and nothing else.

Two runtimes appear in the docs:

- **Claude Code** for skills you invoke in a terminal (`/name-videos`).
- **OpenClaw**, an agent gateway that schedules cron sessions and binds chat
  channels to an agent. That is where the author runs the unattended skills.
  Nothing here requires it: every scheduled step is a plain command you can
  run from cron, launchd, or any other scheduler.

## Setup

1. `cp config.example.json config.json` and fill in the keys the packages you
   want use. Each package README lists them, and `python3 doctor.py` reports
   which are still missing. `config.json` is gitignored. Any value can also
   come from an env var, `SKILLS_<DOTTED_KEY_UPPER>`. A key still holding a
   placeholder from the example (`CHANNEL_ID`, `USER_ID`, `you@example.com`)
   counts as unset: it never reaches Discord as a channel called CHANNEL_ID.
2. Secrets never live in config. `lib/read_secret.py` resolves them at runtime
   from the environment, 1Password, the macOS keychain, or a local JSON file,
   in that order. The names each package needs are in its README
   (`DISCORD_BOT_TOKEN`, `READWISE_TOKEN`, `HEVY_API_KEY`, ...).
3. Everything a skill writes (databases, imported files, caches, browser
   profiles) lands under one data root: `paths.data_root` in config, default
   `_data/` at the repo root, gitignored. Relocating all state is one edit.
4. Skills reach the shared `lib/` by walking up the tree, so the layout here is
   the layout that runs: clone it, do not flatten it.
5. Per-skill Python dependencies are in each skill's `requirements.txt`; most
   have none. Python 3.11+.

`lib/` is small on purpose: config resolution, secret resolution, a Discord
transport, a Claude CLI wrapper, a SQLite state helper, and an HTML-to-text
converter. Nothing else is shared.

## Check the machine

```bash
python3 doctor.py                 # every package
python3 doctor.py video-tools     # just the ones you care about
```

One line per requirement: Python version, binaries, Python modules, config
keys, secrets, files. Config keys report `not set` or `still the placeholder`.
Secrets are checked for resolvability and reported by store (`via env`,
`via keychain`), never printed. `--json` for scripts, `--no-secrets` to skip the
stores, exit 1 while a selected package has a blocking line.

## Let a runtime find the skills

Claude Code, and any runtime that scans `<dir>/<skill>/SKILL.md`, expects
skills one level deep. This repo keeps them two levels deep so each package can
carry its own README. Bridge the two with symlinks:

```bash
python3 link_skills.py                                     # plan against ~/.claude/skills
python3 link_skills.py --apply
python3 link_skills.py --target /path/to/another/runtime/skills --apply
```

Each link points at the skill's real directory inside the clone. Scripts find
the shared `lib/` by walking up from their resolved path, so the tree must stay
intact: copy a skill out of it and it stops finding the helpers. Existing
entries at a skill's name are reported and left alone, never replaced. For
OpenClaw or any other runtime with its own skills directory, pass it as
`--target`.

## Five-minute tour

No accounts, no config, any OS. The video-tools loop on empty files, which
exercises everything except the OCR:

```bash
mkdir -p /tmp/drop && touch /tmp/drop/HOSTED_DENTIST_v2.mp4 /tmp/drop/WH_Coral_Reefs_SPOTLIGHT.mov "/tmp/drop/HOSTED Already Named.mp4"
python3 video-tools/name-videos/lib/scan.py /tmp/drop         # two to name, one skipped as finished
python3 video-tools/name-videos/lib/hint.py HOSTED_DENTIST_v2.mp4 WH_Coral_Reefs_SPOTLIGHT.mov
```

The agent would now read each file's title card and compose a plan. Write one
by hand:

```bash
cat > /tmp/plan.json <<'EOF'
[
  {"path": "/tmp/drop/HOSTED_DENTIST_v2.mp4", "new_name": "HOSTED Things Your Dentist Wishes You Knew", "confidence": "high", "reason": "title card"},
  {"path": "/tmp/drop/WH_Coral_Reefs_SPOTLIGHT.mov", "new_name": "SPOTLIGHT WH Coral Reefs - Great Barrier Reef", "confidence": "medium", "reason": "filename hint"}
]
EOF
python3 video-tools/name-videos/lib/rename.py /tmp/plan.json           # dry run: the table, nothing touched
python3 video-tools/name-videos/lib/rename.py /tmp/plan.json --apply   # renames, writes an undo log
python3 video-tools/name-videos/lib/undo.py _data/name-videos/logs/rename-*.json
```

On macOS with `ffmpeg` and the Vision frameworks installed (`doctor.py
video-tools` says), the OCR step runs against a real export and prints the
title bands it read, ranked by how long each stayed on screen:

```bash
python3 video-tools/name-videos/lib/probe.py --outdir /tmp/clips /path/to/an/export.mp4
```

A second door, morning-news, needs the network and one `pip install` but still
no accounts. It pulls the example feed list, dedupes across outlets, and shows
what the digest would post:

```bash
pip install -r news-triage-assistant/morning-news/requirements.txt
python3 news-triage-assistant/morning-news/scripts/fetch_feeds.py --feeds news-triage-assistant/morning-news/feeds.example.yaml --hours 48 --out /tmp/items.json
echo '["**Digest**", "section one", "section two"]' > /tmp/digest.json
SKILLS_DISCORD_CHANNELS_NEWSFEED=123 python3 news-triage-assistant/morning-news/scripts/post_digest.py --messages /tmp/digest.json --dry-run
```

## Tests

```bash
python3 -m unittest discover -s tests
```

Stdlib `unittest`, no network: Discord is replaced by an in-memory fake in
`tests/support.py`, and every script's `--help` is run in an empty home with
no config. pytest runs the same files if you prefer it. GitHub Actions runs
the suite on Ubuntu and macOS (`.github/workflows/ci.yml`).

## What is deliberately not here

The work-side skills (meeting synthesis, email ingest, Slack triage, a work
knowledge base) and anything that only makes sense against one employer's
systems. A ranked query tool over the full-text index the importers build is
also private for now; `sqlite3` on the file is the interface.

## License

MIT.
