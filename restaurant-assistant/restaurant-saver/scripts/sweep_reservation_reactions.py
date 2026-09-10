#!/usr/bin/env python3
"""Hourly: turn a ✅ on a suggestion card into a booking intent.

The sweep never books. It resolves reactions into state and hands the agent an
explicit ``to_book`` work list — booking is an external, hard-to-reverse action
and this package deliberately stops at the intent.

Only reactions by the configured owner (``discord.user_id``) count; the bot's
own seed and anyone else's clicks are ignored, so the bot can sit in a shared
server.

A card whose reactions cannot be read (deleted message, expired token) is
reported in ``errors`` and makes the exit code non-zero. It is never counted
as "no reaction".
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import restaurant_common as rc  # noqa: E402

# Shared helpers live in lib/ at the repo root; walk up so this works from any cwd.
_LIB = next((p / "lib" for p in Path(__file__).resolve().parents if (p / "lib" / "skill_config.py").is_file()), None)
if _LIB is None:
    raise SystemExit("cannot find the repo-root lib/ directory; run from a clone of the repo, not a copied file")
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

from discord import DiscordError, fetch_message_reactions, get_discord_token, strip_variation_selectors, validate_user_id  # noqa: E402
from skill_config import ConfigError, cfg  # noqa: E402

BOOK_EMOJI = "✅"
CANCEL_EMOJI = "❌"


def _has(reactions: list[dict], emoji: str) -> bool:
    want = strip_variation_selectors(emoji)
    return any(strip_variation_selectors(r["name"]) == want and r["by_user"] for r in reactions)


def main() -> int:
    ap = argparse.ArgumentParser(description="Read ✅/❌ on open suggestion cards and emit a to_book list.")
    ap.add_argument("--dry-run", action="store_true", help="Report without updating suggestion state")
    args = ap.parse_args()

    try:
        user_id = validate_user_id(cfg("discord.user_id"))
    except (ConfigError, DiscordError) as exc:
        print(f"not swept: {exc}", file=sys.stderr)
        return 2

    conn = rc.connect()
    rows = conn.execute(
        "SELECT s.*, r.name, r.reservation_platform, r.reservation_url "
        "FROM suggestions s JOIN restaurants r ON r.id = s.restaurant_id "
        "WHERE s.state = 'offered' AND s.target_date >= date('now', '-1 day')"
    ).fetchall()

    token = get_discord_token() if rows else None
    to_book, declined, errors = [], [], []

    for s in rows:
        try:
            reactions = fetch_message_reactions(s["channel"], s["message_id"], user_id, token=token)
        except DiscordError as exc:
            errors.append({"suggestion_id": s["id"], "name": s["name"],
                           "message_id": s["message_id"], "error": str(exc)})
            continue
        picked = _has(reactions, BOOK_EMOJI)
        nixed = _has(reactions, CANCEL_EMOJI)
        if picked and nixed:
            # Conflicting verbs are never resolved by guessing.
            continue
        if picked:
            to_book.append({
                "suggestion_id": s["id"],
                "restaurant_id": s["restaurant_id"],
                "name": s["name"],
                "target_date": s["target_date"],
                "platform": s["reservation_platform"],
                "reservation_url": s["reservation_url"],
                "message_id": s["message_id"],
            })
        elif nixed:
            declined.append({"suggestion_id": s["id"], "name": s["name"]})

    # One accepted pick per date — a second ✅ on the same night is ambiguous.
    by_date: dict[str, list] = {}
    for item in to_book:
        by_date.setdefault(item["target_date"], []).append(item)
    conflicts = {d: [i["name"] for i in v] for d, v in by_date.items() if len(v) > 1}
    actionable = [v[0] for d, v in by_date.items() if len(v) == 1]

    if not args.dry_run:
        for item in actionable:
            conn.execute(
                "UPDATE suggestions SET state='accepted', resolved_at=? WHERE id=?",
                (rc.now_iso(), item["suggestion_id"]),
            )
        for item in declined:
            conn.execute(
                "UPDATE suggestions SET state='declined', resolved_at=? WHERE id=?",
                (rc.now_iso(), item["suggestion_id"]),
            )
        conn.commit()
    conn.close()

    print(json.dumps({
        "checked": len(rows),
        "to_book": actionable,
        "declined": declined,
        "conflicts": conflicts,
        "errors": errors,
    }, indent=2))
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
