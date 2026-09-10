# Field notes: news-triage-assistant

The operational history behind the package: what runs when, what broke, what
was tried and thrown away, and the constants that matter. It is kept out of the
`SKILL.md` files so those stay short enough to be instructions.

## How it runs

- Five scheduled pieces, in the author's order through a morning: the Readwise
  highlights delta and the Reader archive delta in one job (importer, then
  ingester, for each), the reading-list summarizer, then the news digest. The
  summarizer fires again in the evening; the reaction sweep runs just after
  midnight. Nothing in the package schedules itself; every piece is a script or
  a prompt a scheduler runs.
- `morning-news` pulls about two dozen feeds through eight workers with a
  20-second per-request timeout, so the fetch is bounded at under two minutes
  even if several feeds hang. The agent turn that classifies is the slow part.
  Posting 20-25 messages at 1.2s apart takes about half a minute.
- `reading-list` posts three to a dozen cards a run. Each card costs one or two
  `claude` calls at around 50 seconds per article, so a run against the
  25-article cap can take twenty minutes; the scheduler's timeout was raised to
  forty when the summarizer moved off haiku.
- The backfill drains the old inbox at three articles per quiet run, twice a
  day. A catalog of several hundred unsummarized saves going back a few years
  works out to roughly three months to clear.

## What broke, and what it taught

- **An over-long section 18 aborted a digest that had already posted 17.** The
  poster validated as it went. `post_digest.py` now checks every message against
  the 2000-character cap before sending any; the prompt targets 1900 for slack.
- **Re-running a half-posted digest duplicated every section already in the
  channel.** Progress lands in `<data_root>/morning-news/last_run.json` after
  each post, and `--resume` continues from there, keyed on channel and message
  count so resuming the wrong digest cannot skip sections. `--no-state` exists
  because the failure notice to the errors channel was overwriting that record.
- **Guessing the 429 wait half-posted digests.** Twenty-odd messages exceed
  Discord's per-channel burst allowance. Sends are paced, and a 429 waits for
  exactly the `retry_after` Discord returns, capped at a minute.
- **One feed that never answered took the whole fetch with it.** The old
  collector raised on the first timeout and the run died. `fetch_feeds.py` now
  sets a socket timeout per request and a batch deadline derived from it; feeds
  still outstanding at the deadline land in `errors` and the run carries on. A
  feed that returns an HTTP error or an unparseable body is an error too, not a
  quiet zero.
- **A 2030-character card silently broke triage.** The summary overran, the
  poster split the card in two, and only the first chunk carried the reactions.
  `summary_budget()` derives the allowance from 2000 minus the real title and
  link, and `clamp_to_budget()` enforces it after the retries are exhausted.
- **Every model overshoots a character budget.** Haiku asked for 3000 wrote
  4911; sonnet overshoots on nearly every first pass. The prompt asks for a word
  count and `generate_summary()` retries up to twice with the actual count fed
  back. That is the origin of the "one or two calls per article" cost.
- **Sonnet reports its compliance on the card.** Asked to trim, it led with a
  line like "1713 characters, 269 words, within both limits", which posted
  verbatim. `TRIM_PROMPT` forbids it and `strip_meta_preamble()` removes it;
  the prompt alone did not hold.
- **A 460-word podcast blurb drew a 1792-character summary.** The budget had
  become a target. `SOURCE_COMPRESSION_RATIO` caps the summary at 40% of the
  source; long pieces still hit the 1800 ceiling.
- **A quiet inbox meant an empty channel while hundreds of saves sat unread.**
  Runs with fewer than three new saves now top up from the catalog, newest
  first, articles only, with five reserves behind the picks because old saves
  often have no retrievable text. Dud picks report as `backfill_skipped`, not
  `errors`, because a scheduler that pages on the expected case gets muted.
- **Backfill made the script chatty enough to trip Reader's throttle.** A full
  catalog scan plus a fetch per pick drew 429s. `_reader_get()` reads
  `Retry-After` or the "available in N seconds" body and waits it out, up to
  three times and 90 seconds each, rather than failing the run.
- **A stranger's reaction could delete a Reader document.** The sweep inferred
  ownership from counts: any non-bot reactor was the owner. Found in review
  before it happened live. `fetch_message_reactions` in `lib/discord.py` now
  lists who reacted and sets `by_user` only for `discord.user_id`; the sweep
  exits 2 without a configured id.
- **An unreadable card used to read as "no reaction".** The old reaction reader
  swallowed any Discord error into an empty list, so an expired bot token would
  have aged out every pending card over two weeks. The sweep now records a read
  failure against the card in `errors` and moves on.
- **The ingester's summary calls ran the CLI with permissions bypassed.** Article
  text is untrusted input. `lib/claude_cli.py` now runs `claude --print` with no
  tools, no MCP servers, no user settings and a deny-all permission mode;
  verified with an injection probe asking for shell and file writes.
- **Image-only highlights crashed the digest.** Readwise returns `text: null`
  for them. Both the markdown digest and the ingester treat that as empty.
- **`--doc-id` ignored the log and could double-post.** It now reports
  `already_posted` unless `--force` is given, and never backfills.

## Things tried and dropped

- A shared helper module of about 850 lines from the private repo, most of it
  plumbing for an unrelated question-and-answer skill plus Slack transport.
  Replaced by `lib/discord.py`, `lib/claude_cli.py` and `lib/state_db.py`,
  which is everything this package actually used.
- Slack as the delivery surface. Messages were composed in Slack's dialect
  before the move to Discord, and `normalize_markdown()` in `lib/discord.py`
  still rewrites `<url|label>` links, `*bold*` and `:shortcode:` emoji. The
  reading-list card bypasses it so the card is byte-for-byte what was composed.
- Reading the Discord token from the agent gateway's service environment file.
  Tokens now come from the environment or the stores `lib/read_secret.py` knows.
- A pre-commit lint on personal identifiers and an rsync-based export script
  with an allowlist and a `*.local.*` exclusion. Both belong to the private
  repo and are how this package was produced; neither ships here. The lesson
  that survived is in the file layout: `*.local.*` is gitignored and an
  `*.example.*` twin ships in its place.
- A weekly nag about stale inbox items (a month old, never opened). It shares
  the Reader API with `reading-list` and nothing else, and it stayed private.
- References to a ranked query tool over the full-text index. It is in an
  unpublished package; `sqlite3` on the index file is the interface here.

## Decisions that look odd

- **Reader's `html_content`, not a scraper.** Reader already fetched the page
  as the authenticated user, so subscription articles come back whole. A
  scraper would re-fight every paywall the reader had already passed.
- **One Discord message per theme.** Discord caps messages at 2000 characters,
  so a digest cannot be one message anyway. A theme is the unit a reader skips
  or reads, and it gives the poster an atomic unit to validate and resume on.
- **One message per article, never a batched digest.** Reactions are
  per-message. Consolidating the cards would take the whole triage contract
  with it.
- **Summaries go through the `claude` CLI, not an API key.** The scripts hold
  no key and no LLM client; the CLI is already on the machine that runs the
  agent, and `lib/claude_cli.py` can strip it to a pure text function. The
  agent that shells out to the summarizer stays on a cheap model; only the
  summary itself pays for sonnet, because haiku dropped the figures, names and
  closing argument that make a card worth reading.
- **Small helpers duplicated across the two importers; the FTS schema not.**
  `_slug`, ISO parsing and frontmatter rendering are copied so each skill
  stands alone. The `entries` DDL lives once in `lib/fts_schema.py` because five
  ingesters write the same table and its columns must agree.
- **One table is the only state.** `reading_list_log.doc_id` decides what has
  been posted; selection is a live inbox scan minus that table. The watermark
  in `skill_state` is a performance hint, rewound ten minutes each run, and is
  never the dedupe guard.
- **Undated feed items are kept, not dropped.** Dropping them would blank any
  feed that omits dates. The cost is that such a feed re-surfaces its backlog
  every run, and `counts.undated_kept` is how you notice.
- **The card links to Reader, not the publisher.** That is where the note, the
  highlights and the reaction verbs live; the log keeps the publisher URL.
- **The summary lands in Reader's `notes`, never `summary`.** The archive
  ingester prefers Reader's own summary and records which source it used;
  writing to `summary` would make that accounting lie.

## Numbers worth knowing

| Constant | Value | Lives in |
| --- | --- | --- |
| Discord hard cap / prompt target / card safe length | 2000 / 1900 / 1990 chars | `morning-news/scripts/post_digest.py`, `morning-news/prompt.md`, `reading-list/scripts/reading_list_digest.py` |
| Send interval between digest messages | 1.2 s | `morning-news/scripts/post_digest.py` |
| 429 wait cap / non-429 retry wait / 429 attempts | 60 s / 30 s / 6 | `morning-news/scripts/post_digest.py` |
| Feed fetch: workers / per-request timeout | 8 / 20 s (batch deadline scales with it) | `morning-news/scripts/fetch_feeds.py` |
| Summary target / minimum / compression ratio | 1800 / 700 chars / 0.40 of source | `reading-list/scripts/reading_list_digest.py` |
| Summary attempts (draft plus trims) | 3 | `reading-list/scripts/reading_list_digest.py` |
| Source text cap fed to the summarizer | 40,000 chars | `reading-list/scripts/reading_list_digest.py` |
| Per-run article cap | 25 | `reading-list/scripts/reading_list_digest.py` |
| Backfill floor / reserve | 3 / 5 | `reading-list/scripts/reading_list_digest.py` |
| Thin-content threshold | under 400 words and 1200 chars | `reading-list/scripts/reading_list_common.py` |
| Reaction expiry | 14 days | `reading-list/scripts/reading_list_common.py` |
| Reader throttle retries / max wait / page sleep | 3 / 90 s / 0.25 s | `reading-list/scripts/reading_list_common.py`, `import-reader-archive/scripts/import_reader_archive.py` |
| Ingester summary: html cap / CLI timeout | 60,000 chars (about 15k tokens) / 180 s | `import-reader-archive/scripts/ingest_reader.py` |
