#!/usr/bin/env python3
"""Shared helpers for the Socratic loop: DB access, rotation state, Slack I/O."""

from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import re
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import Any

from skill_config import cfg

# MasterClaw's personal knowledge base. Named for its owner, not "DEFAULT",
# because this module is shared by both agents (CONVENTIONS.md 2) and a neutral
# name invites the wrong one to use it -- see connect_db below.
MASTERCLAW_KB_ROOT = pathlib.Path.home() / ".openclaw" / "workspace" / "knowledge"
MASTERCLAW_KB_DB = MASTERCLAW_KB_ROOT / "index.db"  # lint-conv:allow — the KB itself: cross-skill workspace data, not skill state
OPENCLAW_CONFIG = pathlib.Path.home() / ".openclaw" / "openclaw.json"

QUESTION_TYPES = ("recall", "revision", "bridge", "gap")
SKIP_RETIRE_THRESHOLD = 5
GLOBAL_PAUSE_THRESHOLD = 3

# Action-aware cooldown for resurfacing entries (Phase 3).
# Anchored to surface time for skip; to capture time for substantive engagement.
SKIP_COOLDOWN_DAYS = 30
ANSWERED_COOLDOWN_DAYS = 180  # also applied to feedback / mixed (substantive engagement)

# Config-backed identifiers are LAZY (see __getattr__ at the bottom of this file).
# Resolving them at module scope meant one missing key raised ConfigError during
# import, taking down every skill that imports this module — including skills that
# never read the missing key. reading-list died on slack.bot_user_id (2026-09-03).
# Now a skill only fails on a key it actually touches.

# --- Discord transport (migrated off Slack 2026-07-29) -------------------
# User-facing KB posts + reply-capture now run in Discord #knowledge-hub.
# The question is posted as a channel message; a thread is opened off it; the user
# replies IN the thread; the sweeps read thread messages back. Error/ops posts
# still go to Slack via slack_api()/get_bot_token() below.
DISCORD_API = "https://discord.com/api/v10"
DISCORD_SERVICE_ENV = pathlib.Path.home() / ".openclaw" / "service-env" / "ai.openclaw.gateway.env"
DISCORD_MAX_CHARS = 2000
DISCORD_THREAD_ARCHIVE_MIN = 4320  # auto-archive after 3 days of inactivity

DELETE_TRIGGER_PHRASES = (
    "kill this",
    "get rid of this",
)
DELETE_TRIGGER_WORDS = ("delete", "purge", "remove")
_YES_TOKENS = {"yes", "y", "yeah", "yep", "yup", "confirm", "do it", "go ahead", "go", "proceed"}
_NO_TOKENS = {"no", "n", "nope", "nah", "cancel", "abort", "stop", "never mind", "nvm"}


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def strip_vs(text: str) -> str:
    """Strip variation selectors (U+FE0E, U+FE0F) from emoji for normalization."""
    return text.replace('︎', '').replace('️', '')


def is_delete_intent(text: str) -> bool:
    """True iff `text` reads as a request to delete this surfaced entry.

    Matches the trigger phrases anywhere (case-insensitive) and the trigger
    words on word boundaries. Conservative on bare 'remove' to avoid false
    positives from e.g. "I'd remove that paragraph" — but accepts forms like
    "remove this", "remove it", "please remove".
    """
    norm = _normalize(text)
    if not norm:
        return False
    for phrase in DELETE_TRIGGER_PHRASES:
        if phrase in norm:
            return True
    for word in DELETE_TRIGGER_WORDS:
        if re.search(rf"\b{re.escape(word)}\b", norm):
            return True
    return False


def is_yes_confirm(text: str) -> bool:
    norm = _normalize(text)
    return norm in _YES_TOKENS


def is_no_cancel(text: str) -> bool:
    norm = _normalize(text)
    return norm in _NO_TOKENS


_NONE_TOKENS = {"none", "0", "nothing", "skip none", "delete none"}


def is_none_keyword(text: str) -> bool:
    """True iff the user replied with the literal `none` keyword (delete zero items)."""
    norm = _normalize(text)
    return norm in _NONE_TOKENS


def parse_summary_picks(text: str, max_n: int) -> tuple[str, Any]:
    """Parse a wizard reply into (kind, value).

    kind is one of:
      - "all"      → delete every item
      - "none"     → delete zero items, advance to summary-confirm
      - "cancel"   → abort the wizard
      - "picks"    → value is a sorted unique list of 1-based indices
      - "error"    → value is a human-readable error to post back

    Accepts numbers, comma- or whitespace-separated lists, ranges (`1-3`),
    and the keywords `all` / `none` / `cancel`. The keywords must stand alone;
    mixing them with picks is an error.
    """
    norm = _normalize(text)
    if not norm:
        return ("error", "Reply was empty. Reply with `1,3,5` / `1-3` / `all` / `none` / `cancel`.")
    if norm == "all":
        return ("all", None)
    if is_none_keyword(text):
        return ("none", None)
    if is_no_cancel(text):
        return ("cancel", None)

    picks: set[int] = set()
    tokens = re.split(r"[,\s]+", norm)
    for tok in tokens:
        if not tok:
            continue
        m_range = re.match(r"^(\d+)-(\d+)$", tok)
        if m_range:
            a, b = int(m_range.group(1)), int(m_range.group(2))
            if a > b:
                a, b = b, a
            for i in range(a, b + 1):
                if i < 1 or i > max_n:
                    return ("error", f"Number `{i}` is out of range (valid: 1–{max_n}).")
                picks.add(i)
            continue
        m_single = re.match(r"^(\d+)$", tok)
        if m_single:
            i = int(m_single.group(1))
            if i < 1 or i > max_n:
                return ("error", f"Number `{i}` is out of range (valid: 1–{max_n}).")
            picks.add(i)
            continue
        return (
            "error",
            f"Couldn't parse `{tok}`. Reply with numbers like `1,3,5` or `1-3`, or `all` / `none` / `cancel`.",
        )
    if not picks:
        return (
            "error",
            "No numbers found. Reply with `1,3,5` / `1-3` / `all` / `none` / `cancel`.",
        )
    return ("picks", sorted(picks))


# Phase 4 — Reader inbox triage action vocabulary.
TRIAGE_ACTIONS = ("archive", "later", "delete", "keep")


def classify_triage_reply(text: str, max_n: int) -> tuple[dict[int, str], list[str]]:
    """Parse a Reader-triage reply into (action_map, errors).

    Accepts forms like:
      `1=archive 2=later 3-5=delete 6-10=keep`
      `1=archive, 2=later, 3-5=delete`
      `1 = archive; 2 = later`

    Returns:
      action_map: dict[int, str] of 1-based index → action (one of TRIAGE_ACTIONS).
                  Later assignments to the same index override earlier ones.
      errors:     list of human-readable errors (unknown actions, out-of-range
                  indices, malformed tokens). Caller should re-prompt if non-empty.
    """
    actions: dict[int, str] = {}
    errors: list[str] = []
    norm = (text or "").strip()
    if not norm:
        errors.append("Reply was empty. Format: `1=archive 2=later 3-5=delete 6-10=keep`.")
        return actions, errors

    # Split on whitespace, commas, semicolons. Keep `=` and digits/letters intact.
    tokens = re.split(r"[\s,;]+", norm)
    for tok in tokens:
        if not tok:
            continue
        # Allow whitespace around `=`, but we already split on whitespace, so
        # just trim residual `=`-padding within the token itself.
        if "=" not in tok:
            errors.append(f"Couldn't parse `{tok}` — expected `<index>=<action>` (e.g. `1=archive`).")
            continue
        lhs, _, rhs = tok.partition("=")
        lhs = lhs.strip()
        rhs = rhs.strip().lower()
        if not lhs or not rhs:
            errors.append(f"Couldn't parse `{tok}` — expected `<index>=<action>`.")
            continue
        if rhs not in TRIAGE_ACTIONS:
            errors.append(
                f"Unknown action `{rhs}` in `{tok}`. Valid actions: archive / later / delete / keep."
            )
            continue
        m_range = re.match(r"^(\d+)-(\d+)$", lhs)
        if m_range:
            a, b = int(m_range.group(1)), int(m_range.group(2))
            if a > b:
                a, b = b, a
            for i in range(a, b + 1):
                if i < 1 or i > max_n:
                    errors.append(f"Index `{i}` is out of range (valid: 1–{max_n}).")
                    continue
                actions[i] = rhs
            continue
        m_single = re.match(r"^(\d+)$", lhs)
        if m_single:
            i = int(m_single.group(1))
            if i < 1 or i > max_n:
                errors.append(f"Index `{i}` is out of range (valid: 1–{max_n}).")
                continue
            actions[i] = rhs
            continue
        errors.append(f"Couldn't parse index in `{tok}` — use a number or range like `3-5`.")
    return actions, errors


def iso_utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def iso_utc_plus_days(days: int) -> str:
    """`now()` + `days`, ISO-8601 with `Z` suffix — same shape as iso_utc_now()."""
    return (
        dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=days)
    ).isoformat().replace("+00:00", "Z")


def _shift_iso_days(iso_ts: str, days: int) -> str | None:
    """Add `days` to an ISO-8601 timestamp string. Returns None on parse failure."""
    if not iso_ts:
        return None
    try:
        parsed = dt.datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return (parsed + dt.timedelta(days=days)).isoformat().replace("+00:00", "Z")


def ensure_next_eligible_column(conn: sqlite3.Connection) -> None:
    """Phase 3 migration: add `next_eligible_at` to `socratic_surfaced` and
    backfill existing rows with `last_surfaced_at + SKIP_COOLDOWN_DAYS`.

    Idempotent — safe to call from any entry point (`ask_socratic.run()`,
    `capture_answer.capture()`).
    """
    try:
        conn.execute("ALTER TABLE socratic_surfaced ADD COLUMN next_eligible_at TEXT")
    except sqlite3.OperationalError:
        # Column already exists, or the table itself doesn't yet — both fine.
        pass

    # Backfill is a no-op once everything has a value, but cheap to attempt.
    try:
        rows = conn.execute(
            """
            SELECT entry_id, last_surfaced_at
              FROM socratic_surfaced
             WHERE next_eligible_at IS NULL
               AND last_surfaced_at IS NOT NULL
            """
        ).fetchall()
    except sqlite3.OperationalError:
        return  # table doesn't exist yet on a fresh DB; nothing to backfill

    for row in rows:
        entry_id = row[0] if not isinstance(row, sqlite3.Row) else row["entry_id"]
        last_surfaced_at = row[1] if not isinstance(row, sqlite3.Row) else row["last_surfaced_at"]
        new_eligible = _shift_iso_days(last_surfaced_at, SKIP_COOLDOWN_DAYS)
        if new_eligible is None:
            continue
        conn.execute(
            "UPDATE socratic_surfaced SET next_eligible_at = ? WHERE entry_id = ?",
            (new_eligible, entry_id),
        )


def connect_db(db_path: pathlib.Path) -> sqlite3.Connection:
    """Open a knowledge-base SQLite file. db_path is REQUIRED (CONVENTIONS.md 8).

    It used to default to MASTERCLAW_KB_DB. This module lives in the shared
    lib/ and the work-side agent imports it too, so that default meant one of
    its scripts calling connect_db() with no argument silently opened the
    personal KB -- the cross-agent boundary hole 9553e6d closed at the import
    layer,
    still open one function call lower. Requiring the argument moves the
    decision to the call site, where a reviewer and lint CONV8 can both see it.
    """
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def get_state(conn: sqlite3.Connection, key: str, default: Any = None) -> Any:
    row = conn.execute("SELECT value FROM socratic_state WHERE key = ?", (key,)).fetchone()
    if row is None:
        return default
    try:
        return json.loads(row["value"])
    except json.JSONDecodeError:
        return row["value"]


def set_state(conn: sqlite3.Connection, key: str, value: Any) -> None:
    payload = json.dumps(value)
    conn.execute(
        "INSERT INTO socratic_state (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, payload),
    )


def get_bot_token() -> str:
    data = json.loads(OPENCLAW_CONFIG.read_text(encoding="utf-8"))
    token = data.get("channels", {}).get("slack", {}).get("accounts", {}).get("main", {}).get("botToken")
    if not token:
        raise RuntimeError("Slack bot token not found in openclaw.json")
    return token


def slack_api(method: str, payload: dict[str, Any], token: str | None = None) -> dict[str, Any]:
    token = token or get_bot_token()
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"https://slack.com/api/{method}",
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        },
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        result = json.loads(resp.read())
    if not result.get("ok"):
        raise RuntimeError(f"Slack API {method} failed: {result.get('error')} ({result})")
    return result


def slack_get(method: str, params: dict[str, Any], token: str | None = None) -> dict[str, Any]:
    token = token or get_bot_token()
    qs = "&".join(f"{k}={urllib.request.quote(str(v))}" for k, v in params.items())
    url = f"https://slack.com/api/{method}?{qs}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        result = json.loads(resp.read())
    if not result.get("ok"):
        raise RuntimeError(f"Slack API {method} failed: {result.get('error')} ({result})")
    return result


_MD_LINK_RE = re.compile(r"\[([^\]\n]+?)\]\(<?([^)\s<>]+)>?\)")


def markdown_links_to_slack(text: str) -> str:
    """Convert Markdown `[label](url)` to Slack mrkdwn `<url|label>`.

    Kept for any Slack-side (ops) posting. The KB user-facing posts now go to
    Discord, which renders `[label](url)` natively — see slack_mrkdwn_to_discord().
    """
    if not text:
        return text
    return _MD_LINK_RE.sub(lambda m: f"<{m.group(2).strip()}|{m.group(1).strip()}>", text)


_SLACK_LINK_RE = re.compile(r"<(https?://[^|>\s]+)\|([^>]+)>")
_SLACK_BARE_LINK_RE = re.compile(r"<(https?://[^|>\s]+)>")
# Slack single-asterisk bold at word boundaries; avoids bullets ("* item"),
# mid-word asterisks, and existing `**double**`.
_SLACK_BOLD_RE = re.compile(r"(?<![\w*])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![\w*])")


# Slack :shortcode: emoji the KB posters use -> unicode (Discord renders
# shortcodes as literal text in bot messages, so pre-substitute).
EMOJI_SHORTCODES = {
    ":warning:": "⚠️", ":white_check_mark:": "✅", ":x:": "❌",
    ":one:": "1️⃣", ":two:": "2️⃣", ":three:": "3️⃣",
    ":no_entry_sign:": "🚫", ":next_track_button:": "⏭️", ":brain:": "🧠",
    ":leftwards_arrow_with_hook:": "↩️", ":wastebasket:": "🗑️", ":link:": "🔗",
    ":books:": "📚", ":pencil:": "✏️", ":pause_button:": "⏸️", ":memo:": "📝",
    ":open_book:": "📖", ":mag:": "🔍", ":information_source:": "ℹ️",
    ":hourglass:": "⏳", ":construction:": "🚧", ":arrows_counterclockwise:": "🔄",
}


def slack_mrkdwn_to_discord(text: str) -> str:
    """Convert the Slack-mrkdwn dialect the KB posters emit into Discord markdown.

    - `<url|label>` -> `[label](url)`      (Slack link -> Discord masked link)
    - `<url>`       -> `url`
    - `*bold*`      -> `**bold**`          (single-asterisk bold is *italic* in Discord)
    - `:shortcode:` -> unicode emoji       (known KB shortcodes only)

    `_italic_`, `> quotes`, `` `code` ``, and native `[label](url)` pass through.
    Applied once at the Discord transport layer so individual posters keep their
    existing Slack-flavored formatting.
    """
    if not text:
        return text
    text = _SLACK_LINK_RE.sub(lambda m: f"[{m.group(2).strip()}]({m.group(1).strip()})", text)
    text = _SLACK_BARE_LINK_RE.sub(lambda m: m.group(1), text)
    text = _SLACK_BOLD_RE.sub(lambda m: f"**{m.group(1)}**", text)
    for code, uni in EMOJI_SHORTCODES.items():
        if code in text:
            text = text.replace(code, uni)
    return text


def fetch_message_reactions(channel: str, message_id: str, token: str | None = None) -> list[dict[str, Any]]:
    """Return a message's reactions as ``[{name, count, by_user}]``.

    Discord message reactions carry ``{emoji:{name}, count, me}`` but not the
    user list. In a solo guild the bot never self-reacts in the digest flow, so
    ``by_user`` = the reaction exists and wasn't added solely by the bot.

    """
    token = token or get_discord_token()
    try:
        msg = discord_request("GET", f"/channels/{channel}/messages/{message_id}", token=token)
    except Exception:
        return []
    out: list[dict[str, Any]] = []
    for r in msg.get("reactions") or []:
        name = (r.get("emoji") or {}).get("name")
        if not name:
            continue
        count = int(r.get("count") or 0)
        by_user = count > (1 if r.get("me") else 0)
        out.append({"name": name, "count": count, "by_user": by_user})
    return out


# ---------------------------------------------------------------------------
# Discord transport (user-facing KB posts + reply-capture)
# ---------------------------------------------------------------------------

def get_discord_token() -> str:
    tok = os.environ.get("DISCORD_BOT_TOKEN")
    if tok:
        return tok
    try:
        env_text = DISCORD_SERVICE_ENV.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"DISCORD_BOT_TOKEN not in env and service-env unreadable: {exc}")
    m = re.search(r"DISCORD_BOT_TOKEN='([^']+)'", env_text)
    if not m:
        raise RuntimeError("DISCORD_BOT_TOKEN not found (env or service-env)")
    return m.group(1)


def discord_request(method: str, path: str, payload: dict[str, Any] | None = None,
                    token: str | None = None) -> Any:
    token = token or get_discord_token()
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        f"{DISCORD_API}{path}",
        data=data,
        headers={
            "Authorization": f"Bot {token}",
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": "OpenClaw knowledge-base",
        },
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"Discord {method} {path} failed: HTTP {exc.code} {body[:400]}")


def split_for_discord(text: str, limit: int = 1900) -> list[str]:
    """Split text into <=limit chunks at paragraph, then line, boundaries.
    Never truncates — long KB bodies are split, not cut (see
    feedback_kb_slack_full_quotes.md)."""
    text = text or ""
    if len(text) <= limit:
        return [text] if text else [""]
    chunks: list[str] = []
    cur = ""

    def flush() -> None:
        nonlocal cur
        if cur:
            chunks.append(cur)
            cur = ""

    for para in text.split("\n\n"):
        piece = ("\n\n" + para) if cur else para
        if len(cur) + len(piece) <= limit:
            cur += piece
            continue
        flush()
        if len(para) <= limit:
            cur = para
            continue
        # paragraph alone too long: split on lines, then hard-split
        for line in para.split("\n"):
            add = ("\n" + line) if cur else line
            if len(cur) + len(add) <= limit:
                cur += add
                continue
            flush()
            while len(line) > limit:
                chunks.append(line[:limit])
                line = line[limit:]
            cur = line
    flush()
    return chunks or [""]


def _thread_title(text: str, fallback: str = "KB") -> str:
    """Derive a <=100 char Discord thread name from the first line of a post."""
    first = (text or "").strip().splitlines()[0] if (text or "").strip() else fallback
    first = re.sub(r"[*_`>#~|\[\]()]", "", first).strip()  # strip md noise
    first = re.sub(r"\s+", " ", first)
    return (first[:96] + "…") if len(first) > 97 else (first or fallback)


def _discord_send(channel_id: str, text: str, token: str | None = None) -> list[dict[str, Any]]:
    """Post text (auto-split) to a Discord channel/thread. Returns raw message objects."""
    token = token or get_discord_token()
    text = slack_mrkdwn_to_discord(text)
    sent: list[dict[str, Any]] = []
    for chunk in split_for_discord(text):
        msg = discord_request(
            "POST", f"/channels/{channel_id}/messages",
            {"content": chunk, "flags": 4, "allowed_mentions": {"parse": []}},
            token=token,
        )
        sent.append(msg)
        wait_briefly(250)
    return sent


def post_channel_message(channel: str, text: str, token: str | None = None,
                         thread_name: str | None = None) -> dict[str, Any]:
    """Post a KB message to #knowledge-hub and open a reply-capture thread off it.

    Returns a dict whose ``ts`` is the Discord THREAD id (the key the sweeps use
    to read replies) — mirroring the old Slack contract where ``ts`` was the
    thread anchor. ``id`` is the root message id; ``channel`` is the parent.
    Overflow chunks beyond the first are posted inside the thread.
    """
    token = token or get_discord_token()
    chunks = split_for_discord(slack_mrkdwn_to_discord(text))
    root = discord_request(
        "POST", f"/channels/{channel}/messages",
        {"content": chunks[0], "flags": 4, "allowed_mentions": {"parse": []}},
        token=token,
    )
    root_id = root["id"]
    thread = discord_request(
        "POST", f"/channels/{channel}/messages/{root_id}/threads",
        {"name": _thread_title(thread_name or text), "auto_archive_duration": DISCORD_THREAD_ARCHIVE_MIN},
        token=token,
    )
    thread_id = thread["id"]
    for chunk in chunks[1:]:
        discord_request(
            "POST", f"/channels/{thread_id}/messages",
            {"content": chunk, "flags": 4, "allowed_mentions": {"parse": []}},
            token=token,
        )
        wait_briefly(250)
    return {"ts": thread_id, "id": root_id, "thread_id": thread_id, "channel": channel}


def post_thread_reply(channel: str, thread_ts: str, text: str, token: str | None = None) -> dict[str, Any]:
    """Post a follow-up into an existing capture thread (``thread_ts`` == thread id)."""
    sent = _discord_send(thread_ts, text, token=token)
    first = sent[0] if sent else {}
    return {"ts": thread_ts, "id": first.get("id")}


def fetch_thread_replies(channel: str, thread_ts: str, token: str | None = None) -> list[dict[str, Any]]:
    """Read a capture thread's messages (chronological), normalized to a stable
    shape the sweeps consume: id, ts, user, text, is_bot, from_user.

    ``thread_ts`` is the Discord thread id. The listing may include a bot
    "starter" reference message; it's is_bot=True so from_user filtering drops
    it. Only the operator's messages (from_user=True) are treated as answers.

    """
    token = token or get_discord_token()
    raw = discord_request("GET", f"/channels/{thread_ts}/messages?limit=100", token=token)
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    _user_id = cfg("discord.user_id")
    for m in reversed(raw):  # Discord returns newest-first
        author = m.get("author") or {}
        uid = str(author.get("id", ""))
        out.append({
            "id": m.get("id"),
            "ts": m.get("id"),
            "user": uid,
            "text": m.get("content", ""),
            "is_bot": bool(author.get("bot")),
            "from_user": uid == _user_id,
        })
    return out


def claude_generate(prompt: str, model: str = "claude-haiku-4-5", timeout: int = 120) -> str:
    """Generate text via the local Claude CLI. Returns stdout trimmed."""
    proc = subprocess.run(
        [
            "claude",
            "--print",
            "--permission-mode",
            "bypassPermissions",
            "--model",
            model,
        ],
        input=prompt,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if proc.returncode != 0:
        # The CLI reports some failures on stdout with an empty stderr — most
        # notably "Prompt is too long" — so a stderr-only message shows up as a
        # blank error. Report whichever stream actually said something.
        err = (proc.stderr or "").strip()
        out = (proc.stdout or "").strip()
        detail = " | ".join(p[:400] for p in (err, out) if p) or "(no output on stdout or stderr)"
        raise RuntimeError(f"Claude CLI failed ({proc.returncode}): {detail}")
    return proc.stdout.strip()


# Claude regularly emits \' inside JSON strings, which is not a valid escape and
# kills json.loads outright. Recorded as a standing gotcha in MEMORY; the fix is
# to make the parser tolerant rather than to keep retrying the model.
_BAD_ESCAPE = re.compile(r"\\(['`])")


def _loads_tolerant(text: str) -> dict[str, Any]:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return json.loads(_BAD_ESCAPE.sub(r"\1", text))


def extract_json(raw: str) -> dict[str, Any]:
    """Pull a JSON object out of Claude CLI output (sometimes wrapped in prose)."""
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:]
    try:
        return _loads_tolerant(raw)
    except json.JSONDecodeError:
        pass
    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError(f"no JSON object found in: {raw[:200]}")
    return _loads_tolerant(raw[start : end + 1])


CLASSIFY_PROMPT = """You are classifying a Slack reply to a daily Socratic question posted to {user}'s personal knowledge base.

## The question {user} was asked

Question type: {qtype}
Question text:
---
{question_text}
---

## {user}'s reply

---
{reply_text}
---

## Your job

Classify the reply. Output ONLY valid JSON (no prose, no code fences). Schema:

{{
  "type": "answer" | "skip" | "feedback" | "mixed",
  "answer_text": string or null,
  "feedback_text": string or null,
  "retire_entry": boolean,
  "retire_type": boolean,
  "style_note": string or null,
  "suggested_instead": string or null
}}

Definitions:
- "answer": a substantive response that engages with the question (restatement, takeaway, position, reflection).
- "skip": a non-engagement signal — "skip", "pass", "not now", "idk", or empty agreement ("yeah").
- "feedback": a critique of the PROMPT itself (wording, topic choice, entry choice) with no real answer.
- "mixed": contains BOTH a real answer AND a critique/suggestion about the prompt.

Set retire_entry=true ONLY if {user} explicitly says to stop asking about this specific highlight/entry ("don't surface this again", "skip this one permanently", "bad pick", "junk", "boring source"). Otherwise false.
Set retire_type=true ONLY if {user} explicitly says to stop using this question type ("stop recall prompts", "revision isn't useful"). Otherwise false.
style_note: if feedback includes a rephrasable preference (e.g. "ask for takeaway not restatement", "keep it shorter"), summarize the preference in ONE short imperative sentence usable for tuning future prompts. Null if no such preference.
suggested_instead: if {user} explicitly phrased an alternative question they would have preferred, capture it verbatim. Null if not given.

For "mixed" replies, put the answer portion in answer_text and the critique in feedback_text. Do not duplicate.

Output ONLY the JSON."""


def classify_reply(question_text: str, qtype: str, reply_text: str) -> dict[str, Any]:
    """Use Claude CLI to classify a Slack reply. Falls back to 'answer' on error."""
    prompt = CLASSIFY_PROMPT.format(
        user=cfg("user.full_name", "the user"),
        qtype=qtype,
        question_text=question_text,
        reply_text=reply_text,
    )
    try:
        raw = claude_generate(prompt, timeout=60)
        data = extract_json(raw)
    except Exception as exc:
        stderr(f"classify_reply fallback (treating as answer): {exc}")
        return {
            "type": "answer",
            "answer_text": reply_text,
            "feedback_text": None,
            "retire_entry": False,
            "retire_type": False,
            "style_note": None,
            "suggested_instead": None,
        }

    # normalize
    out = {
        "type": data.get("type") or "answer",
        "answer_text": data.get("answer_text"),
        "feedback_text": data.get("feedback_text"),
        "retire_entry": bool(data.get("retire_entry")),
        "retire_type": bool(data.get("retire_type")),
        "style_note": data.get("style_note"),
        "suggested_instead": data.get("suggested_instead"),
    }
    if out["type"] not in {"answer", "skip", "feedback", "mixed"}:
        out["type"] = "answer"
    return out


POLISH_PROMPT = """You are tuning the final SENTENCE of a daily Socratic question for {user}.

Question type: {qtype}
Base question sentence (default phrasing):
---
{base_question}
---

{user}'s prior feedback on how they want these phrased (most recent first):
{feedback_block}

Rewrite ONLY the question sentence so it honors the feedback. Rules:
- Keep it ONE or TWO sentences. No emoji, no preamble, no explanation.
- End with exactly: (Reply in thread — or `skip`.)
- Do NOT mention the source material — the caller already quoted it above the line you are writing.
- If no feedback conflicts with the base phrasing, return the base sentence unchanged.

Output ONLY the rewritten line."""


def polish_question(base_question: str, qtype: str, feedback_notes: list[str]) -> str:
    """Optionally rewrite a question sentence based on prior feedback. Falls back on error."""
    if not feedback_notes:
        return base_question
    block = "\n".join(f"- {n.strip()}" for n in feedback_notes if n and n.strip())
    if not block:
        return base_question
    prompt = POLISH_PROMPT.format(
        user=cfg("user.full_name", "the user"),
        qtype=qtype,
        base_question=base_question,
        feedback_block=block,
    )
    try:
        polished = claude_generate(prompt, timeout=45).strip()
    except Exception as exc:
        stderr(f"polish_question fallback: {exc}")
        return base_question
    polished = polished.strip().strip("`").strip()
    if not polished or len(polished) > 600:
        return base_question
    if "Reply in thread" not in polished:
        polished = polished.rstrip(".") + " (Reply in thread — or `skip`.)"
    return polished


def wait_briefly(ms: int = 250) -> None:
    time.sleep(ms / 1000.0)


def stderr(*args: Any) -> None:
    print(*args, file=sys.stderr, flush=True)


# ---------- Lazy config-backed module attributes -------------------------
#
# PEP 562 module __getattr__: `sc.KB_DISCORD_CHANNEL` resolves on first access
# instead of at import. A skill that never touches Slack no longer dies because
# a Slack key is missing from config.json.
#
# NOTE: this fires only on attribute access *on the module object*. Inside this
# file, use cfg("...") directly — a bare global name would raise NameError.

_CONFIG_ATTRS = {
    "USER_FULL_NAME": "user.full_name",
    "USER_DISPLAY_NAME": "user.display_name",
    "MASTERCLAW_USER_ID": "slack.bot_user_id",
    "USER_SLACK_ID": "slack.user_id",
    "DAVID_USER_ID": "slack.user_id",           # deprecated alias
    "KB_DISCORD_CHANNEL": "discord.channels.knowledge_hub",
    "DISCORD_USER_ID": "discord.user_id",
    "DISCORD_DAVID_USER_ID": "discord.user_id",  # deprecated alias
    "DISCORD_BOT_USER_ID": "discord.bot_user_id",
    "DISCORD_GUILD_ID": "discord.guild_id",
    "KB_AI_CONVERSATION_CHANNEL": "slack.channels.ai_conversations",
}


def __getattr__(name: str) -> Any:
    key = _CONFIG_ATTRS.get(name)
    if key is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return cfg(key)


def __dir__() -> list[str]:
    return sorted(list(globals()) + list(_CONFIG_ATTRS))

