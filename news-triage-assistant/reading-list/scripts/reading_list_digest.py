#!/usr/bin/env python3
"""Summarize newly-saved Readwise Reader inbox items into Discord #reading-list.

Meant to run a couple of times a day. For every document that has landed in the
Reader inbox since the last watermark:

  1. pull the full text Reader already fetched (`withHtmlContent=true`)
  2. generate a 3-paragraph summary via the Claude CLI
  3. post one Discord message per article, seeded with ✅ / 📌 / 🗑️ reactions
  4. write the summary back to the Reader document note

One message per article is deliberate: reactions are per-message, and the
nightly sweep (`sweep_reading_list_reactions.py`) reads them to drive Reader.

Exit 0 with a JSON status line on stdout. Non-zero only on hard failure.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

# Shared helpers live in lib/ at the repo root; walk up so this works from any cwd.
_LIB = next((p / "lib" for p in Path(__file__).resolve().parents if (p / "lib" / "skill_config.py").is_file()), None)
if _LIB is None:
    raise SystemExit("cannot find the repo-root lib/ directory; run from a clone of the repo, not a copied file")
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))
import reading_list_common as rl  # noqa: E402
from claude_cli import claude_generate  # noqa: E402
from discord import SUPPRESS_EMBEDS, discord_request, split_for_discord  # noqa: E402
from html_text import html_to_text  # noqa: E402
from skill_config import cfg  # noqa: E402
from state_db import connect_db, get_state, set_state  # noqa: E402

DEFAULT_LOOKBACK_HOURS = 24
DEFAULT_MAX_ITEMS = 25
DEFAULT_MODEL = "claude-sonnet-5"
CONTENT_CHAR_CAP = 40_000
DISCORD_SAFE_CHARS = 1990

# Discord caps a plain message at 2000 chars. The card is title + Reader link +
# summary, so the summary budget is 2000 minus that chrome. Staying under the cap
# is what keeps these plain messages instead of embeds.
SUMMARY_TARGET_CHARS = 1800

# A summary must be a compression, not a paraphrase. Without this, a 460-word
# podcast blurb got a 1792-char summary: the budget became a target. Short
# pieces get a proportionally smaller budget; long ones hit the ceiling anyway.
SOURCE_COMPRESSION_RATIO = 0.40
MIN_SUMMARY_CHARS = 700

# A quiet day used to mean an empty channel while a years-deep inbox sat unread.
# Runs that find fewer than this many new saves top themselves up from the back
# catalog. A busy run posts everything new and backfills nothing: the floor is a
# floor, never a cap.
BACKFILL_FLOOR = 3

# Backfill is articles-only, unlike fresh saves. Epubs and PDFs in the catalog
# are whole books, and CONTENT_CHAR_CAP means those get "summarized" from their
# first chapter. Wrong for a reading card, so they stay out of the pool.
BACKFILL_CATEGORIES = {"article"}

# Old saves are likelier to have no retrievable text than fresh ones, and a doc that
# yields nothing must not silently eat a slot. Extra candidates are drawn as reserves
# and cost nothing unless used: html is fetched per doc, lazily, only when needed.
BACKFILL_RESERVE = 5

# --doc-id looks in these Reader locations, in this order. Archived and deleted
# docs are not summarized on request: the sweep would only move them back.
DOC_ID_LOCATIONS = ("new", "later")

# This register runs ~6.2 chars/word. Every model tested overshoots character
# budgets (haiku by ~60%) but tracks word counts well, so the prompt asks for
# words and the retry loop feeds the real character count back.
CHARS_PER_WORD = 6.2
MAX_SUMMARY_ATTEMPTS = 3  # first draft plus up to two trims

SUMMARY_PROMPT = """You are writing a digest entry for {user}'s personal reading list. They saved \
this article to read later; your summary is the version they read when they do not have time for \
the whole thing.

Write exactly three paragraphs of prose. No headings, no bullet points, no preamble, no sign-off. \
Do not restate the title. Do not address {user} or explain why the piece matters to them.

- Paragraph 1: what the piece is actually about and what happens in it.
- Paragraph 2: the substance: the specific claims, findings, events, and any concrete numbers, \
names, dates, dollar figures or data points that carry the story. Prefer specifics over \
characterization.
- Paragraph 3: the overarching argument, perspective or through-line the author is driving at, \
including their stance if it is an opinion piece.

Write like a sharp friend explaining what they just read, not like an academic abstract. Aim for \
about {words} words total, and do not exceed {max_words} words. Spend the budget on real detail \
from the piece: do not pad, do not repeat yourself, and do not add anything the article does not \
say. If the article genuinely does not contain enough substance to fill the budget, write less \
rather than inflate.

TITLE: {title}
SOURCE: {site}

ARTICLE TEXT:
{body}
"""

TRIM_PROMPT = """That draft is {actual} characters. The hard limit is {limit} characters, about \
{words} words. Rewrite it to fit, keeping all three paragraphs and every concrete number, name \
and date. Cut characterization and hedging, not facts.

Your entire response must be the rewritten summary and nothing else, beginning with the first word \
of paragraph one. Do not report the new length, do not state that it fits, do not comment on the \
rewrite in any way: that text would be posted verbatim to a Discord card."""


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Post Reader inbox summaries to Discord #reading-list.")
    p.add_argument("--hours", type=float, help="Lookback window override (default: stored watermark, else 24h).")
    p.add_argument("--max-items", type=int, default=DEFAULT_MAX_ITEMS, help="Safety cap per run.")
    p.add_argument("--channel", default=None,
                   help="Discord channel id (default: discord.channels.reading_list from config.json).")
    p.add_argument("--db", default=None,
                   help="SQLite file for the log and watermark (default: <data_root>/knowledge/index.db).")
    p.add_argument("--doc-id", help="Summarize exactly this doc id (inbox or Later), ignoring the watermark.")
    p.add_argument("--force", action="store_true",
                   help="With --doc-id: re-summarize even if the doc is already in reading_list_log.")
    p.add_argument("--dry-run", action="store_true", help="Print summaries; do not post, write notes, or record state.")
    p.add_argument("--no-note", action="store_true", help="Skip writing the summary back to the Reader document note.")
    p.add_argument("--model", default=DEFAULT_MODEL, help=f"Claude CLI model for summarization (default {DEFAULT_MODEL}).")
    p.add_argument("--backfill-floor", type=int, default=BACKFILL_FLOOR,
                   help=f"Top a quiet run up to this many cards from the back catalog (default {BACKFILL_FLOOR}; 0 disables).")
    p.add_argument("--no-backfill", action="store_true", help="Post only genuinely new saves.")
    return p.parse_args()


def select_backfill(token: str, conn, need: int, exclude: set[str]) -> list[dict[str, Any]]:
    """Newest-saved unsummarized articles, metadata only, with reserves behind them.

    Scanning the whole inbox without html is cheap (a few seconds for hundreds of
    docs), so this runs on every quiet run rather than being cached. A cache would
    just be a second source of truth about what has been summarized, and
    `reading_list_log` already is one.
    """
    if need <= 0:
        return []
    docs = rl.fetch_inbox(token, None, with_html=False)
    pool = [
        d for d in docs
        if d.get("id")
        and d["id"] not in exclude
        and (d.get("category") or "").lower() in BACKFILL_CATEGORIES
        and not rl.already_posted(conn, d["id"])
    ]
    pool.sort(key=lambda d: d.get("saved_at") or d.get("created_at") or "", reverse=True)
    return pool[: need + BACKFILL_RESERVE]


def find_document(token: str, doc_id: str) -> dict[str, Any] | None:
    """Look a doc up by id in each of DOC_ID_LOCATIONS, first hit wins."""
    for location in DOC_ID_LOCATIONS:
        doc = rl.fetch_document(token, doc_id, location=location)
        if doc:
            return doc
    return None


def doc_text(doc: dict[str, Any]) -> str:
    html = doc.get("html_content") or ""
    text = html_to_text(html) if html else ""
    if not text:
        text = (doc.get("summary") or "").strip()
    return text[:CONTENT_CHAR_CAP]


def is_thin(doc: dict[str, Any], text: str) -> bool:
    try:
        words = int(doc.get("word_count") or 0)
    except (TypeError, ValueError):
        words = 0
    return words < rl.THIN_WORD_COUNT and len(text) < rl.THIN_TEXT_CHARS


# Sonnet answers the trim instruction by showing its work ("1713 characters, 269
# words, within both the 1800-char hard limit and the 319-word cap.") as a leading
# line of the summary, which then lands on the card. The prompt asks it not to; this
# is the guard that actually holds. A meta line is always a short one-line paragraph
# that measures the text below it; a real paragraph 1 is 260+ chars of prose about the
# article. All the signals must agree (short, single-line, an explicit "<n> chars/
# words" measurement, a word about fitting a budget, and a spare paragraph above the
# three the summary contract requires) so prose that merely happens to cite numbers
# survives even when it opens the piece.
MAX_META_LINE_CHARS = 150
SUMMARY_PARAGRAPHS = 3
META_LINE_RE = re.compile(
    r"^(?=.*\b\d[\d,]*\s*[-–]?\s*(?:char|chars|character|characters|word|words)\b)"
    r".*\b(?:limit|cap|budget|under|within|fits?|total|count|target|exceed\w*|"
    r"paragraphs?|preserved)\b.*$",
    re.IGNORECASE,
)


def strip_meta_preamble(summary: str, keep: int = SUMMARY_PARAGRAPHS) -> str:
    """Drop a leaked self-audit line ("1713 characters, within the limit") off the top.

    `keep` is how many paragraphs the caller's prompt asked for; anything above that
    count is what a leak would occupy.
    """
    paras = re.split(r"\n\s*\n", summary.strip())
    while len(paras) > keep:
        head = paras[0].strip()
        if len(head) > MAX_META_LINE_CHARS or "\n" in head or not META_LINE_RE.match(head):
            break
        paras.pop(0)
    return "\n\n".join(paras).strip()


def clamp_to_budget(summary: str, budget: int) -> str:
    """Last resort when the retries all came back over budget.

    Without this the card silently exceeds 2000 chars and `post_article` splits it in
    two, which breaks the one-message-per-article contract the reactions depend on,
    since only the first chunk carries them. Losing a trailing sentence is the better
    failure. Cuts on a sentence boundary when there is a reasonable one, and keeps the
    paragraph breaks above it intact.
    """
    if len(summary) <= budget:
        return summary
    window = summary[:budget]
    cut = max(window.rfind(". "), window.rfind(".\n"), window.rfind("? "), window.rfind("! "))
    if cut > int(budget * 0.6):
        return window[: cut + 1].rstrip()
    # The ellipsis has to fit inside the budget, not be appended past it.
    return summary[: budget - 1].rstrip() + "…"


def generate_summary(doc: dict[str, Any], text: str, model: str, user_name: str,
                     budget: int = SUMMARY_TARGET_CHARS) -> str:
    """Generate, then hold the model to the budget so the card stays one message.

    Up to MAX_SUMMARY_ATTEMPTS calls: the first draft, then trims with the real
    character count fed back. Whatever is left is clamped.
    """
    words = int(budget / CHARS_PER_WORD)
    prompt = SUMMARY_PROMPT.format(
        user=user_name,
        words=words,
        max_words=int(words * 1.1),
        title=(doc.get("title") or "Untitled").strip(),
        site=(doc.get("site_name") or doc.get("source") or "unknown").strip(),
        body=text,
    )
    summary = strip_meta_preamble(claude_generate(prompt, model=model, timeout=180))
    attempt = 1
    while len(summary) > budget and attempt < MAX_SUMMARY_ATTEMPTS:
        trim = (
            prompt
            + "\n\nPREVIOUS DRAFT:\n"
            + summary
            + "\n\n"
            + TRIM_PROMPT.format(actual=len(summary), limit=budget, words=words)
        )
        try:
            retry = strip_meta_preamble(claude_generate(trim, model=model, timeout=180))
        except Exception:  # noqa: BLE001 - keep the long draft rather than losing it
            break
        if not retry:
            break
        summary = retry
        attempt += 1
    return clamp_to_budget(summary, budget)


THIN_WARNING = "⚠️ Thin content: Reader only captured a stub, so this summary may be shallow."


def reader_url(doc: dict[str, Any]) -> str:
    """The Reader deep link (`url`), not the publisher's (`source_url`).

    The card links straight into Reader so the article opens where the highlights,
    the document note and the reaction verbs all live.
    """
    return (doc.get("url") or doc.get("source_url") or "").strip()


def summary_budget(doc: dict[str, Any], text: str, thin: bool) -> int:
    """Chars allowed for the summary: the tighter of two constraints.

    1. Card chrome: the whole message must clear Discord's 2000-char limit, which
       is what keeps these plain messages instead of embeds.
    2. Compression: a summary of a short piece must stay a fraction of it.
    """
    chrome = len((doc.get("title") or "Untitled").strip()) + len(reader_url(doc)) + 12
    if thin:
        chrome += len(THIN_WARNING) + 2
    proportional = max(MIN_SUMMARY_CHARS, int(len(text) * SOURCE_COMPRESSION_RATIO))
    return min(SUMMARY_TARGET_CHARS, DISCORD_SAFE_CHARS - chrome, proportional)


def compose_message(doc: dict[str, Any], summary: str, thin: bool) -> str:
    title = (doc.get("title") or "Untitled").strip()
    url = reader_url(doc)

    lines = [f"**{title}**"]
    if url:
        lines.append(f"<{url}>")
    lines.append("")
    if thin:
        lines.append(THIN_WARNING)
        lines.append("")
    lines.append(summary)
    return "\n".join(lines)


def _post_plain(channel: str, content: str, token: str) -> dict[str, Any]:
    """Raw post, bypassing send_message's markdown normalisation so the card
    is byte-for-byte what compose_message produced."""
    return discord_request(
        "POST",
        f"/channels/{channel}/messages",
        {"content": content, "flags": SUPPRESS_EMBEDS, "allowed_mentions": {"parse": []}},
        token=token,
    )


def post_article(channel: str, message: str, token: str) -> str:
    """Post the article card. Returns the root message id (the reaction anchor)."""
    chunks = split_for_discord(message, limit=DISCORD_SAFE_CHARS)
    root = _post_plain(channel, chunks[0], token)
    for chunk in chunks[1:]:
        time.sleep(0.3)
        _post_plain(channel, chunk, token)
    return root["id"]


def main() -> int:
    args = parse_args()
    run_started = rl.utc_now()

    channel = args.channel or rl.reading_list_channel()
    user_name = cfg("user.display_name", "the user")
    db = Path(args.db).expanduser() if args.db else rl.db_path()

    token = rl.reader_token()
    conn = connect_db(db)
    rl.ensure_tables(conn)

    errors: list[str] = []
    backfill_skipped: list[str] = []

    if args.doc_id:
        updated_after = None
        if rl.already_posted(conn, args.doc_id) and not args.force:
            print(json.dumps({
                "status": "already_posted", "doc_id": args.doc_id,
                "hint": "pass --force to re-summarize and re-post it",
            }))
            return 0
        doc = find_document(token, args.doc_id)
        if not doc:
            print(json.dumps({
                "status": "not_found", "doc_id": args.doc_id,
                "error": f"doc not in Reader locations {list(DOC_ID_LOCATIONS)}",
            }))
            return 1
        docs = [doc]
    else:
        if args.hours is not None:
            updated_after = rl.iso_utc(run_started - dt.timedelta(hours=args.hours))
        else:
            stored = get_state(conn, rl.WATERMARK_KEY)
            updated_after = stored or rl.iso_utc(
                run_started - dt.timedelta(hours=DEFAULT_LOOKBACK_HOURS)
            )
        docs = rl.fetch_inbox(token, updated_after)

    candidates: list[dict[str, Any]] = []
    skipped_category = 0
    for doc in docs:
        doc_id = doc.get("id")
        if not doc_id:
            continue
        if (doc.get("category") or "").lower() not in rl.SUMMARIZABLE_CATEGORIES:
            skipped_category += 1
            continue
        if not args.doc_id and rl.already_posted(conn, doc_id):
            continue
        candidates.append(doc)

    # Oldest save first, so the channel reads chronologically.
    candidates.sort(key=lambda d: d.get("saved_at") or d.get("created_at") or "")
    capped = len(candidates) > args.max_items
    dropped = candidates[args.max_items:] if capped else []
    candidates = candidates[: args.max_items]

    # Top a quiet run up from the back catalog. Newest-saved first, so the queue is
    # worked backwards from things saved recently toward the oldest tail.
    backfill_needed = 0
    backfill_pool: list[dict[str, Any]] = []
    if not args.doc_id and not args.no_backfill and len(candidates) < args.backfill_floor:
        backfill_needed = args.backfill_floor - len(candidates)
        try:
            backfill_pool = select_backfill(
                token, conn, backfill_needed, {d["id"] for d in candidates}
            )
        except Exception as exc:  # noqa: BLE001 - a backfill failure must not lose new saves
            errors.append(f"backfill scan: {type(exc).__name__}: {exc}")
            backfill_needed = 0

    # New saves oldest-first so the channel reads chronologically, then the catalog
    # picks behind them. Reserves are only touched if an earlier pick yields no text.
    work = [(d, False) for d in candidates] + [(d, True) for d in backfill_pool]

    discord_token = None if args.dry_run else rl.discord_token()
    posted = 0
    backfill_posted = 0

    def problem(msg: str, is_backfill: bool) -> None:
        """A new save that fails is a real error; a catalog pick that fails is just the
        next reserve's turn. Routing these together would page the errors channel on
        every run that touched an old save whose text Reader never kept."""
        (backfill_skipped if is_backfill else errors).append(msg)

    for doc, is_backfill in work:
        if is_backfill:
            if backfill_posted >= backfill_needed:
                break
            try:
                full = rl.fetch_document(token, doc["id"])
            except Exception as exc:  # noqa: BLE001 - try the next reserve instead
                problem(f"{doc['id']}: backfill fetch {type(exc).__name__}: {exc}", True)
                continue
            if not full:
                problem(f"{doc['id']}: backfill fetch returned nothing", True)
                continue
            doc = full
        doc_id = doc["id"]
        title = (doc.get("title") or "Untitled").strip()
        try:
            text = doc_text(doc)
            if not text.strip():
                problem(f"{doc_id}: no content ({title[:60]})", is_backfill)
                continue
            thin = is_thin(doc, text)
            summary = generate_summary(doc, text, args.model, user_name,
                                       budget=summary_budget(doc, text, thin))
            if not summary:
                problem(f"{doc_id}: empty summary ({title[:60]})", is_backfill)
                continue
            message = compose_message(doc, summary, thin)

            if args.dry_run:
                tag = f" [backfill, saved {(doc.get('saved_at') or '?')[:10]}]" if is_backfill else ""
                print(f"\n--- {doc_id} ({len(message)} chars){tag} ---\n{message}\n")
                posted += 1
                backfill_posted += is_backfill
                continue

            message_id = post_article(channel, message, discord_token)
            rl.add_affordances(channel, message_id, discord_token)

            note_status = "skipped"
            if not args.no_note:
                ok, detail = rl.reader_patch(doc_id, {"notes": summary}, token)
                note_status = "ok" if ok else f"failed: {detail}"
                if not ok:
                    errors.append(f"{doc_id}: note write {detail}")

            conn.execute(
                """
                INSERT OR REPLACE INTO reading_list_log
                    (doc_id, message_id, channel, title, url, site, word_count,
                     thin, summary, posted_at, status, action, action_detail, actioned_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'posted', NULL, ?, NULL)
                """,
                (
                    doc_id,
                    message_id,
                    channel,
                    title,
                    doc.get("source_url") or doc.get("url"),
                    doc.get("site_name") or doc.get("source"),
                    doc.get("word_count"),
                    1 if thin else 0,
                    summary,
                    rl.iso_utc(rl.utc_now()),
                    f"note={note_status}",
                ),
            )
            conn.commit()
            posted += 1
            backfill_posted += is_backfill
            time.sleep(0.5)
        except Exception as exc:  # noqa: BLE001 - one bad doc must not kill the run
            problem(f"{doc_id}: {type(exc).__name__}: {exc}", is_backfill)

    if capped and not args.dry_run:
        try:
            _post_plain(
                channel,
                f"-# ⚠️ {len(dropped)} more saved item(s) exceeded the per-run cap of "
                f"{args.max_items} and were not summarized this run. They stay in the "
                "inbox and will be picked up next run.",
                discord_token,
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(f"cap notice: {type(exc).__name__}: {exc}")

    # Only advance the watermark on a clean-ish run; overlap is harmless because
    # doc_id dedupe is the real guard.
    if not args.dry_run and not args.doc_id:
        set_state(conn, rl.WATERMARK_KEY, rl.iso_utc(run_started - dt.timedelta(minutes=10)))
        conn.commit()

    result = {
        "status": "posted" if posted else "nothing_new",
        "fetched": len(docs),
        "candidates": len(candidates) + len(dropped),
        "posted": posted,
        "new_posted": posted - backfill_posted,
        "backfill_posted": backfill_posted,
        "backfill_wanted": backfill_needed,
        "backfill_skipped": backfill_skipped,
        "skipped_category": skipped_category,
        "deferred_over_cap": len(dropped),
        "updated_after": updated_after,
        "errors": errors,
        "dry_run": bool(args.dry_run),
    }
    print(json.dumps(result))
    return 1 if errors and posted == 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
