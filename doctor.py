#!/usr/bin/env python3
"""Check what this machine can run: Python, binaries, config keys, secrets, files.

Usage:
  doctor.py                          # every package
  doctor.py video-tools token-tracker
  doctor.py --json                   # machine-readable
  doctor.py --no-secrets             # skip the secret stores (1Password can be slow)

One line per check. A config key still holding a placeholder from
config.example.json is a failure, not a value. Secret values are never printed,
only which store resolved them. Exit 1 when a selected package has a failing
required check.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import platform
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO / "lib"))

MIN_PYTHON = (3, 11)
DEFAULT_CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

# (name, required, hint). Hints say what to do, not what is wrong.
REQ = True
OPT = False
FEEDS_REQ = "pip install -r news-triage-assistant/morning-news/requirements.txt"

PACKAGES: dict[str, dict[str, list[tuple]]] = {
    "news-triage-assistant": {
        "config": [("discord.channels.newsfeed", REQ, ""), ("discord.channels.reading_list", REQ, ""),
                   ("discord.user_id", REQ, "whose reactions the sweep acts on"),
                   ("user.display_name", REQ, ""), ("discord.channels.errors", OPT, "where failures page")],
        "secret": [("DISCORD_BOT_TOKEN", REQ, ""), ("readwise", REQ, "Readwise Reader API token")],
        "binary": [("claude", REQ, "summaries run through the claude CLI")],
        "module": [("feedparser", REQ, FEEDS_REQ), ("yaml", REQ, FEEDS_REQ), ("dateutil", REQ, FEEDS_REQ)],
        "file": [("news-triage-assistant/morning-news/feeds.local.yaml", REQ, "cp feeds.example.yaml feeds.local.yaml"),
                 ("news-triage-assistant/morning-news/profile.local.md", REQ, "cp profile.example.md profile.local.md")],
    },
    "quantified-self-coach": {
        "config": [("discord.channels.workouts", REQ, "the coach's channel"),
                   ("discord.channels.errors", OPT, "gap_check pages here")],
        "secret": [("HAE_REST_TOKEN", REQ, "shared secret the Health Auto Export app posts with"),
                   ("HEVY_API_KEY", REQ, ""), ("OURA_CLIENT_ID", REQ, ""), ("OURA_CLIENT_SECRET", REQ, "")],
        "binary": [("claude", OPT, "only the coach prompt needs a model")],
        "module": [],
        "file": [("<data_root>/workout-coach/GOALS.md", REQ, "cp quantified-self-coach/workout-coach/GOALS.example.md there"),
                 ("<data_root>/workout-coach/PROGRAM.md", REQ, "cp quantified-self-coach/workout-coach/PROGRAM.example.md there")],
        "os": [("Darwin", OPT, "the shipped launchd templates; any scheduler works")],
    },
    "restaurant-assistant": {
        "config": [("discord.channels.restaurants", REQ, ""), ("discord.user_id", REQ, "whose ✅ books"),
                   ("user.home", REQ, "drive-time origin"), ("accounts.personal", REQ, "the Maps sign-in"),
                   ("accounts.service", REQ, "the calendar CLI account"), ("calendars.shared", REQ, ""),
                   ("discord.channels.errors", OPT, "")],
        "secret": [("DISCORD_BOT_TOKEN", REQ, "")],
        "binary": [("gog", REQ, "calendar CLI; swap fetch_events in calendar_lib.py for another"),
                   ("chrome", REQ, "Google Chrome.app, or CHROME_PATH pointing at a Chrome binary")],
        "module": [("playwright", REQ, "pip install -r restaurant-assistant/restaurant-saver/requirements.txt")],
        "file": [],
        "os": [("Darwin", OPT, "the default Chrome path is macOS; set CHROME_PATH elsewhere")],
    },
    "token-tracker": {
        "config": [("discord.channels.token_tracker", REQ, ""), ("token_tracker.host", OPT, "label for this machine's rows; defaults to the short hostname")],
        "secret": [("DISCORD_BOT_TOKEN", REQ, "")],
        "binary": [("security", OPT, "macOS keychain; quota sampling reads the Claude Code login from it")],
        "module": [],
        "file": [("~/.claude/projects", OPT, "Claude Code transcripts; nothing to count without them")],
    },
    "video-tools": {
        "config": [("paths.video_drop", OPT, "scan.py also takes folders on the command line")],
        "secret": [],
        "binary": [("ffmpeg", REQ, "brew install ffmpeg")],
        "module": [("Vision", REQ, "pip install -r video-tools/name-videos/requirements.txt (macOS)"),
                   ("Quartz", REQ, "pip install -r video-tools/name-videos/requirements.txt (macOS)")],
        "file": [],
        "os": [("Darwin", REQ, "probe.py uses Apple's Vision framework for OCR")],
    },
}


def result(kind: str, name: str, ok: bool, required: bool, detail: str) -> dict:
    status = "ok" if ok else ("FAIL" if required else "warn")
    return {"kind": kind, "name": name, "status": status, "required": required, "detail": detail}


def check_config(name: str, required: bool, hint: str) -> dict:
    from skill_config import lookup
    state, value = lookup(name)
    if state == "env":
        return result("config", name, True, required, "set (environment)")
    if state == "config":
        return result("config", name, True, required, "set")
    if state == "placeholder":
        return result("config", name, False, required, f"still the placeholder {value}. {hint}".strip())
    return result("config", name, False, required, f"not set. {hint}".strip())


def check_secret(name: str, required: bool, hint: str, skip: bool) -> dict:
    if skip:
        return {"kind": "secret", "name": name, "status": "skip", "required": required, "detail": "--no-secrets"}
    from read_secret import SecretError, resolve_with_source
    try:
        source, _value = resolve_with_source(name)
    except SecretError as exc:
        return result("secret", name, False, required, f"{exc}. {hint}".strip())
    except Exception as exc:  # noqa: BLE001 - a store that errors is still "not resolvable"
        return result("secret", name, False, required, f"{type(exc).__name__}: {exc}")
    return result("secret", name, True, required, f"via {source}")


def check_binary(name: str, required: bool, hint: str) -> dict:
    if name == "chrome":
        path = os.environ.get("CHROME_PATH") or DEFAULT_CHROME
        ok = os.access(path, os.X_OK)
        return result("binary", name, ok, required, path if ok else f"{path} not found. {hint}".strip())
    path = shutil.which(name)
    return result("binary", name, bool(path), required, path or f"not on PATH. {hint}".strip())


def check_module(name: str, required: bool, hint: str) -> dict:
    ok = importlib.util.find_spec(name) is not None
    return result("module", name, ok, required, "importable" if ok else f"not installed. {hint}".strip())


def check_file(name: str, required: bool, hint: str, data_root: Path | None) -> dict:
    if name.startswith("<data_root>/"):
        if data_root is None:
            return result("file", name, False, required, "data root unavailable")
        path = data_root / name[len("<data_root>/"):]
    elif name.startswith("~"):
        path = Path(name).expanduser()
    else:
        path = REPO / name
    ok = path.exists()
    return result("file", name, ok, required, str(path) if ok else f"{path} missing. {hint}".strip())


def check_os(name: str, required: bool, hint: str) -> dict:
    ok = platform.system() == name
    label = {"Darwin": "macOS"}.get(name, name)
    return result("os", label, ok, required, platform.system() if ok else f"this is {platform.system()}. {hint}".strip())


def environment(data_root: Path | None, data_root_error: str) -> list[dict]:
    from skill_config import CONFIG_PATH, LOCAL_PATH
    out = [result("python", platform.python_version(), sys.version_info >= MIN_PYTHON, True,
                  sys.executable if sys.version_info >= MIN_PYTHON else f"needs {'.'.join(map(str, MIN_PYTHON))}+")]
    out.append(result("file", "config.json", CONFIG_PATH.is_file(), False,
                      str(CONFIG_PATH) if CONFIG_PATH.is_file() else "cp config.example.json config.json"))
    if LOCAL_PATH.is_file():
        out.append(result("file", "config.local.json", True, False, str(LOCAL_PATH)))
    if data_root is None:
        out.append(result("data_root", "paths.data_root", False, True, data_root_error))
    else:
        writable = os.access(data_root, os.W_OK)
        out.append(result("data_root", str(data_root), writable, True, "writable" if writable else "not writable"))
    return out


def run(packages: list[str], skip_secrets: bool) -> dict:
    from skill_config import ConfigError, data_root as _data_root
    root: Path | None
    try:
        root = _data_root()
        root_error = ""
    except (ConfigError, OSError) as exc:
        root, root_error = None, str(exc)

    report = {"os": f"{platform.system()} {platform.release()} {platform.machine()}",
              "environment": environment(root, root_error), "packages": {}}
    for pkg in packages:
        spec = PACKAGES[pkg]
        checks: list[dict] = []
        checks += [check_os(*c) for c in spec.get("os", [])]
        checks += [check_binary(*c) for c in spec["binary"]]
        checks += [check_module(*c) for c in spec["module"]]
        checks += [check_config(*c) for c in spec["config"]]
        checks += [check_secret(*c, skip=skip_secrets) for c in spec["secret"]]
        checks += [check_file(*c, data_root=root) for c in spec["file"]]
        report["packages"][pkg] = checks
    failing = [c for c in report["environment"] if c["status"] == "FAIL"]
    failing += [c for checks in report["packages"].values() for c in checks if c["status"] == "FAIL"]
    report["ok"] = not failing
    return report


def print_report(report: dict) -> None:
    def line(c: dict) -> str:
        return f"  {c['status']:4}  {c['kind']:9} {c['name']:44} {c['detail']}"

    print(f"{report['os']}")
    for c in report["environment"]:
        print(line(c))
    for pkg, checks in report["packages"].items():
        fails = sum(c["status"] == "FAIL" for c in checks)
        print(f"\n{pkg}: {'ready' if not fails else f'{fails} blocking'}")
        for c in checks:
            print(line(c))
    print("\nall selected packages ready" if report["ok"] else "\nfix the FAIL lines above, then re-run")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 epilog="packages: " + ", ".join(PACKAGES))
    ap.add_argument("package", nargs="*", choices=[*PACKAGES, []], metavar="package",
                    help="limit the report to these packages (default: all)")
    ap.add_argument("--json", action="store_true", help="print the report as JSON")
    ap.add_argument("--no-secrets", action="store_true", help="do not touch the secret stores")
    args = ap.parse_args()
    packages = args.package or list(PACKAGES)
    report = run(packages, skip_secrets=args.no_secrets)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print_report(report)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
