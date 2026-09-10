# News triage assistant

Four skills that make a closed loop out of reading on the internet: surface what
is worth reading, make it small enough to actually read, and keep what you read.

Each one runs on its own schedule and none of them import each other. What joins
them is state that already exists — a Readwise Reader account and a Discord
server — so any single stage is useful on its own and losing one does not break
the others.

## The loop

```
your RSS feed list
     │
     │  morning-news — daily
     ▼
Discord #newsfeed ──── you save the interesting ones ────┐
                                                          ▼
                                             Readwise Reader inbox
                                                          │
                                           reading-list — twice a day
                                                          ▼
                                          Discord #reading-list
                                       one card per article, summarized
                                                          │
                                    ✅ archive  📌 later  🗑️ delete
                                          swept nightly back into Reader
                                                          │
                                                          ▼
                            import-reader-archive · import-readwise-highlights
                                    what you read, and what you marked in it,
                                  back out as JSON, then into a local FTS index
```

## The pieces

| Skill | Runs | Does |
| --- | --- | --- |
| [`morning-news`](morning-news/) | Daily (the author: 6:00 AM) | Pulls your RSS feed list, classifies the last 24 hours against a personal interest profile, posts one message per theme. |
| [`reading-list`](reading-list/) | Twice daily, plus a nightly sweep | Summarizes everything newly saved to the Reader inbox into three paragraphs, posts one card per article with reaction affordances. The sweep turns those reactions into real Reader actions. |
| [`import-reader-archive`](import-reader-archive/) | On demand, or a daily delta | Dumps every document you have finished — `location=archive` — to JSON, and ingests it into the local full-text index. |
| [`import-readwise-highlights`](import-readwise-highlights/) | On demand, or a daily delta | Dumps the highlights you made, raw JSON plus a markdown digest grouped by book, and ingests them into the same index. |

## Ideas worth stealing

**The agent is the model; the scripts are the deterministic ends.** `morning-news`
contains no API key and no LLM call. A scheduler runs an agent turn, and that
agent does the classifying — the scripts only fetch and post. This is why the
whole thing is publishable: there is no prompt-plus-key blob at the center to
scrub.

**Separate the engine from the opinion.** `prompt.md` says how a digest is
produced. `profile.local.md` says what belongs in it: the themes, the item
counts, the posting order, what to drop. `feeds.local.yaml` says where it can
come from, and in what priority order — which outlet wins when two carry the same
story. Both are gitignored, and `profile.example.md` and `feeds.example.yaml`
ship in their place. The line to defend is that an interest which leaks into the
prompt is one a clone inherits and cannot find to change. Editing the profile or
the feed list changes what you get; editing the prompt changes how it is made.
Reach for the profile first — it is almost always the right file.

**A feed list is personal data.** It is tempting to treat it as configuration, but
which outlets someone reads is a fingerprint. The example list carries
general-interest feeds only; whatever else you follow stays in the gitignored
file.

**A missing profile is a failure, not a quiet day.** Falling back to a built-in
default would produce a digest that looks fine and silently ignores everything
you asked for.

**The middle step is the whole point.** Saving an article to a read-later app is
where articles go to die. `reading-list` adds a digestible version delivered
where you already are, so the queue gets processed instead of accumulating.
Summaries are deliberately three paragraphs of *what the piece says* — no "why
this matters to you" section. You asked for the article; you don't need the pitch.

**Fetch through the authenticated reader, not the open web.** Reader's API
returns `html_content` for documents it already fetched as you, so subscription
articles come back in full. No scraping and no paywall fight, because the fetch
already happened under your own session.

**Backfill from a floor, never a cap.** A run that finds nothing new used to post
nothing, so a queue years deep only ever grew. Now any run with fewer than three
new saves tops itself up from the back catalog — but a run with twelve new saves
posts all twelve and backfills nothing. One table of processed document ids is
the only record of what has been done; selection is a live inbox scan minus that
table, so there is no cursor to disagree with reality.

**A dud backfill pick is not an error.** An old save whose text was never
retrievable is expected. Those are reported separately from genuine failures,
because a scheduler that pages on the expected case gets muted, and then it does
not page on the real one either.

**Only the owner's reactions count.** The sweep lists who reacted with each
emoji and acts only on `discord.user_id`. The bot's own seeds and other members'
clicks are ignored, so the bot can live in a shared server without a stranger's
🗑️ deleting your article.

**An unreadable card is an error, not an empty one.** When the sweep cannot read
a card's reactions, it records that against the card rather than treating it as
"no reaction". Otherwise an expired bot token would quietly age out every pending
article.

**Validate the whole batch before sending any of it.** `morning-news` refuses to
post if one message exceeds Discord's 2000-character limit. Validating as it went
meant an over-long section 18 aborted a run that had already posted 17, leaving a
truncated digest and no clean way to finish. For the same reason a partial run
resumes rather than restarts, keyed on channel *and* message count so resuming
the wrong digest cannot silently skip its first N sections.

## Running it

You need:

- a Readwise account with Reader, and its API token as `READWISE_TOKEN`
- a Discord bot in a server you control, and its token as `DISCORD_BOT_TOKEN`
- Python 3.11+
- the `claude` CLI on `PATH`, for `reading-list`'s summaries and for
  `import-reader-archive`'s ingest of documents Reader did not summarize itself
- `pip install -r morning-news/requirements.txt` (feed parsing is not in the
  stdlib; a venv is the sane place for it)

```bash
cp config.example.json config.json                    # your Discord channel ids and user id
cp morning-news/profile.example.md  morning-news/profile.local.md
cp morning-news/feeds.example.yaml  morning-news/feeds.local.yaml
```

Then rewrite the profile. The digest is only as good as that file — `prompt.md`
deliberately holds no opinion about what matters, so a vague profile produces a
vague digest. Be specific about what you want *and* what you don't; the "drop
aggressively" list does more for quality than any other tuning.

Configuration resolves in this order, and there is deliberately no baked-in
fallback — a fresh clone fails loudly with "configure this" rather than running
against someone else's channels:

1. `SKILLS_<DOTTED_KEY_UPPER>` env var — e.g. `SKILLS_DISCORD_CHANNELS_READING_LIST`
2. `config.local.json` — gitignored, for anything sensitive or per-machine
3. `config.json`

Tokens are read at runtime, never from the config. Everything the skills write —
the SQLite index, import dumps, resume state — lands under `paths.data_root`
(default `_data/` at the repo root, gitignored); every script takes a flag to
point elsewhere.

Each skill's `SKILL.md` carries its own flags, invariants and failure modes.
Start with `--dry-run`; every stage that posts has one.

## Honest limits

- **Nothing here ships a scheduler.** Each skill is a script or a prompt you run
  from cron, launchd, or an agent scheduler of your choice; the times above are
  the author's, not defaults. The scripts print a JSON status line and exit
  non-zero on hard failure, which is all a scheduler needs to alert on.
- **Discord is assumed, not abstracted.** The reaction contract in `reading-list`
  is built on Discord's per-message reactions; there is no posting interface to
  swap out. One owner per deployment: `discord.user_id` is a single id.
- **Feed runs are not reproducible.** Two fetches seconds apart routinely differ
  by an item, because outlets re-publish the same story under a second URL. A
  one-item delta between runs is not a bug.
- **There is no search front-end.** The two importers ship with their ingesters,
  so what you have read and highlighted lands in a SQLite FTS5 index at
  `<data_root>/knowledge/index.db` — but a query tool over that index is not
  included. `sqlite3` on the file is the interface.
