#!/usr/bin/env python3
"""Unified secrets resolver for OpenClaw skills and crons.

Resolution order for `read_secret("foo")`:

    1. Env var matching `{NAME}_TOKEN` or `{NAME}_API_TOKEN` or just `{NAME}` (upper-cased).
       Useful for one-off testing without touching keychain/1P.

    2. 1Password via the `op` CLI, using the service-account token stashed in macOS Keychain
       under service=openclaw-1password-token account=$USER.
       Looks up `op://{vault}/{name}/{field}` using secrets.onepassword_vault and
       secrets.onepassword_field from config.json. Override the item path via
       SECRET_1P_PATH_{NAME} (upper-cased) if a secret needs a different location.

    3. macOS Keychain generic password under service=openclaw-{name} account=$USER.
       Useful for secrets we explicitly don't want in 1Password.

    4. `~/.openclaw/secrets.json` — flat {"name": "value"} map. Low-stakes fallback.

Exit codes:
    0  secret printed to stdout (no trailing newline)
    1  not found in any backing store
    2  backing-store error (1P unreachable, keychain locked, etc.)

CLI:
    read_secret.py <name>              # print secret to stdout
    read_secret.py --check <name>      # exit 0 if resolvable, 1 if not; prints source

Library:
    from read_secret import read_secret
    token = read_secret("readwise")
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


try:
    from skill_config import cfg as _cfg
except ImportError:  # standalone copy without lib/ on the path
    def _cfg(_path, default=None):
        return default

DEFAULT_1P_VAULT = _cfg("secrets.onepassword_vault", "OpenClaw")
DEFAULT_1P_FIELD = _cfg("secrets.onepassword_field", "credential")
SERVICE_ACCOUNT_KEYCHAIN_SERVICE = "openclaw-1password-token"
SECRETS_JSON_PATH = Path.home() / ".openclaw" / "secrets.json"


class SecretError(Exception):
    """Raised when a secret cannot be resolved or a backing store errors out."""


def _env_var_candidates(name: str) -> list[str]:
    upper = name.upper().replace("-", "_")
    return [f"{upper}_TOKEN", f"{upper}_API_TOKEN", upper]


def _try_env(name: str) -> str | None:
    for var in _env_var_candidates(name):
        value = os.environ.get(var)
        if value:
            return value
    return None


def _service_account_token() -> str | None:
    env_token = os.environ.get("OP_SERVICE_ACCOUNT_TOKEN")
    if env_token:
        return env_token
    try:
        result = subprocess.run(
            [
                "security",
                "find-generic-password",
                "-s",
                SERVICE_ACCOUNT_KEYCHAIN_SERVICE,
                "-a",
                getpass.getuser(),
                "-w",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    token = result.stdout.strip()
    return token or None


def _try_1password(name: str) -> str | None:
    # Cron-friendly opt-out: skip 1P entirely when set, so launchd-spawned
    # callers don't trigger op-CLI prompts/timeouts. Pair with a keychain mirror.
    if os.environ.get("OPENCLAW_SKIP_1PASSWORD"):
        return None
    if not shutil.which("op"):
        return None
    token = _service_account_token()
    if not token:
        return None

    override_var = f"SECRET_1P_PATH_{name.upper().replace('-', '_')}"
    path = os.environ.get(override_var) or f"op://{DEFAULT_1P_VAULT}/{name}/{DEFAULT_1P_FIELD}"

    env = dict(os.environ)
    env["OP_SERVICE_ACCOUNT_TOKEN"] = token
    try:
        result = subprocess.run(
            ["op", "read", path],
            check=False,
            capture_output=True,
            text=True,
            env=env,
            timeout=15,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise SecretError(f"1Password lookup failed: {exc}") from exc

    if result.returncode == 0:
        value = result.stdout.strip()
        return value or None
    # Item not found in 1P is not fatal — we fall through to other stores.
    stderr = result.stderr.strip()
    if "isn't an item" in stderr or "no item" in stderr.lower() or "not found" in stderr.lower():
        return None
    raise SecretError(f"1Password error for {path}: {stderr or 'unknown'}")


def _try_keychain(name: str) -> str | None:
    try:
        result = subprocess.run(
            [
                "security",
                "find-generic-password",
                "-s",
                f"openclaw-{name}",
                "-a",
                getpass.getuser(),
                "-w",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value or None


def _try_secrets_json(name: str) -> str | None:
    if not SECRETS_JSON_PATH.is_file():
        return None
    try:
        data = json.loads(SECRETS_JSON_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SecretError(f"{SECRETS_JSON_PATH} is unreadable: {exc}") from exc
    value = data.get(name)
    if isinstance(value, str) and value:
        return value
    return None


RESOLVERS: list[tuple[str, callable]] = [
    ("env", _try_env),
    ("1password", _try_1password),
    ("keychain", _try_keychain),
    ("secrets_json", _try_secrets_json),
]


def read_secret(name: str) -> str:
    """Return the secret value for `name`, raising SecretError if unresolvable."""
    last_error: SecretError | None = None
    for _source, resolver in RESOLVERS:
        try:
            value = resolver(name)
        except SecretError as exc:
            last_error = exc
            continue
        if value:
            return value
    if last_error:
        raise last_error
    raise SecretError(f"secret {name!r} not found in env, 1Password, keychain, or {SECRETS_JSON_PATH}")


def resolve_with_source(name: str) -> tuple[str, str]:
    """Return (source, value) — useful for `--check` introspection."""
    last_error: SecretError | None = None
    for source, resolver in RESOLVERS:
        try:
            value = resolver(name)
        except SecretError as exc:
            last_error = exc
            continue
        if value:
            return source, value
    if last_error:
        raise last_error
    raise SecretError(f"secret {name!r} not found")


def main() -> int:
    p = argparse.ArgumentParser(description="Resolve a secret from OpenClaw's secret stores.")
    p.add_argument("name", help="Logical secret name, e.g. 'readwise'.")
    p.add_argument("--check", action="store_true", help="Print source + length to stderr; exit 0 if resolvable.")
    args = p.parse_args()
    try:
        if args.check:
            source, value = resolve_with_source(args.name)
            print(f"resolved {args.name!r} via {source} (length={len(value)})", file=sys.stderr)
            return 0
        value = read_secret(args.name)
        sys.stdout.write(value)
    except SecretError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1 if "not found" in str(exc) else 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
