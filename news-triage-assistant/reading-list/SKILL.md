---
name: reading-list
description: Summarise new Readwise Reader saves into Discord cards with ✅ 📌 🗑️ reactions; a nightly sweep applies them. Use to tune the summary, cadence or reaction verbs.
compatibility: Requires the claude CLI for summaries, a Readwise Reader account and a Discord bot.
metadata: {"openclaw": {"requires": {"bins": ["claude"]}}}
---

# Reading List

## Why it exists

The newsfeed digest surfaces articles; saving one to Readwise Reader was where they went to die. This skill adds the missing middle step — a digestible version that lands where the user already is, so the queue gets processed instead of accumulating.

Reader is the content source on purpose: its API returns `html_content` for documents it has already fetched **as the authenticated user**, so paywalled articles come back in full. No scraping, no paywall fight.

## Pieces

| Piece | Path |
| --- | --- |
| Shared plumbing | `scripts/reading_list_common.py` |
| Summarizer | `scripts/reading_list_digest.py` |
| Reaction sweep | `scripts/sweep_reading_list_reactions.py` |
| Length sampler (one-off A/B) | `scripts/sample_summary_lengths.py` |
| State | `reading_list_log` and `skill_state` tables in `<data_root>/knowledge/index.db` (`--db` overrides) |
| Discord channel | `#reading-list` — `discord.channels.reading_list` in `config.json` |
| Schedule | summarizer twice a day, sweep once nightly, from any cron or agent scheduler (the author runs 5:45 AM / 5:45 PM and 12:01 AM) |

Secrets: Readwise token via `read_secret("readwise")` — `READWISE_TOKEN` in the environment, else the stores `lib/read_secret.py` knows about. Discord token via `DISCORD_BOT_TOKEN` the same way. Summaries need the `claude` CLI on `PATH`.

## Flow

1. `GET /api/v3/list/?location=new&withHtmlContent=true&updatedAfter=<watermark>` — everything saved to the inbox since the last run.
2. Skip categories that don't summarize (`tweet`, `video`, `note`, `highlight`); skip doc ids already in `reading_list_log`.
3. If that turned up fewer than `BACKFILL_FLOOR` (3) items, top the run up from the back catalog — see below.
4. Strip HTML → text (`lib/html_text.py` at the repo root, shared with `import-reader-archive`), cap at 40k chars.
5. Generate three paragraphs via the Claude CLI: what it's about / the specifics and numbers / the overarching argument. Deliberately **no** "why this matters to you" — the user asked for the piece, not the pitch.
6. Post one plain Discord message per article — bolded title, Reader deep link, summary. Seed ✅ 📌 🗑️ on it and record the message id.
7. `PATCH /api/v3/update/<id>/ {"notes": ...}` — the same summary lands on the Reader document note, so it's there when the article is opened.

## Backfill

Saving outruns reading. A run that found nothing new used to post nothing, so the queue only ever grew.

Now any run with fewer than 3 new saves fills the remainder from the catalog. **The floor is a floor, never a cap** — a run with 12 new saves posts all 12 and backfills nothing.

- **Newest-saved first.** The queue is worked backwards from recent saves toward the oldest tail, not chronologically forward.
- **Articles only** (`BACKFILL_CATEGORIES`), unlike fresh saves. Epubs and PDFs in the catalog are whole books, and `CONTENT_CHAR_CAP` would have them "summarized" from their first chapter.
- **Backfill cards are indistinguishable from fresh ones.** Deliberate: an article is an article regardless of when it was saved. Only the `--dry-run` output tags them.
- **`reading_list_log.doc_id` is the only record of what's been done.** No second state table, no cursor — a cursor would be a second source of truth that could disagree with the log. Selection is a live inbox scan minus that table, so a re-saved article can never double-post.
- **Scan cheap, fetch expensive only for picks.** `select_backfill()` pulls the catalog with `with_html=False` (a few seconds for hundreds of docs); `rl.fetch_document()` pays for `html_content` per chosen doc. Fetching html for the whole catalog would be tens of MB per run.
- **Reserves absorb dead entries.** Old saves often have no retrievable text. `BACKFILL_RESERVE` (5) extra candidates queue behind the picks and cost nothing unless used; the run still delivers its 3.
- **A dud catalog pick is not an error.** Those land in `backfill_skipped`, not `errors` — a scheduler that alerts `discord.channels.errors` on any non-empty `errors` would otherwise page on every run that touched an old save whose text Reader never kept. Genuine failures on *new* saves still page.

`--no-backfill` or `--backfill-floor 0` turns it off; `--backfill-floor N` changes the rate.

Nightly, the sweep reads reactions on every `posted` card and drives Reader:

- ✅ → `location: archive`
- 📌 → `location: later`
- 🗑️ → `DELETE /api/v3/delete/<id>/`

## Invariants

- **One message per article.** Reactions are per-message; a batched digest would make the whole reaction contract impossible. Don't "consolidate" the cards.
- **The whole card stays under Discord's 2000 chars.** That's what keeps these plain messages. Anything longer has to become an embed (4096-char description), which is a visibly different card — and note that SUPPRESS_EMBEDS hides *our own* embed, not just link previews. `summary_budget()` derives the summary allowance from 2000 minus the real title and link length, and `clamp_to_budget()` enforces it after the retries. Without that clamp an exhausted retry loop returns an over-budget draft and `post_article` splits the card in two — only the first chunk carries the reactions, so the whole triage contract silently breaks. Observed live at 2030 chars.
- **Summaries are capped proportionally to the source.** `SOURCE_COMPRESSION_RATIO` (0.40) stops the budget from becoming a target — a 460-word podcast blurb was drawing a 1792-char summary before this. Long pieces hit the 1800 ceiling regardless.
- **Length is enforced by retry, not by hope.** Every model tested overshoots character budgets — haiku by ~60% (asked 3000, wrote 4911), sonnet on essentially every first pass. The prompt asks for a *word* count and `generate_summary()` makes up to 3 attempts (`MAX_SUMMARY_ATTEMPTS`): the first draft, then trims with the actual char count fed back. Budget one or two extra LLM calls per article.
- **The trim retry gets stripped, not just instructed.** Asked to fit a limit, sonnet reports its compliance — `"1713 characters, 269 words — within both the 1800-char hard limit and the 319-word cap."` — as a leading paragraph, which lands on the card. Reproduced on every run. `TRIM_PROMPT` forbids it *and* `strip_meta_preamble()` removes it; keep both, the prompt alone did not hold. The strip only fires on a short single-line paragraph that measures the text below it **and** only when there are more paragraphs than the prompt asked for, so real prose that cites numbers survives.
- **The card links to Reader (`url`), not the publisher (`source_url`).** Opening from the card lands where the highlights, document note and reaction verbs live. `reading_list_log.url` still stores `source_url` — that's the delete-recovery record.
- **Bot seeds the reactions, the owner's click is the signal.** `fetch_message_reactions` in `lib/discord.py` lists who reacted with each emoji and sets `by_user` only when `discord.user_id` is among them. The bot's seeds and anyone else's clicks are ignored, so the bot can sit in a shared server. Without a numeric `discord.user_id` the sweep exits 2 and touches nothing.
- **An unreadable card is an error, never "no reaction".** If Discord refuses the message read (deleted message, expired token), the sweep records that against the card in `errors` and moves on. Treating it as a quiet night would let a broken token silently expire every pending card.
- **Conflicting reactions are never resolved by guessing.** Two verbs on one card → left `posted`, reported in the recap. Archive vs delete is not a coin flip.
- **Thin content is flagged, not hidden.** Under 400 words *and* under 1200 chars of text → the card carries a ⚠️ warning rather than a confident summary of a paywall stub.
- **Deletes are recorded before they happen.** `reading_list_log` keeps title, url and summary permanently, so a mis-click is recoverable by re-saving the url.
- **The watermark is a performance hint, not the dedupe guard.** `doc_id` uniqueness in `reading_list_log` is what prevents double-posting; the watermark (in `skill_state`) just keeps the API call small and is deliberately rewound 10 minutes each run.
- **`--doc-id` respects the log.** A doc already in `reading_list_log` is reported as `already_posted` and nothing happens; `--force` overrides. It looks the doc up in the inbox and then in Later, fetches just that one document, and never backfills.

## Operating it

```bash
cd news-triage-assistant/reading-list/scripts

python3 reading_list_digest.py --dry-run --hours 48      # see the cards, post nothing
python3 reading_list_digest.py --doc-id <id>             # one article from inbox or Later
python3 reading_list_digest.py --doc-id <id> --force     # ...even if it was already posted
python3 reading_list_digest.py --hours 72 --max-items 5  # manual catch-up run
python3 reading_list_digest.py --no-backfill             # new saves only
python3 reading_list_digest.py --backfill-floor 6        # drain the catalog faster

python3 sweep_reading_list_reactions.py --dry-run        # what would the sweep do
python3 sweep_reading_list_reactions.py --quiet          # apply, no recap post

python3 sample_summary_lengths.py --doc-id <id> --lengths 1500,2200,3000
```

`reading_list_digest.py` flags: `--hours N` (lookback override; default is the stored watermark, else 24h), `--max-items N` (per-run cap, default 25), `--channel ID`, `--db PATH`, `--doc-id ID`, `--force`, `--dry-run`, `--no-note` (don't write the summary to the Reader note), `--model NAME` (default `claude-sonnet-5`), `--backfill-floor N`, `--no-backfill`.

`sweep_reading_list_reactions.py` flags: `--dry-run` (reads reactions, changes nothing, never resolves the Readwise token), `--quiet` (no recap post), `--db PATH`, `--expiry-days N` (default 14).

`sample_summary_lengths.py` flags: `--doc-id ID` (required), `--lengths A,B,C` (target char counts), `--channel ID`, `--model NAME`, `--dry-run`. It renders the same article at several budgets for side-by-side comparison, posts labelled sample cards, and touches no state.

Both scheduled scripts print a JSON status line and exit 0 on success. Wire your scheduler to alert on non-zero exit or a non-empty `errors` list — the Discord channel in `config.json` → `discord.channels.errors` is the conventional place.

Useful knobs in `reading_list_common.py`: `SUMMARIZABLE_CATEGORIES`, `THIN_WORD_COUNT`, `REACTION_EXPIRY_DAYS` (unreacted cards stop being swept after 14 days), `ACTION_EMOJI`. Length knobs live in `reading_list_digest.py`: `SUMMARY_TARGET_CHARS`, `SOURCE_COMPRESSION_RATIO`, `MIN_SUMMARY_CHARS`.

Summarization defaults to `claude-sonnet-5` (`--model` on both scripts). It replaced haiku for holding onto far more of the article's hard specifics — figures, dates, names, the closing argument — at ~50s and two LLM calls per article. Size the schedule's timeout accordingly: a 25-article run can take twenty minutes.

## Interaction with the importers

`ingest_reader.py` (in `import-reader-archive`) ingests **archived** Reader docs into the local full-text index and generates its own short abstract. This skill writes to the document `notes` field, not `summary`, precisely so the two don't collide — the index's `summary_source` accounting stays honest. Both skills share `<data_root>/knowledge/index.db`; they use different tables.
