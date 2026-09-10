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
   want use. Each package README lists them. `config.json` is gitignored. Any
   value can also come from an env var, `SKILLS_<DOTTED_KEY_UPPER>`.
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

## Tests

```bash
python3 -m unittest discover -s tests
```

Stdlib `unittest`, no network: Discord is replaced by an in-memory fake in
`tests/support.py`. pytest runs the same files if you prefer it.

## What is deliberately not here

The work-side skills (meeting synthesis, email ingest, Slack triage, a work
knowledge base) and anything that only makes sense against one employer's
systems. A ranked query tool over the full-text index the importers build is
also private for now; `sqlite3` on the file is the interface.

## License

MIT.
