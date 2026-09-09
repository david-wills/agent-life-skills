#!/usr/bin/env python3
"""Import Oura Ring v2 data (workouts + readiness + sleep) into a local JSON dump.

Defaults to the last 90 days (initial backfill window). Pass --since YYYY-MM-DD
or --days N to bound the delta. The script auto-refreshes the access token using
the stored refresh token, and persists rotated tokens back to the same file.

Reads tokens from ~/.openclaw/oura_tokens.json (written by authorize_oura.py).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

TOKEN_URL = "https://api.ouraring.com/oauth/token"
API_BASE = "https://api.ouraring.com/v2/usercollection"

COLLECTIONS = ("workout", "daily_readiness", "daily_sleep", "sleep")

DEFAULT_TOKEN_FILE = pathlib.Path.home() / ".openclaw" / "oura_tokens.json"

# Refresh access token if it expires within this many seconds.
REFRESH_BUFFER_SECONDS = 5 * 60

# Retry transient network failures (timeouts, 5xx, 429) before bailing out.
MAX_HTTP_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = (2, 5)


def _urlopen_with_retry(req: urllib.request.Request, timeout: float) -> bytes:
    """Open a urllib request with retry on transient errors. Returns response bytes."""
    last_exc: Exception | None = None
    for attempt in range(1, MAX_HTTP_ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            transient = exc.code in (429, 500, 502, 503, 504)
            if not transient or attempt == MAX_HTTP_ATTEMPTS:
                raise
            last_exc = exc
        except (urllib.error.URLError, socket.timeout, TimeoutError) as exc:
            if attempt == MAX_HTTP_ATTEMPTS:
                raise
            last_exc = exc
        delay = RETRY_BACKOFF_SECONDS[min(attempt - 1, len(RETRY_BACKOFF_SECONDS) - 1)]
        sys.stderr.write(
            f"[oura] transient HTTP error on attempt {attempt}/{MAX_HTTP_ATTEMPTS}: {last_exc!r}; "
            f"retrying in {delay}s\n"
        )
        time.sleep(delay)
    raise RuntimeError("unreachable")  # for type-checkers


# Shared helpers live in the repo-root lib/ (CONVENTIONS.md 2). Walk up to find it
# rather than hardcoding a path: skills are reached through a symlink.
from pathlib import Path as _P  # noqa: E402
_LIB = next(p / "lib" for p in _P(__file__).resolve().parents if (p / "lib" / "read_secret.py").is_file())
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))
from read_secret import read_secret, SecretError  # noqa: E402


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


def _read_tokens(path: pathlib.Path) -> dict[str, Any]:
    if not path.exists():
        raise SystemExit(
            f"Token file missing: {path}\n"
            "Run skills/import-oura-data/scripts/authorize_oura.py first."
        )
    return json.loads(path.read_text(encoding="utf-8"))


def _write_tokens(path: pathlib.Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
            fh.write("\n")
    except Exception:
        os.close(fd)
        raise


def _refresh_access_token(tokens: dict[str, Any], token_path: pathlib.Path) -> dict[str, Any]:
    """Use the refresh token to mint a new access token, persist back."""
    try:
        client_id = read_secret("OURA_CLIENT_ID")
        client_secret = read_secret("OURA_CLIENT_SECRET")
    except SecretError as exc:
        raise SystemExit(f"Missing Oura client credentials in 1P: {exc}")

    body = urllib.parse.urlencode(
        {
            "grant_type": "refresh_token",
            "refresh_token": tokens["refresh_token"],
            "client_id": client_id,
            "client_secret": client_secret,
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        TOKEN_URL,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        payload = json.loads(_urlopen_with_retry(req, timeout=20))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace") if exc.fp else str(exc)
        raise SystemExit(
            f"Refresh-token exchange failed: HTTP {exc.code} — {detail}\n"
            "If the refresh token is invalid, re-run authorize_oura.py."
        )

    access_token = payload.get("access_token")
    refresh_token = payload.get("refresh_token") or tokens["refresh_token"]
    expires_in = int(payload.get("expires_in") or 3600)
    if not access_token:
        raise SystemExit(f"Refresh response missing access_token: {payload}")

    expires_at = (_utc_now() + dt.timedelta(seconds=expires_in)).isoformat().replace("+00:00", "Z")
    new_tokens = {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_at": expires_at,
        "token_type": payload.get("token_type", "Bearer"),
        "scope": payload.get("scope", tokens.get("scope", "")),
    }
    _write_tokens(token_path, new_tokens)
    return new_tokens


def _ensure_fresh_access_token(token_path: pathlib.Path) -> str:
    tokens = _read_tokens(token_path)
    expires_at_raw = tokens.get("expires_at")
    needs_refresh = True
    if expires_at_raw:
        try:
            expires_at = _parse_iso(expires_at_raw)
            needs_refresh = (expires_at - _utc_now()).total_seconds() < REFRESH_BUFFER_SECONDS
        except ValueError:
            needs_refresh = True
    if needs_refresh:
        tokens = _refresh_access_token(tokens, token_path)
    return tokens["access_token"]


def _request_collection(
    access_token: str,
    collection: str,
    start_date: str,
    end_date: str,
) -> list[dict[str, Any]]:
    """Fetch every page of a collection within [start_date, end_date]."""
    items: list[dict[str, Any]] = []
    next_token: str | None = None
    while True:
        params = {"start_date": start_date, "end_date": end_date}
        if next_token:
            params["next_token"] = next_token
        url = f"{API_BASE}/{collection}?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json",
            },
        )
        try:
            payload = json.loads(_urlopen_with_retry(req, timeout=60))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise SystemExit(
                f"Oura {collection} request failed: HTTP {exc.code} {exc.reason}\n{detail}"
            ) from exc

        batch = payload.get("data") or []
        items.extend(batch)
        next_token = payload.get("next_token")
        if not next_token:
            break
    return items


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--since", help="Start date YYYY-MM-DD (inclusive). Defaults to today − 90 days.")
    p.add_argument("--days", type=int, help="Pull last N days (today − N days through today).")
    p.add_argument("--end", help="End date YYYY-MM-DD (inclusive). Default: today (UTC).")
    p.add_argument("--output-dir", default="imports/oura", help="Directory to write the export JSON into.")
    p.add_argument(
        "--token-file",
        default=str(DEFAULT_TOKEN_FILE),
        help=f"Path to the Oura tokens JSON. Default: {DEFAULT_TOKEN_FILE}",
    )
    return p.parse_args()


def _resolve_window(args: argparse.Namespace) -> tuple[str, str]:
    today = _utc_now().date()
    if args.end:
        end = dt.date.fromisoformat(args.end)
    else:
        end = today
    if args.since and args.days:
        raise SystemExit("Pass --since OR --days, not both.")
    if args.since:
        start = dt.date.fromisoformat(args.since)
    elif args.days:
        start = end - dt.timedelta(days=int(args.days))
    else:
        start = end - dt.timedelta(days=90)
    if start > end:
        raise SystemExit(f"start_date {start} is after end_date {end}.")
    return start.isoformat(), end.isoformat()


def main() -> int:
    args = parse_args()
    token_path = pathlib.Path(args.token_file).expanduser()
    start_date, end_date = _resolve_window(args)

    access_token = _ensure_fresh_access_token(token_path)

    synced_at_dt = _utc_now()
    synced_at = _iso_utc(synced_at_dt)
    stamp = synced_at.lower().replace(":", "-").replace(".", "-")

    payload: dict[str, Any] = {
        "synced_at": synced_at,
        "start_date": start_date,
        "end_date": end_date,
    }
    total = 0
    for collection in COLLECTIONS:
        items = _request_collection(access_token, collection, start_date, end_date)
        payload[collection] = {"count": len(items), "data": items}
        total += len(items)
        print(f"  {collection}: {len(items)}")

    output_dir = pathlib.Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / f"oura-data-{stamp}.json"
    json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    print(f"Wrote {json_path}")
    print(f"Window: {start_date} → {end_date}, total items: {total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
