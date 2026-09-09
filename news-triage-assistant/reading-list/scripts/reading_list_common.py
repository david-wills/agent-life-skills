#!/usr/bin/env python3
"""Shared plumbing for the reading-list intermediary stage.

The reading list sits between the newsfeed digest (surfacing) and actually
reading an article: anything the user saves into the Readwise Reader inbox gets a
3-paragraph summary posted to Discord #reading-list, plus reaction affordances
that drive Reader itself (archive / later / delete).

Reader API notes:
  - GET    /api/v3/list/           location=new, withHtmlContent=true
  - PATCH  /api/v3/update/<id>/    {"location": ...} | {"notes": ...}
  - DELETE /api/v3/delete/<id>/
"""

from __future__ import annotations

import datetime as dt
import json
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

# Shared helpers live in the repo-root lib/ (CONVENTIONS.md 2). Walk up to find it
# rather than hardcoding a path: skills are reached through a symlink.
from pathlib import Path as _P  # noqa: E402
_LIB = next(p / "lib" for p in _P(__file__).resolve().parents if (p / "lib" / "read_secret.py").is_file())
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))
import socratic_common as sc  # noqa: E402
from read_secret import read_secret  # noqa: E402
from skill_config import cfg  # noqa: E402

READER_LIST_URL = "https://readwise.io/api/v3/list/"
READER_UPDATE_URL = "https://readwise.io/api/v3/update/{doc_id}/"
READER_DELETE_URL = "https://readwise.io/api/v3/delete/{doc_id}/"
PAGE_SLEEP_SECONDS = 0.25

# Lazy — see __getattr__ at the bottom. Eager resolution here would make a missing
# key an import-time failure for every module that imports this one.

# Categories worth a prose summary. Tweets/videos/notes/highlights are skipped —
# there is nothing to compress and the model just paraphrases the title.
SUMMARIZABLE_CATEGORIES = {"article", "email", "pdf", "epub", "rss"}

# Reaction verbs. Keys are the canonical (variation-selector-stripped) emoji.
ACTION_EMOJI = {
    "✅": "archive",   # ✅
    "\U0001F4CC": "later",  # 📌
    "\U0001F5D1": "delete",  # 🗑️
}
# Display order when seeding the affordances onto a fresh post.
AFFORDANCE_EMOJI = ["✅", "\U0001F4CC", "\U0001F5D1️"]

# Below this, Reader almost certainly only captured a paywall stub.
THIN_WORD_COUNT = 400
THIN_TEXT_CHARS = 1200

# Posts that never get a reaction stop being swept after this many days.
REACTION_EXPIRY_DAYS = 14

WATERMARK_KEY = "reading_list_watermark"


# ---------- time helpers --------------------------------------------------


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso_utc(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def parse_iso(value: str) -> dt.datetime:
    text = (value or "").strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = dt.datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def strip_vs(emoji: str) -> str:
    """Drop variation selectors so 🗑️ and 🗑 compare equal."""
    return (emoji or "").replace("️", "").replace("︎", "")


# ---------- state ---------------------------------------------------------


def ensure_tables(conn: sqlite3.Connection) -> None:
    """One row per article we posted. `status` drives the reaction sweep.

    status: posted -> actioned | expired
    action: archive | later | delete (null until swept)
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS reading_list_log (
            doc_id TEXT PRIMARY KEY,
            message_id TEXT NOT NULL,
            channel TEXT NOT NULL,
            title TEXT,
            url TEXT,
            site TEXT,
            word_count INTEGER,
            thin INTEGER NOT NULL DEFAULT 0,
            summary TEXT,
            posted_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'posted',
            action TEXT,
            action_detail TEXT,
            actioned_at TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_reading_list_status ON reading_list_log(status)"
    )
    conn.commit()


def already_posted(conn: sqlite3.Connection, doc_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM reading_list_log WHERE doc_id = ?", (doc_id,)
    ).fetchone()
    return row is not None


# ---------- Reader API ----------------------------------------------------


def reader_token() -> str:
    return read_secret("readwise")


# Reader throttles the list endpoint, and it tells you for how long — in the
# `Retry-After` header, and failing that in the error body ("available in 41
# seconds"). Backfill made this script chattier (a full catalog scan plus a fetch
# per pick), so a 429 has to be waited out rather than surfaced as a run failure.
THROTTLE_RETRIES = 3
THROTTLE_MAX_WAIT = 90


def _throttle_wait(exc: urllib.error.HTTPError, body: str) -> int | None:
    header = exc.headers.get("Retry-After") if exc.headers else None
    if header and header.strip().isdigit():
        return min(int(header.strip()), THROTTLE_MAX_WAIT)
    match = re.search(r"available in (\d+)\s*second", body, re.IGNORECASE)
    if match:
        return min(int(match.group(1)), THROTTLE_MAX_WAIT)
    return None


def _reader_get(token: str, params: dict[str, str], timeout: int = 120) -> dict[str, Any]:
    url = READER_LIST_URL + ("?" + urllib.parse.urlencode(params) if params else "")
    req = urllib.request.Request(url, headers={"Authorization": f"Token {token}"})
    for attempt in range(THROTTLE_RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")[:300]
            wait = _throttle_wait(exc, body) if exc.code == 429 else None
            if wait is None or attempt == THROTTLE_RETRIES:
                raise RuntimeError(f"Reader list HTTP {exc.code}: {body}") from exc
            time.sleep(wait + 1)
    raise RuntimeError("Reader list: retries exhausted")  # unreachable, keeps type checkers happy


def fetch_inbox(token: str, updated_after: str | None, with_html: bool = True) -> list[dict[str, Any]]:
    """All `location=new` docs, optionally limited to those updated since a watermark."""
    docs: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        params: dict[str, str] = {"location": "new"}
        if updated_after:
            params["updatedAfter"] = updated_after
        if with_html:
            params["withHtmlContent"] = "true"
        if cursor:
            params["pageCursor"] = cursor
        payload = _reader_get(token, params)
        docs.extend(payload.get("results", []))
        cursor = payload.get("nextPageCursor")
        if not cursor:
            break
        time.sleep(PAGE_SLEEP_SECONDS)
    return docs


def fetch_document(token: str, doc_id: str, with_html: bool = True) -> dict[str, Any] | None:
    """One document by id. Lets a caller scan the catalog cheaply (metadata only) and
    pay for `html_content` just on the handful it actually intends to summarize."""
    params: dict[str, str] = {"id": doc_id}
    if with_html:
        params["withHtmlContent"] = "true"
    results = _reader_get(token, params).get("results", [])
    return results[0] if results else None


def reader_patch(doc_id: str, body: dict[str, Any], token: str) -> tuple[bool, str]:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        READER_UPDATE_URL.format(doc_id=doc_id),
        data=data,
        method="PATCH",
        headers={
            "Authorization": f"Token {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return True, f"HTTP {resp.status}"
    except urllib.error.HTTPError as exc:
        body_text = exc.read().decode("utf-8", errors="replace")[:200]
        return False, f"HTTP {exc.code} {exc.reason} {body_text}"
    except Exception as exc:  # noqa: BLE001 - surfaced in the audit row
        return False, f"{type(exc).__name__}: {exc}"


def reader_delete(doc_id: str, token: str) -> tuple[bool, str]:
    req = urllib.request.Request(
        READER_DELETE_URL.format(doc_id=doc_id),
        method="DELETE",
        headers={"Authorization": f"Token {token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return True, f"HTTP {resp.status}"
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return True, "HTTP 404 (already deleted)"
        body_text = exc.read().decode("utf-8", errors="replace")[:200]
        return False, f"HTTP {exc.code} {exc.reason} {body_text}"
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"


# ---------- Discord ------------------------------------------------------


def add_affordances(channel: str, message_id: str, token: str | None = None) -> None:
    """Seed ✅ 📌 🗑️ on a post so the user just clicks instead of typing an emoji."""
    token = token or sc.get_discord_token()
    for emoji in AFFORDANCE_EMOJI:
        path = (
            f"/channels/{channel}/messages/{message_id}"
            f"/reactions/{urllib.parse.quote(emoji)}/@me"
        )
        try:
            sc.discord_request("PUT", path, token=token)
        except Exception:  # noqa: BLE001 - affordances are cosmetic, never fatal
            pass
        time.sleep(0.3)


# ---------- Lazy config-backed module attributes -------------------------
# PEP 562: `rl.READING_LIST_CHANNEL` resolves on first access, not at import.
# Inside this file use cfg("discord.channels.reading_list") directly.

_CONFIG_ATTRS = {"READING_LIST_CHANNEL": "discord.channels.reading_list"}


def __getattr__(name: str):
    key = _CONFIG_ATTRS.get(name)
    if key is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return cfg(key)


def __dir__() -> list[str]:
    return sorted(list(globals()) + list(_CONFIG_ATTRS))

