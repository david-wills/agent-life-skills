---
name: reading-list
description: Intermediary stage between the newsfeed digest and actually reading an article. Twice-daily cron pulls everything newly saved to the user's Readwise Reader inbox, generates a 3-paragraph summary from the full article text Reader already fetched, and posts one card per article to Discord #reading-list with ✅ / 📌 / 🗑️ reaction affordances. A nightly sweep turns those reactions into real Readwise actions. Use when tuning the summary prompt, changing cadence, debugging a missing or malformed card, or adding a reaction verb.
---

# Reading List

## Why it exists

The newsfeed digest surfaces articles; saving one to Readwise Reader was where they went to die. This skill adds the missing middle step — a digestible version that lands where the user already is, so the queue gets processed instead of accumulating.

Reader is the content source on purpose: its API returns `html_content` for documents it has already fetched **as the authenticated user**, so paywalled WSJ/NYT/etc. articles come back in full. No scraping, no paywall fight.

## Pieces

| Piece | Path |
| --- | --- |
| Shared plumbing | `scripts/reading_list_common.py` |
| Summarizer (5:45 AM / 5:45 PM PT) | `scripts/reading_list_digest.py` |
| Reaction sweep (12:01 AM PT) | `scripts/sweep_reading_list_reactions.py` |
| State | `reading_list_log` table in `knowledge/index.db` |
| Discord channel | `#reading-list` — `discord.channels.reading_list` in `config.json` |
| Crons | `reading-list-digest`, `reading-list-reaction-sweep` |

Secrets: Readwise token via `read_secret("readwise")`, which resolves through 1Password using the vault named in `config.json` → `secrets.onepassword_vault`. Discord token via `DISCORD_BOT_TOKEN` env or the gateway service-env file.

## Flow

1. `GET /api/v3/list/?location=new&withHtmlContent=true&updatedAfter=<watermark>` — everything saved to the inbox since the last run.
2. Skip categories that don't summarize (`tweet`, `video`, `note`, `highlight`); skip doc ids already in `reading_list_log`.
2b. If that turned up fewer than `BACKFILL_FLOOR` (3) items, top the run up from the back catalog — see below.
3. Strip HTML → text (`lib/html_text.py` at the repo root, shared with `import-reader-archive`), cap at 40k chars.
4. Generate three paragraphs via the Claude CLI (`claude-sonnet-5`): what it's about / the specifics and numbers / the overarching argument. Deliberately **no** "why this matters to you" — the user asked for the piece, not the pitch.
5. Post one plain Discord message per article — bolded title, Reader deep link, summary. Seed ✅ 📌 🗑️ on it and record the message id.
6. `PATCH /api/v3/update/<id>/ {"notes": ...}` — the same summary lands on the Reader document note, so it's there when the article is opened.

## Backfill

Saving outruns reading: 674 docs sat in the inbox when this shipped, 645 never summarized, going back to 2022-12. A run that found nothing new used to post nothing, so the queue only ever grew.

Now any run with fewer than 3 new saves fills the remainder from the catalog. **The floor is a floor, never a cap** — a run with 12 new saves posts all 12 and backfills nothing.

- **Newest-saved first.** The queue is worked backwards from recent saves toward the 2022 tail, not chronologically forward.
- **Articles only** (`BACKFILL_CATEGORIES`), unlike fresh saves. The catalog holds 76 epubs and 36 PDFs — *Thinking, Fast and Slow* among them — and `CONTENT_CHAR_CAP` would have them "summarized" from their first chapter.
- **Backfill cards are indistinguishable from fresh ones.** Deliberate: an article is an article regardless of when it was saved. Only the `--dry-run` output tags them.
- **`reading_list_log.doc_id` is the only record of what's been done.** No second state table, no cursor — a cursor would be a second source of truth that could disagree with the log. Selection is a live inbox scan minus that table, so a hand-run `--doc-id` or a re-saved article can never double-post.
- **Scan cheap, fetch expensive only for picks.** `select_backfill()` pulls the catalog with `with_html=False` (~4s for 674 docs); `rl.fetch_document()` pays for `html_content` per chosen doc (~0.2s). Fetching html for the whole catalog would be tens of MB per run.
- **Reserves absorb dead entries.** Old saves often have no retrievable text. `BACKFILL_RESERVE` (5) extra candidates queue behind the picks and cost nothing unless used; the run still delivers its 3.
- **A dud catalog pick is not an error.** Those land in `backfill_skipped`, not `errors` — the cron pages `#errors-alerts` on any non-empty `errors`, and a 2023 save whose text Reader never kept is expected, not a failure. Genuine failures on *new* saves still page.

At 3/run × 2 runs/day the 532-article catalog drains in ~89 days. `--no-backfill` or `--backfill-floor 0` turns it off; `--backfill-floor N` changes the rate.

Nightly, the sweep reads reactions on every `posted` card and drives Reader:

- ✅ → `location: archive`
- 📌 → `location: later`
- 🗑️ → `DELETE /api/v3/delete/<id>/`

## Invariants

- **One message per article.** Reactions are per-message; a batched digest would make the whole reaction contract impossible. Don't "consolidate" the cards.
- **The whole card stays under Discord's 2000 chars.** That's what keeps these plain messages. Anything longer has to become an embed (4096-char description), which is a visibly different card — and note that `flags: 4` is SUPPRESS_EMBEDS, so it hides *our own* embed, not just link previews. `summary_budget()` derives the summary allowance from 2000 minus the real title and link length, and `clamp_to_budget()` enforces it after the retries. Without that clamp an exhausted retry loop returns an over-budget draft and `post_article` splits the card in two — only the first chunk carries the reactions, so the whole triage contract silently breaks. Observed live at 2030 chars.
- **Summaries are capped proportionally to the source.** `SOURCE_COMPRESSION_RATIO` (0.40) stops the budget from becoming a target — a 460-word podcast blurb was drawing a 1792-char summary before this. Long pieces hit the 1800 ceiling regardless.
- **Length is enforced by retry, not by hope.** Every model tested overshoots character budgets — haiku by ~60% (asked 3000, wrote 4911), sonnet-5 on essentially every first pass. The prompt asks for a *word* count and `generate_summary()` retries up to 3× with the actual char count fed back. Budget one extra LLM call per article.
- **The trim retry gets stripped, not just instructed.** Asked to fit a limit, sonnet-5 reports its compliance — `"1713 characters, 269 words — within both the 1800-char hard limit and the 319-word cap."` — as a leading paragraph, which lands on the card. Reproduced on every run. `TRIM_PROMPT` forbids it *and* `strip_meta_preamble()` removes it; keep both, the prompt alone did not hold. The strip only fires on a short single-line paragraph that measures the text below it **and** only when there are more paragraphs than the prompt asked for, so real prose that cites numbers survives.
- **The card links to Reader (`url`), not the publisher (`source_url`).** Opening from the card lands where the highlights, document note and reaction verbs live. `reading_list_log.url` still stores `source_url` — that's the delete-recovery record.
- **Bot seeds the reactions, the user's click is the signal.** `sc.fetch_message_reactions` computes `by_user` as `count > (1 if me else 0)` — the bot's own seed reaction must stay at exactly one, so never react twice from the bot.
- **Conflicting reactions are never resolved by guessing.** Two verbs on one card → left `posted`, reported in the recap. Archive vs delete is not a coin flip.
- **Thin content is flagged, not hidden.** Under 400 words *and* under 1200 chars of text → the card carries a ⚠️ warning rather than a confident summary of a paywall stub.
- **Deletes are recorded before they happen.** `reading_list_log` keeps title, url and summary permanently, so a mis-click is recoverable by re-saving the url.
- **The watermark is a performance hint, not the dedupe guard.** `doc_id` uniqueness in `reading_list_log` is what prevents double-posting; the watermark just keeps the API call small and is deliberately rewound 10 minutes each run.

## Operating it

```bash
cd <repo>/reading/reading-list/scripts

python3 reading_list_digest.py --dry-run --hours 48      # see the cards, post nothing
python3 reading_list_digest.py --doc-id <id>             # force one article (never backfills)
python3 reading_list_digest.py --hours 72 --max-items 5  # manual catch-up run
python3 reading_list_digest.py --no-backfill             # new saves only
python3 reading_list_digest.py --backfill-floor 6        # drain the catalog faster

python3 sweep_reading_list_reactions.py --dry-run        # what would the sweep do
python3 sweep_reading_list_reactions.py --quiet          # apply, no recap post
```

Both scripts print a JSON status line and exit 0 on success. The crons are silent on success and alert to the Discord channel named in `config.json` → `discord.channels.errors` on failure.

`sample_summary_lengths.py --doc-id <id> --lengths 1500,2200,3000` renders the same article at several budgets for side-by-side comparison. It posts labelled sample cards and touches no state.

Useful knobs in `reading_list_common.py`: `SUMMARIZABLE_CATEGORIES`, `THIN_WORD_COUNT`, `REACTION_EXPIRY_DAYS` (unreacted cards stop being swept after 14 days), `ACTION_EMOJI`. Length knobs live in `reading_list_digest.py`: `SUMMARY_TARGET_CHARS`, `SOURCE_COMPRESSION_RATIO`, `MIN_SUMMARY_CHARS`.

Summarization runs on `claude-sonnet-5` (`--model` on both scripts). It replaced haiku on 2026-08-10 for holding onto far more of the article's hard specifics — figures, dates, names, the closing argument — at ~50s and two LLM calls per article. The cron's own `payload.model` is a separate thing: that's the agent that shells out to the script, and it stays on haiku. The digest cron allows 2400s of exec against a 25-article cap because of the slower model.

## Interaction with the rest of the KB

`ingest_reader.py` ingests **archived** Reader docs into the knowledge base and generates its own short KB summary. This skill writes to the document `notes` field, not `summary`, precisely so the two don't collide — the KB's `summary_source` accounting stays honest.

The Saturday `reader-inbox-triage-weekly` cron is a different surface: it nags about *stale* inbox items (30+ days, never opened). This skill handles *fresh* ones. They share the Reader API and nothing else.
