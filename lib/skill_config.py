#!/usr/bin/env python3
"""Repo-level configuration for skills: identity, channel ids, vault names.

Why this exists: things like Discord channel ids, a 1Password vault name and the
operator's display name are *deployment* facts, not source. Hardcoding them makes
a skill unrunnable by anyone else and unpublishable without a scrub pass.

Resolution order for ``cfg("discord.channels.reading_list")``:

    1. Env var ``SKILLS_DISCORD_CHANNELS_READING_LIST`` (dots -> underscores, upper).
       Lets a single cron or a test override one value without editing the file.
    2. ``config.local.json`` at the repo root — gitignored, wins over config.json.
       Put per-machine overrides or anything genuinely sensitive here.
    3. ``config.json`` at the repo root — tracked, holds non-secret deployment
       values (channel ids, vault *name*, display name) so a clone just works.
    4. The ``default`` argument, if one was passed.
    4. KeyError with a message naming the key and pointing at config.example.json.

There is deliberately no silent fallback to a baked-in id: a fresh clone should
fail loudly with "configure this", never run against someone else's channels.

    from skill_config import cfg
    channel = cfg("discord.channels.reading_list")
    name    = cfg("user.display_name", "the user")
"""

from __future__ import annotations

import json
import os
import pathlib
from typing import Any

_MISSING = object()

# Repo root is the parent of lib/. Resolve from __file__ so this works when a
# skill is reached through the ~/.claude/skills symlink or run from any cwd.
REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO_ROOT / "config.json"
LOCAL_PATH = REPO_ROOT / "config.local.json"   # gitignored; overrides config.json
EXAMPLE_PATH = REPO_ROOT / "config.example.json"

_cache: dict[str, Any] | None = None


class ConfigError(KeyError):
    """A required config key is missing."""


def _read(path: pathlib.Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path} is not valid JSON: {exc}") from exc


def _merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    """Recursive dict merge; `over` wins at the leaves."""
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def _load() -> dict[str, Any]:
    global _cache
    if _cache is None:
        # config.json is tracked and holds non-secret deployment values.
        # config.local.json is gitignored and wins — put anything genuinely
        # sensitive, or any per-machine override, there.
        _cache = _merge(_read(CONFIG_PATH), _read(LOCAL_PATH))
    return _cache


def _env_name(path: str) -> str:
    return "SKILLS_" + path.upper().replace(".", "_").replace("-", "_")


def cfg(path: str, default: Any = _MISSING) -> Any:
    """Look up a dotted config path. See module docstring for resolution order."""
    env = os.environ.get(_env_name(path))
    if env is not None:
        return env

    node: Any = _load()
    for part in path.split("."):
        if isinstance(node, dict) and part in node:
            node = node[part]
        else:
            node = _MISSING
            break

    if node is not _MISSING:
        return node
    if default is not _MISSING:
        return default

    raise ConfigError(
        f"missing config key {path!r}. Set {_env_name(path)} in the environment, or add it "
        f"to {CONFIG_PATH}. Copy {EXAMPLE_PATH.name} to config.json and fill it in."
    )


# ─── WORKSPACE ROOTS ─────────────────────────────────────────────────────────
# The agent workspace anchor. CONVENTIONS.md 4 mandates this exact segmented,
# absolute form and forbids deriving it from __file__ — after the 2026-09-03
# symlink move, __file__ resolves into this repo rather than the workspace tree.
WORKSPACE = pathlib.Path.home() / ".openclaw" / "workspace"


def work_root() -> pathlib.Path:
    """The work agent's workspace directory.

    Sixteen scripts across nine skills used to end this path with a hardcoded
    leaf naming an employer. Publishing any of them meant renaming that leaf — and
    because it names a directory that actually exists and holds the live knowledge
    base, "rename" meant moving live data on the machine that holds it, in the
    same window as sixteen edits and a set of cron definitions.

    Making the leaf a config key deletes that problem instead of scheduling it.
    The tracked ``config.json`` names the real directory, ``config.example.json``
    ships a generic default, and nothing on disk has to move. The next rename is
    a one-line config edit rather than a migration.

    The *anchor* stays hardcoded on purpose — only the leaf is deployment.
    """
    return WORKSPACE / cfg("workspace.work_agent_dir")


def reload() -> None:
    """Drop the cache — for tests that write config.json between assertions."""
    global _cache
    _cache = None


if __name__ == "__main__":
    import sys
    if len(sys.argv) != 2:
        print("usage: skill_config.py <dotted.key>", file=sys.stderr)
        raise SystemExit(2)
    try:
        print(cfg(sys.argv[1]))
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        raise SystemExit(1)
