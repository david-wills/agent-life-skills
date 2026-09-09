"""Detect missing days in the Apple Health folder and post to the errors channel.

Run daily, mid-morning, after the phone has had its nightly chance to sync:

  - The expected window is yesterday (the most recent fully-elapsed day) back
    to N days ago (default 14).
  - A date is missing when <data_root>/knowledge/apple-health/YYYY-MM-DD.md
    does not exist.
  - If any are missing, post one message to ``discord.channels.errors`` naming
    them and asking for a manual re-sync from the phone.

Cooldown: the same set of missing dates is not re-alerted within
ALERT_COOLDOWN_HOURS (state at <data_root>/imports/apple-health/.last_alert.json).
State is written only after a successful post, so a failed post retries next run.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable

# Shared helpers live in lib/ at the repo root; walk up so this works from any cwd.
_LIB = next((p / "lib" for p in Path(__file__).resolve().parents if (p / "lib" / "skill_config.py").is_file()), None)
if _LIB is None:
    raise SystemExit("cannot find the repo-root lib/ directory; run from a clone of the repo, not a copied file")
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

from skill_config import ConfigError, cfg, data_root  # noqa: E402

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


def _format_alert(missing: list[str]) -> str:
    bullets = "\n".join(f"  - {d}" for d in sorted(missing))
    return (
        "Apple Health sync gap detected. Missing dates:\n"
        f"{bullets}\n\n"
        "Open Health Auto Export on the phone and tap **Sync** to backfill, "
        "while the data is still in HealthKit's recent buffer."
    )


def main() -> int:
    p = argparse.ArgumentParser(description="Detect gaps in the per-day Apple Health files.")
    p.add_argument("--out-dir", type=Path, default=None,
                   help="Per-day markdown directory. Default: <data_root>/knowledge/apple-health")
    p.add_argument("--state-path", type=Path, default=None,
                   help="Cooldown state file. Default: <data_root>/imports/apple-health/.last_alert.json")
    p.add_argument("--window-days", type=int, default=DEFAULT_WINDOW_DAYS,
                   help=f"How many days back to check (default {DEFAULT_WINDOW_DAYS}).")
    p.add_argument("--cooldown-hours", type=float, default=ALERT_COOLDOWN_HOURS,
                   help=f"Do not re-alert the same missing set within this many hours (default {ALERT_COOLDOWN_HOURS}).")
    p.add_argument("--dry-run", action="store_true", help="Print the would-be alert; do not post or save state.")
    args = p.parse_args()

    out_dir = args.out_dir or data_root() / "knowledge" / "apple-health"
    state_path = args.state_path or data_root() / "imports" / "apple-health" / ".last_alert.json"

    dates = expected_dates(args.window_days)
    missing = find_missing(out_dir, dates)
    if not missing:
        print("ok: no gaps")
        return 0

    state = _load_state(state_path)
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

    try:
        channel = cfg("discord.channels.errors")
    except ConfigError as exc:
        print(f"not posted: {exc}", file=sys.stderr)
        return 2

    from discord import DiscordError, send_message

    try:
        send_message(channel, message)
    except DiscordError as exc:
        print(f"discord post failed: {exc}", file=sys.stderr)
        return 1

    _save_state(state_path, {"last_alert": now.isoformat(), "last_missing": sorted(missing)})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
