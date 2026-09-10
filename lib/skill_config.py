#!/usr/bin/env python3
"""Repo-level configuration for skills: identity, channel ids, paths.

Discord channel ids, a 1Password vault name and the operator's display name are
deployment facts, not source. Hardcoding them makes a skill unrunnable by anyone
else, so they live in a config file at the repo root.

Resolution order for ``cfg("discord.channels.reading_list")``:

    1. Env var ``SKILLS_DISCORD_CHANNELS_READING_LIST`` (dots -> underscores, upper).
       Lets a single cron or a test override one value without editing a file.
    2. ``config.local.json`` at the repo root: gitignored, wins over config.json.
    3. ``config.json`` at the repo root: gitignored too. Copy config.example.json
       to it and fill in the keys the packages you run need.
    4. The ``default`` argument, if one was passed.
    5. ConfigError naming the key and pointing at config.example.json.

A value still holding a placeholder copied from config.example.json
(``CHANNEL_ID``, ``USER_ID``, ``you@example.com``, ``/path/to/...``) counts as
unset: it falls through to the default or raises. Without that, a copied
example reaches Discord as a real request with a channel called CHANNEL_ID.

There is deliberately no silent fallback to a baked-in id: a fresh clone should
fail loudly with "configure this", never run against someone else's channels.

    from skill_config import cfg, data_root
    channel = cfg("discord.channels.reading_list")
    name    = cfg("user.display_name", "the user")
    db      = data_root() / "knowledge" / "index.db"

Call ``cfg`` from inside ``main()`` or a function, never at module scope, so
that ``--help`` and imports work before the config exists.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
from typing import Any

_MISSING = object()

# Repo root is the parent of lib/. Resolved from __file__ so this works when a
# skill is reached through a symlink or run from any cwd.
REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO_ROOT / "config.json"
LOCAL_PATH = REPO_ROOT / "config.local.json"
EXAMPLE_PATH = REPO_ROOT / "config.example.json"
DEFAULT_DATA_ROOT = REPO_ROOT / "_data"

_cache: dict[str, Any] | None = None


class ConfigError(KeyError):
    """A required config key is missing."""

    def __str__(self) -> str:  # KeyError quotes its message; we want it plain
        return str(self.args[0]) if self.args else ""


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
        _cache = _merge(_read(CONFIG_PATH), _read(LOCAL_PATH))
    return _cache


def _env_name(path: str) -> str:
    return "SKILLS_" + path.upper().replace(".", "_").replace("-", "_")


# --- placeholders ------------------------------------------------------------
# The example file's fill-me-in strings, learned from the file itself so the
# two cannot drift: SHOUTING_SNAKE_CASE tokens, /path/to/... paths, example.com
# addresses. Real defaults in the example ("credential", "agent-skills") do not
# match the pattern and are never treated as placeholders.

_PLACEHOLDER_SHAPE = re.compile(r"[A-Z][A-Z0-9_]*(?:[ ,]+[A-Z][A-Z0-9_]*)*(?:@\S+)?|/path/to/.*|.*@example\.com")
_placeholders: frozenset[str] | None = None


def _leaves(node: Any, prefix: str = "") -> list[tuple[str, Any]]:
    """(dotted key, value) for every non-comment leaf under node."""
    if isinstance(node, dict):
        out: list[tuple[str, Any]] = []
        for k, v in node.items():
            if str(k).startswith("_"):
                continue
            out.extend(_leaves(v, f"{prefix}.{k}" if prefix else str(k)))
        return out
    if isinstance(node, list):
        return [pair for i, v in enumerate(node) for pair in _leaves(v, f"{prefix}[{i}]")]
    return [(prefix, node)]


def placeholders() -> frozenset[str]:
    """Every placeholder string config.example.json ships with."""
    global _placeholders
    if _placeholders is None:
        found = set()
        for _key, value in _leaves(_read(EXAMPLE_PATH)):
            if isinstance(value, str) and _PLACEHOLDER_SHAPE.fullmatch(value.strip()):
                found.add(value.strip())
        _placeholders = frozenset(found)
    return _placeholders


def is_placeholder(value: Any) -> bool:
    return isinstance(value, str) and value.strip() in placeholders()


def _find_placeholder(node: Any) -> tuple[str, str] | None:
    """(sub-key, value) of the first placeholder inside node, or None."""
    for key, value in _leaves(node):
        if is_placeholder(value):
            return key, value.strip()
    return None


def lookup(path: str) -> tuple[str, Any]:
    """Resolve a key without defaults: ('env'|'config'|'placeholder'|'missing', value).

    ``placeholder`` carries the offending value; ``missing`` carries None. This
    is what ``cfg`` and ``doctor.py`` both build on.
    """
    env = os.environ.get(_env_name(path))
    if env is not None:
        return ("placeholder", env.strip()) if is_placeholder(env) else ("env", env)

    node: Any = _load()
    for part in path.split("."):
        if isinstance(node, dict) and part in node:
            node = node[part]
        else:
            return "missing", None

    hit = _find_placeholder(node)
    if hit:
        sub, value = hit
        return "placeholder", f"{value} (at {path}.{sub})" if sub else value
    return "config", node


def cfg(path: str, default: Any = _MISSING) -> Any:
    """Look up a dotted config path. See module docstring for resolution order."""
    state, value = lookup(path)
    if state in ("env", "config"):
        return value
    if default is not _MISSING:
        return default
    if state == "placeholder":
        raise ConfigError(
            f"config key {path!r} still holds the placeholder {value!r} from {EXAMPLE_PATH.name}; "
            f"set a real value in {CONFIG_PATH} or {_env_name(path)}."
        )
    raise ConfigError(
        f"missing config key {path!r}. Set {_env_name(path)} in the environment, or add it "
        f"to {CONFIG_PATH} (copy {EXAMPLE_PATH.name} to config.json and fill it in)."
    )


def data_root() -> pathlib.Path:
    """Where skills keep their databases, imported files and caches.

    ``paths.data_root`` in config (or ``SKILLS_PATHS_DATA_ROOT``), else ``_data/``
    at the repo root, which is gitignored. Skills derive every writable path from
    this, so relocating all state is one config edit.
    """
    raw = cfg("paths.data_root", None)
    root = pathlib.Path(raw).expanduser() if raw else DEFAULT_DATA_ROOT
    root.mkdir(parents=True, exist_ok=True)
    return root


def reload() -> None:
    """Drop the caches, for tests that write config.json between assertions."""
    global _cache, _placeholders
    _cache = None
    _placeholders = None


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
