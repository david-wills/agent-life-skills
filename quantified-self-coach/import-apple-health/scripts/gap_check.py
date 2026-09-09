"""Detect missing days in knowledge/apple-health/ and post to #errors-and-alerts.

Run via OpenClaw cron, daily ~9am PT. Logic:

  - Define the expected window: yesterday (most recent fully-elapsed day) back
    to N days ago (default 14).
  - For each expected date, check that knowledge/apple-health/YYYY-MM-DD.md
    exists.
  - If any are missing, post a single message to #errors-and-alerts naming the
    missing dates and suggesting the user re-trigger HAE.

Cooldown: skip alert if the same set-of-missing-dates was alerted in the last
ALERT_COOLDOWN_HOURS hours (state file at imports/apple-health/.last_alert.json).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable
# Shared helpers live in the repo-root lib/ (CONVENTIONS.md 2). Walk up to find it
# rather than hardcoding a path: skills are reached through a symlink.
import sys as _sys  # noqa: E402
from pathlib import Path as _P  # noqa: E402
_LIB = next(p / "lib" for p in _P(__file__).resolve().parents if (p / "lib" / "read_secret.py").is_file())
if str(_LIB) not in _sys.path:
    _sys.path.insert(0, str(_LIB))
from skill_config import cfg  # noqa: E402

SCRIPT_DIR = Path(__file__).resolve().parent
# The workspace is a fixed location, not derivable from this file's path: skills
# live in ~/Local/skills and are reached through a symlink, so .resolve() lands in
# the repo, not the workspace (this broke the email digest on 2026-09-03).
WORKSPACE = Path.home() / ".openclaw" / "workspace"

GATEWAY_ENV = Path.home() / ".openclaw" / "service-env" / "ai.openclaw.gateway.env"
ALERT_COOLDOWN_HOURS = 6
DEFAULT_WINDOW_DAYS = 14


def expected_dates(window_days: int) -> list[str]:
    today = date.today()
    return [(today - timedelta(days=i)).isoformat() for i in range(1, window_days + 1)]


def find_missing(out_dir: Path, dates: Iterable[str]) -> list[str]:
    return [d for d in dates if not (out_dir / f"{d}.md").exists()]


def _load_state(state_path: Path) -> dict:
    if not state_path.exists():
        return {}
    try:
        return json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_state(state_path: Path, state: dict) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state), encoding="utf-8")


def _discord_token() -> str | None:
    """Resolve the MasterClaw Discord bot token (env, then gateway env file)."""
    tok = os.environ.get("DISCORD_BOT_TOKEN")
    if tok:
        return tok
    try:
        m = re.search(r"DISCORD_BOT_TOKEN='([^']+)'", GATEWAY_ENV.read_text(encoding="utf-8"))
    except OSError:
        return None
    return m.group(1) if m else None


def _post_to_discord(message: str) -> None:
    """Post directly to the Discord errors channel using the MasterClaw bot token."""
    token = _discord_token()
    if not token:
        print("discord post skipped: no bot token", file=sys.stderr)
        return
    body = json.dumps({
        "content": message[:2000],
        "flags": 4,
        "allowed_mentions": {"parse": []},
    }).encode("utf-8")
    req = urllib.request.Request(
        f"https://discord.com/api/v10/channels/{cfg('discord.channels.errors')}/messages",
        data=body,
        headers={
            "Authorization": f"Bot {token}",
            "Content-Type": "application/json",
            "User-Agent": "DiscordBot (https://github.com/david-wills, 1.0)",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            if resp.status not in (200, 201):
                print(f"discord post failed: {resp.status}", file=sys.stderr)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
        print(f"discord post failed: {exc}", file=sys.stderr)


def _format_alert(missing: list[str]) -> str:
    bullets = "\n".join(f"  - {d}" for d in sorted(missing))
    return (
        "Apple Health sync gap detected. Missing dates in `knowledge/apple-health/`:\n"
        f"{bullets}\n\n"
        "Open Health Auto Export on iPhone and tap **Sync** to backfill, "
        "while the data is still in HealthKit's recent buffer."
    )


def main() -> int:
    p = argparse.ArgumentParser(description="Detect gaps in apple-health knowledge dir")
    p.add_argument("--out-dir", type=Path, default=WORKSPACE / "knowledge" / "apple-health")
    p.add_argument("--state-path", type=Path, default=WORKSPACE / "imports" / "apple-health" / ".last_alert.json")
    p.add_argument("--window-days", type=int, default=DEFAULT_WINDOW_DAYS)
    p.add_argument("--cooldown-hours", type=float, default=ALERT_COOLDOWN_HOURS)
    p.add_argument("--dry-run", action="store_true", help="Print would-be alert; do not post.")
    args = p.parse_args()

    dates = expected_dates(args.window_days)
    missing = find_missing(args.out_dir, dates)
    if not missing:
        print("ok: no gaps")
        return 0

    state = _load_state(args.state_path)
    last_alert_iso = state.get("last_alert")
    last_alert_set = set(state.get("last_missing", []))
    now = datetime.now().astimezone()

    if last_alert_iso and set(missing) == last_alert_set:
        last_alert = datetime.fromisoformat(last_alert_iso)
        if (now - last_alert).total_seconds() < args.cooldown_hours * 3600:
            print(f"cooldown: same {len(missing)} missing dates alerted {last_alert_iso}; skipping")
            return 0

    message = _format_alert(missing)
    print(f"missing {len(missing)} dates; alerting")
    print(message)
    if args.dry_run:
        return 0

    _post_to_discord(message)
    _save_state(args.state_path, {"last_alert": now.isoformat(), "last_missing": sorted(missing)})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

# ---------- Lazy config-backed module attributes -------------------------
# PEP 562: resolved on first access, not at import, so a missing key cannot break
# the import of a skill that never reads it. Inside this file, call cfg() directly.

_CONFIG_ATTRS = {"DISCORD_ERRORS_CHANNEL": "discord.channels.errors"}


def __getattr__(name: str):
    key = _CONFIG_ATTRS.get(name)
    if key is None:
        raise AttributeError("module %r has no attribute %r" % (__name__, name))
    return cfg(key)


def __dir__() -> list[str]:
    return sorted(list(globals()) + list(_CONFIG_ATTRS))

