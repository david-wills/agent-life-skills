#!/usr/bin/env python3
"""Hourly: turn a ✅ on a suggestion card into a booking intent.

The sweep never books. It resolves reactions into state and hands the agent an
explicit work list — booking is an external, hard-to-reverse action and stays
behind the user's ✅ plus a deliberate call to book_reservation.py.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import restaurant_common as rc  # noqa: E402

# Shared helpers live in the repo-root lib/ (CONVENTIONS.md 2). Walk up to find it
# rather than hardcoding a path: skills are reached through a symlink.
from pathlib import Path as _P  # noqa: E402
_LIB = next(p / "lib" for p in _P(__file__).resolve().parents if (p / "lib" / "read_secret.py").is_file())
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))
import socratic_common as sc  # noqa: E402

BOOK_EMOJI = "✅"
CANCEL_EMOJI = "❌"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    conn = rc.connect()
    rows = conn.execute(
        "SELECT s.*, r.name, r.reservation_platform, r.reservation_url "
        "FROM suggestions s JOIN restaurants r ON r.id = s.restaurant_id "
        "WHERE s.state = 'offered' AND s.target_date >= date('now', '-1 day')"
    ).fetchall()

    token = sc.get_discord_token()
    to_book, declined = [], []

    for s in rows:
        reactions = sc.fetch_message_reactions(s["channel"], s["message_id"], token=token)
        picked = any(sc.strip_vs(r["name"]) == sc.strip_vs(BOOK_EMOJI) and r["by_user"]
                     for r in reactions)
        nixed = any(sc.strip_vs(r["name"]) == sc.strip_vs(CANCEL_EMOJI) and r["by_user"]
                    for r in reactions)
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
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
