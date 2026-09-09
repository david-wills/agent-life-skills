#!/usr/bin/env python3
"""Post a composed digest to Discord, surviving rate limits and partial runs.

The agent decides *what* the digest says; this script owns *getting it there*.
That split is the point: chunk accounting, 429 backoff and resume are
deterministic, and deterministic work does not belong in a prompt where it is
re-derived (and re-broken) on every run.

Input is a JSON array of message strings — header first, then one per section,
in send order.

    python3 post_digest.py --messages /tmp/digest.json --dry-run
    python3 post_digest.py --messages /tmp/digest.json
    python3 post_digest.py --messages /tmp/digest.json --resume
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# Shared helpers live in the repo-root lib/ (CONVENTIONS.md 2). Walk up to find
# it rather than hardcoding a path: skills are reached through a symlink.
_LIB = next(p / "lib" for p in Path(__file__).resolve().parents if (p / "lib" / "read_secret.py").is_file())
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))
import socratic_common as sc  # noqa: E402
from skill_config import cfg  # noqa: E402

DISCORD_API = "https://discord.com/api/v10"

# .resolve() matters: this skill is reached through a symlink, so without it
# _state/ lands next to the symlink instead of in the repo (CONVENTIONS.md 1).
STATE_DIR = Path(__file__).resolve().parent.parent / "_state"
PROGRESS_FILE = STATE_DIR / "last_run.json"

# Discord's hard cap is 2000. The prompt targets 1900 so a section that grows a
# few characters between drafting and posting still fits.
HARD_LIMIT = 2000

# ~20-25 messages per digest exceeds Discord's per-channel burst allowance, so
# pacing is not optional — without it the run reliably half-posts.
SEND_INTERVAL = 1.2
RETRY_AFTER_CAP = 60
NON_429_RETRY_WAIT = 30
MAX_429_ATTEMPTS = 6


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--messages", required=True,
                    help="JSON file holding an array of message strings, in send order")
    ap.add_argument("--channel", default=None,
                    help="Discord channel id (default: discord.channels.newsfeed)")
    ap.add_argument("--dry-run", action="store_true",
                    help="validate and report, send nothing")
    ap.add_argument("--resume", action="store_true",
                    help="continue the last run instead of starting from the top")
    return ap.parse_args()


def load_messages(path: str) -> list[str]:
    payload = json.loads(Path(path).read_text())
    if not isinstance(payload, list) or not all(isinstance(m, str) for m in payload):
        raise SystemExit("--messages must be a JSON array of strings")
    if not payload:
        raise SystemExit("--messages is empty")
    return payload


def validate(messages: list[str]) -> list[dict]:
    """Every message is checked before any is sent.

    The old inline version validated as it went, so an over-length section 18
    aborted a run that had already posted 17 messages — leaving a truncated
    digest in the channel and no clean way to finish it.
    """
    return [
        {"index": i, "chars": len(m), "preview": m.splitlines()[0][:60]}
        for i, m in enumerate(messages)
        if len(m) > HARD_LIMIT
    ]


# ---------- progress ------------------------------------------------------


def read_progress() -> dict:
    """Absent or corrupt progress means "start from the top", never a traceback.

    A fresh clone has no _state/, and a half-written file is likelier than usual
    here precisely because this script runs during failures.
    """
    try:
        return json.loads(PROGRESS_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def write_progress(data: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    PROGRESS_FILE.write_text(json.dumps(data, indent=2))


def resume_index(messages: list[str], channel: str) -> int:
    """Where to pick up, or 0 if the recorded run doesn't match this one.

    Keyed on message count and channel so resuming a *different* digest can't
    silently skip its first N sections.
    """
    prog = read_progress()
    if prog.get("channel") != channel or prog.get("total") != len(messages):
        return 0
    return int(prog.get("posted") or 0)


# ---------- sending -------------------------------------------------------


def post_one(channel: str, text: str, token: str) -> None:
    body = json.dumps({
        "content": text,
        "flags": 4,  # SUPPRESS_EMBEDS — 25 link previews would bury the digest
        "allowed_mentions": {"parse": []},
    }).encode("utf-8")

    for _ in range(MAX_429_ATTEMPTS):
        req = urllib.request.Request(
            f"{DISCORD_API}/channels/{channel}/messages",
            data=body,
            headers={
                "Authorization": f"Bot {token}",
                "Content-Type": "application/json; charset=utf-8",
                "User-Agent": "OpenClaw morning-news",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                if not json.loads(resp.read() or b"{}").get("id"):
                    raise RuntimeError("Discord accepted the post but returned no message id")
                return
        except urllib.error.HTTPError as exc:
            if exc.code != 429:
                raise
            # Discord tells you how long to wait; guessing is what half-posts.
            try:
                wait = float(json.loads(exc.read()).get("retry_after", 5)) + 1
            except Exception:  # noqa: BLE001 - malformed 429 body, fall back
                wait = 10
            time.sleep(min(wait, RETRY_AFTER_CAP))
    raise RuntimeError(f"rate limited {MAX_429_ATTEMPTS}x in a row")


def main() -> int:
    args = parse_args()
    messages = load_messages(args.messages)
    channel = args.channel or cfg("discord.channels.newsfeed")

    oversized = validate(messages)
    if oversized:
        print(json.dumps({"ok": False, "stage": "validate",
                          "error": f"{len(oversized)} message(s) over {HARD_LIMIT} chars",
                          "oversized": oversized}, indent=2))
        return 1

    start = resume_index(messages, channel) if args.resume else 0

    if args.dry_run:
        print(json.dumps({
            "ok": True, "dry_run": True, "channel": channel,
            "messages": len(messages), "would_start_at": start,
            "chars": [len(m) for m in messages],
        }, indent=2))
        return 0

    token = sc.get_discord_token()
    run = {
        "channel": channel,
        "total": len(messages),
        "posted": start,
        "started_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }

    for i in range(start, len(messages)):
        try:
            post_one(channel, messages[i], token)
        except Exception as exc:  # noqa: BLE001 - one retry, then report position
            time.sleep(NON_429_RETRY_WAIT)
            try:
                post_one(channel, messages[i], token)
            except Exception as exc2:  # noqa: BLE001
                write_progress(run)
                print(json.dumps({
                    "ok": False, "stage": "post", "failed_at": i,
                    "posted": run["posted"], "total": len(messages),
                    "error": f"{type(exc2).__name__}: {exc2}",
                    "first_error": f"{type(exc).__name__}: {exc}",
                    "hint": "re-run with --resume; already-posted messages are not resent",
                }, indent=2))
                return 1
        run["posted"] = i + 1
        write_progress(run)
        if i + 1 < len(messages):
            time.sleep(SEND_INTERVAL)

    run["finished_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    write_progress(run)
    print(json.dumps({"ok": True, "channel": channel,
                      "posted": run["posted"], "total": len(messages),
                      "resumed_from": start if start else None}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
