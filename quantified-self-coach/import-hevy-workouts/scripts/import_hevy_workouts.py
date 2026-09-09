#!/usr/bin/env python3
"""Import workouts from the Hevy API into a local JSON dump.

Defaults to a full historical pull. Pass --since <ISO> to bound the delta
against each workout's `updated_at` field.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

# Shared helpers live in the repo-root lib/ (CONVENTIONS.md 2).
from pathlib import Path as _P  # noqa: E402
_LIB = next(p / "lib" for p in _P(__file__).resolve().parents if (p / "lib" / "read_secret.py").is_file())
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))


API_URL = "https://api.hevyapp.com/v1/workouts"
PAGE_SIZE = 10  # Hevy's documented max for this endpoint.


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _iso_utc(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_iso(value: str) -> dt.datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = dt.datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _slug(value: str) -> str:
    cleaned = []
    for ch in value.lower():
        cleaned.append(ch if ch.isalnum() else "-")
    slug = "".join(cleaned).strip("-")
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug or "hevy-import"


def _token_from_env() -> str | None:
    return os.getenv("HEVY_API_KEY") or os.getenv("HEVY_TOKEN")


def _token_from_secret_store() -> str | None:
    try:
        from read_secret import read_secret  # type: ignore[import-not-found]
        value = read_secret("HEVY_API_KEY")
        return value or None
    except Exception:
        return None


def _resolve_token(cli_token: str | None) -> str | None:
    return cli_token or _token_from_env() or _token_from_secret_store()


def _request_json(token: str, page: int) -> dict[str, Any]:
    params = {"page": str(page), "pageSize": str(PAGE_SIZE)}
    url = API_URL + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"api-key": token, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"Hevy request failed: HTTP {exc.code} {exc.reason}\n{detail}") from exc


def fetch_workouts(token: str, since: dt.datetime | None) -> list[dict[str, Any]]:
    """Fetch workouts page-by-page, newest first. Stops early if `since` is set
    and the current page contains only workouts older than the cutoff.
    """
    workouts: list[dict[str, Any]] = []
    page = 1
    while True:
        payload = _request_json(token, page)
        batch = payload.get("workouts") or []
        if not batch:
            break

        if since is not None:
            kept_any = False
            stop = False
            for w in batch:
                updated_raw = w.get("updated_at") or w.get("start_time") or ""
                try:
                    updated = _parse_iso(updated_raw) if updated_raw else None
                except ValueError:
                    updated = None
                if updated is None or updated >= since:
                    workouts.append(w)
                    kept_any = True
                else:
                    stop = True
            if stop and not kept_any:
                break
            if stop:
                break
        else:
            workouts.extend(batch)

        page_count = payload.get("page_count") or 0
        if page >= page_count:
            break
        page += 1

    return workouts


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Import Hevy workouts into a local JSON dump.")
    parser.add_argument("--since", help="ISO 8601 timestamp; only workouts updated at/after this are kept.")
    parser.add_argument("--output-dir", default="imports/hevy", help="Directory to write the import JSON into.")
    parser.add_argument("--token", help="Hevy API key. Defaults to HEVY_API_KEY env or 1Password.")
    parser.add_argument(
        "--watermark-file",
        help=(
            "Path to a JSON state file. If present, the prior synced_at minus 1h"
            " is combined (via min) with --since to produce the actual cutoff."
            " Updated on successful run."
        ),
    )
    return parser.parse_args()


def _read_watermark(path: Path) -> dt.datetime | None:
    if not path.exists():
        return None
    try:
        state = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    raw = state.get("last_synced_at")
    if not raw:
        return None
    try:
        return _parse_iso(raw) - dt.timedelta(hours=1)
    except ValueError:
        return None


def main() -> int:
    args = parse_args()
    token = _resolve_token(args.token)
    if not token:
        print("Missing Hevy API key. Set HEVY_API_KEY or store it in 1Password (OpenClaw vault).", file=sys.stderr)
        return 2

    since = _parse_iso(args.since) if args.since else None
    watermark_path = Path(args.watermark_file) if args.watermark_file else None
    if watermark_path is not None:
        wm = _read_watermark(watermark_path)
        if wm is not None:
            since = min(since, wm) if since else wm
    synced_at_dt = _utc_now()
    synced_at = _iso_utc(synced_at_dt)

    workouts = fetch_workouts(token, since)

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = _slug(synced_at)
    json_path = output_dir / f"hevy-workouts-{stamp}.json"

    payload = {
        "synced_at": synced_at,
        "since": _iso_utc(since) if since else None,
        "workout_count": len(workouts),
        "workouts": workouts,
    }
    json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    if watermark_path is not None:
        watermark_path.parent.mkdir(parents=True, exist_ok=True)
        watermark_path.write_text(
            json.dumps({"last_synced_at": synced_at}, indent=2) + "\n",
            encoding="utf-8",
        )

    print(f"Wrote {json_path}")
    print(f"Imported {len(workouts)} workouts")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
