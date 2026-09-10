"""Shared fixtures for the test suite: repo paths and a fake Discord server.

The fake answers the two endpoints reaction sweeps use, the message read and
the per-emoji reactor list, with the shapes Discord's v10 API returns. Tests
install it over ``discord.discord_request`` so the real ownership logic runs
with no network.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from urllib.parse import unquote

REPO = Path(__file__).resolve().parent.parent
LIB = REPO / "lib"


def add_path(*parts: str) -> None:
    """Put a repo directory at the front of sys.path (idempotent)."""
    p = str(REPO.joinpath(*parts))
    if p not in sys.path:
        sys.path.insert(0, p)


add_path("lib")

BOT = "100000000000000001"
OWNER = "200000000000000002"
STRANGER = "300000000000000003"

_REACTIONS_RE = re.compile(r"^/channels/([^/]+)/messages/([^/]+)/reactions/([^?]+)(?:\?(.*))?$")
_MESSAGE_RE = re.compile(r"^/channels/([^/]+)/messages/([^/?]+)$")


class FakeDiscord:
    """In-memory stand-in for ``discord.discord_request``.

    ``messages`` maps message_id -> {emoji_name: [user ids]}. A bot id in the
    list marks the seed reaction. ``burst`` holds super reactions in the same
    shape. Unknown message ids raise DiscordError like a 404 would.
    """

    def __init__(self, messages: dict[str, dict[str, list[str]]],
                 burst: dict[str, dict[str, list[str]]] | None = None, bot: str = BOT) -> None:
        self.messages = messages
        self.burst = burst or {}
        self.bot = bot
        self.calls: list[str] = []

    def __call__(self, method: str, path: str, payload=None, token=None, timeout=30):
        from discord import DiscordError
        self.calls.append(f"{method} {path}")
        m = _REACTIONS_RE.match(path)
        if m:
            _channel, mid, emoji, query = m.groups()
            if mid not in self.messages:
                raise DiscordError(f"Discord GET {path} failed: HTTP 404 Unknown Message")
            params = dict(kv.split("=", 1) for kv in (query or "").split("&") if kv)
            source = self.burst if params.get("type") == "1" else self.messages
            users = list(source.get(mid, {}).get(unquote(emoji), []))
            after = params.get("after")
            if after:
                users = users[users.index(after) + 1:] if after in users else []
            limit = int(params.get("limit", 25))
            return [{"id": u, "username": f"user-{u[-2:]}"} for u in users[:limit]]
        m = _MESSAGE_RE.match(path)
        if m and method == "GET":
            _channel, mid = m.groups()
            if mid not in self.messages:
                raise DiscordError(f"Discord GET {path} failed: HTTP 404 Unknown Message")
            reactions = []
            for name, users in self.messages[mid].items():
                burst = self.burst.get(mid, {}).get(name, [])
                reactions.append({
                    "emoji": {"id": None, "name": name},
                    "count": len(users) + len(burst),
                    "count_details": {"burst": len(burst), "normal": len(users)},
                    "me": self.bot in users,
                    "me_burst": False,
                })
            return {"id": mid, "reactions": reactions}
        raise AssertionError(f"FakeDiscord: unexpected {method} {path}")
