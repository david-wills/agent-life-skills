---
name: morning-news
description: Daily news digest. A fetcher pulls a list of public RSS feeds, a scheduled agent classifies the last 24 hours against a personal interest profile, and a poster ships one Discord message per theme to #newsfeed with rate-limit backoff and resume. Use when tuning what the digest surfaces (edit the profile), changing feeds, debugging a failed or half-posted run, or adjusting the digest format.
---

# Morning news

## Why it exists

A daily brief across a couple of dozen feeds, cut down to the couple of dozen
stories worth knowing about, grouped by theme, in the place the day already
starts.

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
| Resume state | `<data_root>/morning-news/last_run.json` |
| Discord channel | `#newsfeed` — `discord.channels.newsfeed` in `config.json` |
| Schedule | run `prompt.md` once a day with whatever agent scheduler you use (the author uses a 6:00 AM cron) |

Python: `python3` with `requirements.txt` installed (`feedparser`,
`python-dateutil`, `pyyaml`). The stdlib is not enough; a venv is the sane place
for it. Nothing in this skill touches the network except the fetcher (feeds) and
the poster (Discord).

## Flow

1. `fetch_feeds.py --hours 24` reads `feeds.local.yaml`, pulls every feed in
   parallel, dedupes by url and normalized title, and writes `/tmp/news_items.json`.
2. The scheduled agent reads `prompt.md`, loads `profile.local.md`, and
   classifies each item into one theme or drops it.
3. It writes the composed messages to `/tmp/digest_messages.json`.
4. `post_digest.py` ships them to `#newsfeed`.

## The engine/profile split

**The agent is the model.** There is no API key and no LLM call in any script —
the scheduler runs an agent turn, and that agent does the classifying. Scripts
own only the deterministic ends: fetching and posting.

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
  `feeds.example.yaml` ships a general-interest list in its place.
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
  `<data_root>/morning-news/last_run.json` after each successful post; `--resume`
  continues from there. Re-running without it duplicates every section already
  in the channel. Resume is keyed on channel *and* message count, so resuming a
  different digest cannot silently skip its first N sections. `--no-state` posts
  without touching that record, for one-off notices to another channel.
- **Rate limiting is not optional.** ~20–25 messages per digest exceeds Discord's
  per-channel burst allowance. Sends are paced 1.2s apart and 429s are retried
  for exactly the `retry_after` Discord returns. Guessing the wait is what
  half-posts a digest.
- **Embeds are suppressed.** Twenty-five link previews bury the text.
- **One story appears once.** Cross-theme duplication is the failure the
  classification rules in the profile exist to prevent.
- **A hung feed is an error, never a hang.** Each request is bounded by
  `--timeout` and the whole batch by a deadline derived from it; feeds still
  outstanding at the deadline land in `errors` and the run carries on. A feed
  that returns nothing (HTTP error, unparseable body) is an error too, not a
  quiet zero.
- **Undated items are kept.** An item with no parseable date passes the recency
  window and is counted in `counts.undated_kept`. Dropping them would silently
  blank any feed that omits dates; the cost is that such a feed re-surfaces its
  backlog every run, and the count is how you notice and fix or remove it.
- **The prompt never hardcodes a channel id or an absolute home path.** Both
  would follow a public clone home. Channel ids come from `config.json`; paths
  in the prompt are relative to the skill root.
- **Feed churn makes runs non-reproducible.** Two fetches seconds apart routinely
  differ by an item — outlets re-publish the same story under a second url. Do
  not chase a one-item delta between runs as if it were a bug.

## Operating it

```bash
cd news-triage-assistant/morning-news
pip install -r requirements.txt                       # once, ideally in a venv

python3 scripts/fetch_feeds.py --hours 24 --out /tmp/news_items.json
python3 scripts/post_digest.py --messages /tmp/digest_messages.json --dry-run
python3 scripts/post_digest.py --messages /tmp/digest_messages.json
python3 scripts/post_digest.py --messages /tmp/digest_messages.json --resume
```

`fetch_feeds.py` flags: `--hours N` (recency window, default 24), `--out PATH`
(default stdout), `--feeds PATH` (default `feeds.local.yaml` next to this skill),
`--timeout S` (per-request seconds, default 20; also scales the batch deadline),
`--no-filter` (skip the recency window). Output is one JSON object with
`counts`, `errors` and `items`.

`post_digest.py` flags: `--messages PATH` (required; a JSON array of strings in
send order), `--channel ID` (default `discord.channels.newsfeed`), `--dry-run`,
`--resume`, `--no-state`. `--dry-run` validates lengths and reports where a
resume would start, without sending. It is the fast way to check a composed
digest will fit.

To run the whole thing by hand, follow `prompt.md` — it is the same text the
scheduler dispatches.
