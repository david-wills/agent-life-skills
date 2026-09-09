---
name: morning-news
description: Daily 6:00 AM PT news digest. A fetcher pulls ~28 public RSS feeds, the cron agent classifies the last 24 hours against a personal interest profile, and a poster ships one Discord message per theme to #newsfeed with rate-limit backoff and resume. Use when tuning what the digest surfaces (edit the profile), changing feeds, debugging a failed or half-posted run, or adjusting the digest format.
---

# Morning news

## Why it exists

A daily brief across ~28 feeds, cut down to the couple of dozen stories worth
knowing about, grouped by theme, in the place the day already starts.

## Pieces

| Piece | Path |
| --- | --- |
| Feed list | `feeds.local.yaml` (gitignored) |
| Published feed template | `feeds.example.yaml` |
| Fetcher | `scripts/fetch_feeds.py` |
| Agent instructions | `prompt.md` |
| Personal profile | `profile.local.md` (gitignored) |
| Published template | `profile.example.md` |
| Poster | `scripts/post_digest.py` |
| Resume state | `_state/last_run.json` |
| Discord channel | `#newsfeed` — `discord.channels.newsfeed` in `config.json` |
| Cron | `morning-news-digest`, 6:00 AM PT |

Python: `~/VirtualEnvs/news_venv` — `requirements.txt` (`feedparser`,
`python-dateutil`, `pyyaml`). The stdlib is not enough.

## Flow

1. `fetch_feeds.py --hours 24` reads `feeds.local.yaml`, pulls every feed in parallel,
   dedupes by url and normalized title, and writes `/tmp/news_items.json`.
2. The cron agent reads `prompt.md`, loads `profile.local.md`, and classifies
   each item into one theme or drops it.
3. It writes the composed messages to `/tmp/digest_messages.json`.
4. `post_digest.py` ships them to `#newsfeed`.

## The engine/profile split

**The agent is the model.** There is no API key and no LLM call in any script —
the cron runs an agent turn, and that agent does the classifying. Scripts own
only the deterministic ends: fetching and posting.

**`prompt.md` holds no opinions.** Which themes exist, what belongs in each, how
many items each gets, the posting order, what to drop — all of it lives in
`profile.local.md`. That is what makes the rest of this skill publishable, and it
is the line to defend: an interest that leaks into `prompt.md` is one a public
clone inherits and cannot find to change.

- **Editing the profile changes what the digest contains. Editing the prompt
  changes how it is produced.** Reach for the profile first; it is almost always
  the right file.
- **A missing profile is a failure, not a quiet day.** Falling back to a built-in
  default would produce a digest that looks fine and silently ignores everything
  you asked for.
- **The feed list is data too, and it is personal data.** `feeds.local.yaml`
  carries the feeds and a `priority` order that decides which outlet wins a
  duplicate and the order links appear in a clustered bullet. An unranked source
  sorts last rather than crashing. It is gitignored for the same reason the
  profile is: which outlets you read reveals interests even when nothing states
  them — a local paper gives away your city, a community outlet your background.
  `feeds.example.yaml` ships the ~23 general-interest feeds in its place.
- **A missing feed list fails with the fix, not just the complaint.** A fresh
  clone has the example and no local copy, so the error names the `cp` that
  resolves it.
- **Only free public RSS.** Nothing here authenticates to a publisher. Feeds that
  need a subscription do not belong in `feeds.local.yaml`.

## Invariants

- **Every message is validated before any is sent.** `post_digest.py` refuses the
  whole batch if one message exceeds Discord's 2000 characters. Validating as it
  went — the old behavior — meant an over-long section 18 aborted a run that had
  already posted 17, leaving a truncated digest and no clean way to finish.
- **A partial run resumes, it never restarts.** Progress lands in
  `_state/last_run.json` after each successful post; `--resume` continues from
  there. Re-running without it duplicates every section already in the channel.
  Resume is keyed on channel *and* message count, so resuming a different digest
  cannot silently skip its first N sections.
- **Rate limiting is not optional.** ~20–25 messages per digest exceeds Discord's
  per-channel burst allowance. Sends are paced 1.2s apart and 429s are retried
  for exactly the `retry_after` Discord returns. Guessing the wait is what
  half-posts a digest.
- **Embeds are suppressed** (`flags: 4`). Twenty-five link previews bury the text.
- **One story appears once.** Cross-theme duplication is the failure the
  classification rules in the profile exist to prevent.
- **The prompt never hardcodes a channel id or an absolute home path.** Both are
  blocked by the pre-commit identifier lint, and both would follow a public
  clone home. Channel ids come from `config.json`; paths are written `~/…`.
- **Feed churn makes runs non-reproducible.** Two fetches seconds apart routinely
  differ by an item — outlets re-publish the same story under a second url. Do
  not chase a one-item delta between runs as if it were a bug.

## Operating it

```bash
cd <repo>/reading/morning-news

~/VirtualEnvs/news_venv/bin/python scripts/fetch_feeds.py --hours 24 --out /tmp/news_items.json
~/VirtualEnvs/news_venv/bin/python scripts/post_digest.py --messages /tmp/digest_messages.json --dry-run
~/VirtualEnvs/news_venv/bin/python scripts/post_digest.py --messages /tmp/digest_messages.json
~/VirtualEnvs/news_venv/bin/python scripts/post_digest.py --messages /tmp/digest_messages.json --resume
```

`--dry-run` on the poster validates lengths and reports where a resume would
start, without sending. It is the fast way to check a composed digest will fit.

To run the whole thing by hand, follow `prompt.md` — it is the same text the cron
dispatches.

## Publishing

On the `ALLOW` list in `scripts/export-public.sh`. What keeps it exportable:
`profile.local.md` and `feeds.local.yaml` are gitignored *and* excluded from the
export by the `*.local.*` rsync rule — gitignore alone would not have protected
them, because rsync does not read it. `prompt.md` carries no personal
identifiers and no script hardcodes a path or channel id. `profile.example.md`
and `feeds.example.yaml` ship in their place so a clone has the shape to fill in.

Re-run `./scripts/export-public.sh --dry-run` after changing anything here.
