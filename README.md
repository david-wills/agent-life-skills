# agent-life-skills

Agent skills I run every day, organised by the workflow they belong to rather
than as a pile of scripts. Each folder is a system: a few skills that compose
around state which already exists (a Discord server, a Readwise account, a
calendar, a SQLite file), so any single piece is useful alone and losing one
does not break the others.

Everything here is a working copy of something in production. It is exported
one-way from a private repo through an allowlist and a set of gates (secret
scan, identifier scan, employer-name scrub, import reachability), then
hand-reviewed. Names and addresses are placeholders; the code is not.

## The packages

| Package | Skills | What it does |
| --- | --- | --- |
| [`news-triage-assistant`](news-triage-assistant/) | 4 | RSS → a classified morning digest → a Readwise Reader inbox summarised into Discord cards → what you read and highlighted, back out as JSON. |
| [`quantified-self-coach`](quantified-self-coach/) | 5 | Oura, Hevy and Apple Health into one SQLite resolution view, and a coach that reads it to propose the next session and push it to your phone. |
| [`restaurant-assistant`](restaurant-assistant/) | 1 | Drop a restaurant in a channel; it is resolved and enriched. On Wednesday an agent finds free Thursdays and posts picks. A ✅ is the booking intent. |
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
└── launchd/          *.plist.template for anything scheduled outside the agent
```

`SKILL.md` is the [agent skills](https://docs.anthropic.com/en/docs/agents-and-tools/agent-skills) format:
the same file works as a Claude Code slash command and as an OpenClaw skill.
The scripts do the deterministic work; the model does the judgement the
instructions describe, and nothing else.

Two runtimes appear throughout:

- **Claude Code** for skills you invoke in a terminal (`/name-videos`).
- **OpenClaw**, an agent gateway that schedules cron sessions and binds chat
  channels to an agent. Most of the skills here run there, unattended.
  **MasterClaw** is the name of the agent that runs them.

## Setup

1. Copy `config.example.json` to `config.json` and fill in the keys the
   package you want uses. Each README lists them. Any value can also come from
   an env var, `SKILLS_<DOTTED_KEY_UPPER>`.
2. Secrets never live in config. `lib/read_secret.py` resolves them at runtime
   from a secret manager or the keychain.
3. Skills reach the shared `lib/` by walking up the tree, so the layout here is
   the layout that runs: clone it, do not flatten it.
4. Per-skill Python dependencies are in each skill's `requirements.txt`; most
   have none.

## What is deliberately not here

The work-side skills (meeting synthesis, email ingest, Slack triage, a work
knowledge base) and anything that only makes sense against one employer's
systems. Some of it may follow once it is generic enough to be worth reading.

## License

MIT.
