#!/usr/bin/env python3
"""Post Thursday suggestion cards to Discord #restaurants.

Reads a payload on stdin so the agent supplies the blurbs:
    {"date": "2026-09-10",
     "intro": "Thursday the 10th is open...",
     "picks": [{"id": 3, "blurb": "..."}, ...]}

One card per restaurant, each seeded with ✅ — reactions are per-message, so a
batched digest would make the whole book-this contract impossible.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time
import urllib.parse
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


def card_text(row, blurb: str, date_label: str) -> str:
    bits = [b for b in (row["cuisine"], row["price"],
                        f"{row['rating']}★" if row["rating"] else None,
                        row["drive_text"] and f"{row['drive_text']} away") if b]
    lines = [f"**{row['name']}** — {' · '.join(bits)}" if bits else f"**{row['name']}**"]
    if blurb:
        lines.append(blurb)
    if row["note"]:
        lines.append(f"_Your note: {row['note']}_")
    links = [f"[Maps](<{row['maps_url']}>)"] if row["maps_url"] else []
    if row["reservation_url"]:
        plat = (row["reservation_platform"] or "book").title()
        links.append(f"[{plat}](<{row['reservation_url']}>)")
    if links:
        lines.append(" · ".join(links))
    lines.append(f"{BOOK_EMOJI} to book {date_label}, 7:00–7:30pm")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    payload = json.load(sys.stdin)
    target_date = payload["date"]
    label = dt.date.fromisoformat(target_date).strftime("%a %b %-d")
    picks = payload.get("picks") or []
    if not picks:
        print(json.dumps({"error": "no_picks"}))
        return 1

    conn = rc.connect()
    token = None if args.dry_run else sc.get_discord_token()

    if payload.get("intro") and not args.dry_run:
        sc._discord_send(rc.RESTAURANTS_CHANNEL, payload["intro"], token=token)
        time.sleep(0.4)
    elif payload.get("intro"):
        print(f"[intro]\n{payload['intro']}\n")

    posted = []
    for pick in picks:
        row = conn.execute("SELECT * FROM restaurants WHERE id=?", (pick["id"],)).fetchone()
        if row is None:
            continue
        text = card_text(row, pick.get("blurb", ""), label)
        if args.dry_run:
            print(f"[card {row['id']}]\n{text}\n")
            continue
        msgs = sc._discord_send(rc.RESTAURANTS_CHANNEL, text, token=token)
        if not msgs:
            continue
        mid = msgs[0]["id"]
        path = (f"/channels/{rc.RESTAURANTS_CHANNEL}/messages/{mid}"
                f"/reactions/{urllib.parse.quote(BOOK_EMOJI)}/@me")
        try:
            sc.discord_request("PUT", path, token=token)
        except Exception:  # noqa: BLE001 - affordance is cosmetic, never fatal
            pass
        conn.execute(
            "INSERT OR REPLACE INTO suggestions "
            "(restaurant_id, target_date, channel, message_id, state, posted_at) "
            "VALUES (?,?,?,?,'offered',?)",
            (row["id"], target_date, rc.RESTAURANTS_CHANNEL, mid, rc.now_iso()),
        )
        conn.execute("UPDATE restaurants SET last_suggested_at=? WHERE id=?",
                     (rc.now_iso(), row["id"]))
        conn.commit()
        posted.append({"restaurant_id": row["id"], "name": row["name"], "message_id": mid})
        time.sleep(0.4)

    conn.close()
    print(json.dumps({"posted": posted, "date": target_date}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
