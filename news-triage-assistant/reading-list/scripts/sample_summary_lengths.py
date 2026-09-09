#!/usr/bin/env python3
"""One-off A/B: render the same article at several summary lengths.

Longer than ~1900 chars can't be a plain Discord message, so the long variants
post as embeds (4096-char description cap). That means the comparison is of two
things at once, summary length AND message chrome, which is the point: you
can't have the longer summary without the embed.

Posts labelled sample cards to #reading-list. Does not touch reading_list_log,
does not write Reader notes, does not seed reaction affordances.

    python3 sample_summary_lengths.py --doc-id <id> --lengths 1500,2200,3000
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

# Shared helpers live in lib/ at the repo root; walk up so this works from any cwd.
_LIB = next((p / "lib" for p in Path(__file__).resolve().parents if (p / "lib" / "skill_config.py").is_file()), None)
if _LIB is None:
    raise SystemExit("cannot find the repo-root lib/ directory; run from a clone of the repo, not a copied file")
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))
import reading_list_common as rl  # noqa: E402
from claude_cli import claude_generate  # noqa: E402
from discord import SUPPRESS_EMBEDS, discord_request  # noqa: E402
from html_text import html_to_text  # noqa: E402
from reading_list_digest import CONTENT_CHAR_CAP, DEFAULT_MODEL, find_document, strip_meta_preamble  # noqa: E402
from skill_config import cfg  # noqa: E402

EMBED_COLOR = 0x5865F2

VARIANT_PROMPT = """You are writing a digest entry for {user}'s personal reading list. They saved \
this article to read later; your summary is the version they read when they do not have time for \
the whole thing.

Write exactly {paras} paragraphs of prose. No headings, no bullet points, no preamble, no \
sign-off. Do not restate the title. Do not address {user} or explain why the piece matters to them.

Cover, in order: what the piece is actually about and what happens in it; the substance, meaning the \
specific claims, findings, events, and any concrete numbers, names, dates, dollar figures or data \
points that carry the story; and the overarching argument, perspective or through-line the author \
is driving at, including their stance if it is an opinion piece. Prefer specifics over \
characterization.

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
{words} words. Rewrite it to fit, keeping all {paras} paragraphs and every concrete number, name \
and date. Cut characterization and hedging, not facts.

Your entire response must be the rewritten summary and nothing else, beginning with the first word \
of paragraph one. Do not report the new length, do not state that it fits, do not comment on the \
rewrite in any way: that text would be posted verbatim to a Discord card."""

# Paragraph count scales with budget: three 1000-char paragraphs is a wall on Discord.
PARAGRAPHS_FOR = [(1800, 3), (2500, 4), (99_999, 5)]

# This register runs ~6.2 chars/word; models track word counts far better than char counts.
CHARS_PER_WORD = 6.2
MAX_ATTEMPTS = 3


def paragraphs_for(target: int) -> int:
    for ceiling, paras in PARAGRAPHS_FOR:
        if target <= ceiling:
            return paras
    return 5


def generate_at_length(doc_text: str, title: str, site: str, target: int, paras: int,
                       model: str, user_name: str) -> str:
    """Generate, then hold the model to the budget. Models overshoot char targets badly."""
    words = int(target / CHARS_PER_WORD)
    prompt = VARIANT_PROMPT.format(
        user=user_name,
        paras=paras,
        words=words,
        max_words=int(words * 1.1),
        title=title,
        site=site,
        body=doc_text,
    )
    summary = strip_meta_preamble(claude_generate(prompt, model=model, timeout=240), keep=paras)
    attempt = 1
    while len(summary) > target and attempt < MAX_ATTEMPTS:
        trim = (
            prompt
            + "\n\nPREVIOUS DRAFT:\n"
            + summary
            + "\n\n"
            + TRIM_PROMPT.format(actual=len(summary), limit=target, words=words, paras=paras)
        )
        try:
            retry = strip_meta_preamble(claude_generate(trim, model=model, timeout=240), keep=paras)
        except Exception:  # noqa: BLE001 - keep the longer draft rather than losing it
            break
        if not retry:
            break
        summary = retry
        attempt += 1
    return summary


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Post length-variant samples of one article.")
    p.add_argument("--doc-id", required=True, help="Reader document id (inbox or Later).")
    p.add_argument("--lengths", default="1500,2200,3000", help="Comma-separated target char counts.")
    p.add_argument("--channel", default=None,
                   help="Discord channel id (default: discord.channels.reading_list from config.json).")
    p.add_argument("--model", default=DEFAULT_MODEL, help=f"Claude CLI model (default {DEFAULT_MODEL}).")
    p.add_argument("--dry-run", action="store_true", help="Print the variants; post nothing.")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    targets = [int(x) for x in args.lengths.split(",") if x.strip()]
    channel = args.channel or rl.reading_list_channel()
    user_name = cfg("user.display_name", "the user")

    token = rl.reader_token()
    doc = find_document(token, args.doc_id)
    if not doc:
        print(f"doc {args.doc_id} not found in Reader inbox or Later")
        return 1

    text = html_to_text(doc.get("html_content") or "")[:CONTENT_CHAR_CAP]
    title = (doc.get("title") or "Untitled").strip()
    url = (doc.get("source_url") or doc.get("url") or "").strip()
    site = (doc.get("site_name") or doc.get("source") or "").strip()

    discord = None if args.dry_run else rl.discord_token()

    for target in targets:
        paras = paragraphs_for(target)
        summary = generate_at_length(text, title, site, target, paras, args.model, user_name)
        label = f"🧪 {target}-char sample · {paras} paragraphs · actual {len(summary)}"
        print(f"\n=== {label} ===\n{summary}\n")

        if args.dry_run:
            continue

        if len(summary) + 200 <= 1900:
            content = f"-# {label}\n**{title}**\n<{url}>\n\n{summary}\n\n-# {site} · plain message"
            payload = {"content": content, "flags": SUPPRESS_EMBEDS, "allowed_mentions": {"parse": []}}
        else:
            # No SUPPRESS_EMBEDS here: that flag hides our own embed, not just link
            # previews. Safe to omit: the description carries no bare URLs.
            payload = {
                "content": f"-# {label} · embed",
                "allowed_mentions": {"parse": []},
                "embeds": [
                    {
                        "title": title[:256],
                        "url": url or None,
                        "description": summary[:4096],
                        "color": EMBED_COLOR,
                        "footer": {"text": f"{site} · ✅ archive · 📌 later · 🗑️ delete"},
                    }
                ],
            }
        discord_request("POST", f"/channels/{channel}/messages", payload, token=discord)
        time.sleep(0.6)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
