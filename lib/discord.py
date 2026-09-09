"""Minimal Discord bot transport, stdlib only.

Every skill that posts to Discord goes through this module. It knows how to
find the bot token, make an authenticated request, split long text at the
2000-character limit, convert a little legacy markup, and read reactions.

Token resolution: ``DISCORD_BOT_TOKEN`` in the environment, then the secret
stores ``read_secret`` knows about (1Password, keychain, secrets.json) under the
name ``DISCORD_BOT_TOKEN``.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from typing import Any

from read_secret import SecretError, read_secret

DISCORD_API = "https://discord.com/api/v10"
MAX_CHARS = 2000
SUPPRESS_EMBEDS = 4  # message flag


class DiscordError(RuntimeError):
    """An HTTP or transport failure talking to Discord."""


def get_discord_token() -> str:
    try:
        return read_secret("DISCORD_BOT_TOKEN")
    except SecretError as exc:
        raise DiscordError(
            "no Discord bot token: set DISCORD_BOT_TOKEN in the environment or store it "
            "under that name in one of the secret stores lib/read_secret.py reads"
        ) from exc


def discord_request(method: str, path: str, payload: dict[str, Any] | None = None,
                    token: str | None = None, timeout: int = 30) -> Any:
    """One authenticated call. Raises DiscordError on any non-2xx."""
    token = token or get_discord_token()
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        f"{DISCORD_API}{path}",
        data=data,
        headers={
            "Authorization": f"Bot {token}",
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": "DiscordBot (agent-life-skills, 1.0)",
        },
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        raise DiscordError(f"Discord {method} {path} failed: HTTP {exc.code} {body[:400]}") from exc
    except urllib.error.URLError as exc:
        raise DiscordError(f"Discord {method} {path} unreachable: {exc.reason}") from exc


def split_for_discord(text: str, limit: int = 1900) -> list[str]:
    """Split text into <=limit chunks at paragraph, then line, boundaries. Never truncates."""
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


# --- light markup normalisation --------------------------------------------
# Some composers still emit `<url|label>` links, `*bold*` and `:shortcode:`
# emoji. Discord wants `[label](url)`, `**bold**` and unicode.

_LINK_RE = re.compile(r"<(https?://[^|>\s]+)\|([^>]+)>")
_BARE_LINK_RE = re.compile(r"<(https?://[^|>\s]+)>")
_BOLD_RE = re.compile(r"(?<![\w*])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![\w*])")

EMOJI_SHORTCODES = {
    ":warning:": "⚠️", ":white_check_mark:": "✅", ":x:": "❌",
    ":one:": "1️⃣", ":two:": "2️⃣", ":three:": "3️⃣",
    ":no_entry_sign:": "🚫", ":next_track_button:": "⏭️", ":brain:": "🧠",
    ":leftwards_arrow_with_hook:": "↩️", ":wastebasket:": "🗑️", ":link:": "🔗",
    ":books:": "📚", ":pencil:": "✏️", ":pause_button:": "⏸️", ":memo:": "📝",
    ":open_book:": "📖", ":mag:": "🔍", ":information_source:": "ℹ️",
    ":hourglass:": "⏳", ":construction:": "🚧", ":arrows_counterclockwise:": "🔄",
}


def normalize_markdown(text: str) -> str:
    if not text:
        return text
    text = _LINK_RE.sub(lambda m: f"[{m.group(2).strip()}]({m.group(1).strip()})", text)
    text = _BARE_LINK_RE.sub(lambda m: m.group(1), text)
    text = _BOLD_RE.sub(lambda m: f"**{m.group(1)}**", text)
    for code, uni in EMOJI_SHORTCODES.items():
        if code in text:
            text = text.replace(code, uni)
    return text


def strip_variation_selectors(text: str) -> str:
    """Drop U+FE0E / U+FE0F so '✅' compares equal however a client encoded it."""
    return (text or "").replace("︎", "").replace("️", "")


def send_message(channel_id: str, text: str, token: str | None = None,
                 pause_ms: int = 250) -> list[dict[str, Any]]:
    """Post text to a channel, splitting if needed. Returns the raw message objects."""
    token = token or get_discord_token()
    sent: list[dict[str, Any]] = []
    for chunk in split_for_discord(normalize_markdown(text)):
        msg = discord_request(
            "POST", f"/channels/{channel_id}/messages",
            {"content": chunk, "flags": SUPPRESS_EMBEDS, "allowed_mentions": {"parse": []}},
            token=token,
        )
        sent.append(msg)
        time.sleep(pause_ms / 1000.0)
    return sent


def add_reaction(channel_id: str, message_id: str, emoji: str, token: str | None = None) -> None:
    from urllib.parse import quote
    discord_request("PUT", f"/channels/{channel_id}/messages/{message_id}/reactions/{quote(emoji)}/@me",
                    token=token)


def fetch_message_reactions(channel_id: str, message_id: str,
                            token: str | None = None) -> list[dict[str, Any]]:
    """Return a message's reactions as ``[{name, count, by_user}]``.

    Raises DiscordError if the message cannot be read. Callers must not treat a
    failure as "no reactions": a deleted message or expired token is a problem
    to report, not a quiet day.

    Discord returns ``{emoji:{name}, count, me}`` without a user list. The bot
    seeds one reaction of its own, so ``by_user`` means the count exceeds the
    bot's contribution.
    """
    msg = discord_request("GET", f"/channels/{channel_id}/messages/{message_id}", token=token)
    out: list[dict[str, Any]] = []
    for r in msg.get("reactions") or []:
        name = (r.get("emoji") or {}).get("name")
        if not name:
            continue
        count = int(r.get("count") or 0)
        by_user = count > (1 if r.get("me") else 0)
        out.append({"name": name, "count": count, "by_user": by_user})
    return out
