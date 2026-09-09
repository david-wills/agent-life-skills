#!/usr/bin/env python3
"""Apply the user's Discord reactions on #reading-list posts to Readwise Reader.

Runs daily at 00:01 PT. For every article card still in `posted` state, read
its reactions and drive Reader accordingly:

    ✅  → PATCH /v3/update/<id>/ {"location": "archive"}
    📌  → PATCH /v3/update/<id>/ {"location": "later"}
    🗑️  → DELETE /v3/delete/<id>/

Cards with no reaction stay pending until they age out (14 days). Cards with
two conflicting reactions are left alone and reported — guessing between
"archive" and "delete" is not a call this script gets to make.

Exit 0 with a JSON status line on stdout.
"""

from __future__ import annotations

import argparse
import json
from typing import Any

import reading_list_common as rl
import sys  # noqa: E402
# Shared helpers live in the repo-root lib/ (CONVENTIONS.md 2).
from pathlib import Path as _P  # noqa: E402
_LIB = next(p / "lib" for p in _P(__file__).resolve().parents if (p / "lib" / "read_secret.py").is_file())
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))
import socratic_common as sc

ACTION_LABEL = {
    "archive": "archived",
    "later": "moved to Later",
    "delete": "deleted",
}


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Apply #reading-list reactions to Readwise Reader.")
    p.add_argument("--dry-run", action="store_true", help="Report intended actions; change nothing.")
    p.add_argument("--quiet", action="store_true", help="Never post the recap message to Discord.")
    p.add_argument("--expiry-days", type=int, default=rl.REACTION_EXPIRY_DAYS,
                   help="Stop sweeping unreacted cards after this many days.")
    return p.parse_args()


def user_actions(channel: str, message_id: str, token: str) -> list[str]:
    """Distinct actions the user reacted with, de-duplicated and order-stable."""
    found: list[str] = []
    for reaction in sc.fetch_message_reactions(channel, message_id, token=token):
        if not reaction.get("by_user"):
            continue
        action = rl.ACTION_EMOJI.get(rl.strip_vs(reaction.get("name") or ""))
        if action and action not in found:
            found.append(action)
    return found


def apply_action(doc_id: str, action: str, token: str) -> tuple[bool, str]:
    if action == "archive":
        return rl.reader_patch(doc_id, {"location": "archive"}, token)
    if action == "later":
        return rl.reader_patch(doc_id, {"location": "later"}, token)
    if action == "delete":
        return rl.reader_delete(doc_id, token)
    return False, f"unknown action {action}"


def main() -> int:
    args = parse_args()
    now = rl.utc_now()

    conn = sc.connect_db(sc.MASTERCLAW_KB_DB)
    rl.ensure_tables(conn)

    rows = conn.execute(
        "SELECT doc_id, message_id, channel, title, posted_at FROM reading_list_log "
        "WHERE status = 'posted' ORDER BY posted_at ASC"
    ).fetchall()

    if not rows:
        print(json.dumps({"status": "nothing_pending", "checked": 0}))
        return 0

    reader = rl.reader_token()
    discord = sc.get_discord_token()

    counts = {"archive": 0, "later": 0, "delete": 0}
    conflicts: list[str] = []
    expired = 0
    errors: list[str] = []
    applied: list[dict[str, Any]] = []
    channel_for_recap = rows[0]["channel"]

    for row in rows:
        doc_id = row["doc_id"]
        title = row["title"] or doc_id
        try:
            actions = user_actions(row["channel"], row["message_id"], discord)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{doc_id}: reaction read {type(exc).__name__}: {exc}")
            continue

        if not actions:
            try:
                age_days = (now - rl.parse_iso(row["posted_at"])).total_seconds() / 86400
            except Exception:  # noqa: BLE001
                age_days = 0
            if age_days >= args.expiry_days and not args.dry_run:
                conn.execute(
                    "UPDATE reading_list_log SET status = 'expired', actioned_at = ? WHERE doc_id = ?",
                    (rl.iso_utc(now), doc_id),
                )
                conn.commit()
                expired += 1
            continue

        if len(actions) > 1:
            conflicts.append(f"{title[:70]} ({' + '.join(actions)})")
            continue

        action = actions[0]
        if args.dry_run:
            counts[action] += 1
            applied.append({"doc_id": doc_id, "title": title, "action": action, "detail": "dry-run"})
            continue

        ok, detail = apply_action(doc_id, action, reader)
        if not ok:
            errors.append(f"{doc_id}: {action} failed — {detail}")
            continue

        conn.execute(
            "UPDATE reading_list_log SET status = 'actioned', action = ?, action_detail = ?, "
            "actioned_at = ? WHERE doc_id = ?",
            (action, detail, rl.iso_utc(now), doc_id),
        )
        conn.commit()
        counts[action] += 1
        applied.append({"doc_id": doc_id, "title": title, "action": action, "detail": detail})

    total = sum(counts.values())
    if (total or conflicts) and not args.quiet and not args.dry_run:
        parts = [f"{counts[a]} {ACTION_LABEL[a]}" for a in ("archive", "later", "delete") if counts[a]]
        lines = ["🧹 **Reading list swept** — " + (", ".join(parts) if parts else "no actions")]
        for item in applied:
            if item["action"] == "delete":
                lines.append(f"-# 🗑️ deleted: {item['title'][:120]}")
        if conflicts:
            lines.append("")
            lines.append("⚠️ Conflicting reactions — left pending, pick one:")
            lines.extend(f"-# • {c}" for c in conflicts)
        try:
            sc.discord_request(
                "POST",
                f"/channels/{channel_for_recap}/messages",
                {"content": "\n".join(lines)[:1990], "flags": 4, "allowed_mentions": {"parse": []}},
                token=discord,
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(f"recap post: {type(exc).__name__}: {exc}")

    result = {
        "status": "swept" if total else ("conflicts" if conflicts else "no_reactions"),
        "checked": len(rows),
        "archived": counts["archive"],
        "later": counts["later"],
        "deleted": counts["delete"],
        "conflicts": len(conflicts),
        "expired": expired,
        "errors": errors,
        "dry_run": bool(args.dry_run),
    }
    print(json.dumps(result))
    return 1 if errors and total == 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
