#!/usr/bin/env python3
"""Render the launchd templates in ../launchd/ and install them as LaunchAgents.

Each ``*.plist.template`` carries four placeholders:

  {{HOME}}    this user's home directory
  {{REPO}}    the root of this clone (the directory that contains lib/)
  {{PYTHON}}  the interpreter to run the scripts with (default: this one)
  {{BIND}}    the interface the ingest server listens on (default 127.0.0.1)

Rendered files go to ~/Library/LaunchAgents/<label>.plist. Logs land under
<repo>/_data/logs/, which is created here so launchd can open them.

Usage:
  python3 render_launchd.py --dry-run apple-health-server         # print, write nothing
  python3 render_launchd.py --install apple-health-server --bind 100.101.102.103
  python3 render_launchd.py --install --all --load                # render both + launchctl
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

# Shared helpers live in lib/ at the repo root; walk up so this works from any cwd.
_LIB = next((p / "lib" for p in Path(__file__).resolve().parents if (p / "lib" / "skill_config.py").is_file()), None)
if _LIB is None:
    raise SystemExit("cannot find the repo-root lib/ directory; run from a clone of the repo, not a copied file")
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

REPO_ROOT = _LIB.parent
TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "launchd"
LABEL_PREFIX = "com.agent-life-skills."
LAUNCH_AGENTS = Path.home() / "Library" / "LaunchAgents"


def available() -> list[str]:
    return sorted(
        p.name[len(LABEL_PREFIX):-len(".plist.template")]
        for p in TEMPLATE_DIR.glob(f"{LABEL_PREFIX}*.plist.template")
    )


def render(name: str, python: str, bind: str) -> tuple[str, str]:
    """Return (label, rendered plist text)."""
    label = LABEL_PREFIX + name
    template = TEMPLATE_DIR / f"{label}.plist.template"
    if not template.is_file():
        raise SystemExit(f"no template for {name!r}; available: {', '.join(available())}")
    text = template.read_text(encoding="utf-8")
    subs = {
        "{{HOME}}": str(Path.home()),
        "{{REPO}}": str(REPO_ROOT),
        "{{PYTHON}}": python,
        "{{BIND}}": bind,
    }
    for k, v in subs.items():
        text = text.replace(k, v)
    leftover = [k for k in ("{{",) if k in text]
    if leftover:
        raise SystemExit(f"{template.name} still has an unfilled placeholder after rendering")
    return label, text


def launchctl_load(plist: Path) -> None:
    uid = subprocess.run(["id", "-u"], capture_output=True, text=True, check=True).stdout.strip()
    domain = f"gui/{uid}"
    # bootout first so a re-render replaces the running job; ignore "not loaded".
    subprocess.run(["launchctl", "bootout", domain, str(plist)], capture_output=True)
    subprocess.run(["launchctl", "bootstrap", domain, str(plist)], check=True)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("names", nargs="*", help=f"Template name(s): {', '.join(available()) or '(none found)'}")
    p.add_argument("--all", action="store_true", help="Render every template in launchd/.")
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="Print the rendered plist(s) to stdout.")
    mode.add_argument("--install", action="store_true", help=f"Write to {LAUNCH_AGENTS}.")
    p.add_argument("--load", action="store_true", help="After --install, launchctl bootstrap the job(s).")
    p.add_argument("--python", default=sys.executable, help="Interpreter path to bake in (default: this one).")
    p.add_argument("--bind", default="127.0.0.1", help="Interface for the ingest server (default 127.0.0.1).")
    args = p.parse_args()

    names = available() if args.all else args.names
    if not names:
        p.error("name at least one template, or pass --all")
    if args.load and not args.install:
        p.error("--load only makes sense with --install")

    for name in names:
        label, text = render(name, args.python, args.bind)
        if args.dry_run:
            print(f"# ---- {label}.plist")
            print(text)
            continue
        (REPO_ROOT / "_data" / "logs").mkdir(parents=True, exist_ok=True)
        LAUNCH_AGENTS.mkdir(parents=True, exist_ok=True)
        out = LAUNCH_AGENTS / f"{label}.plist"
        out.write_text(text, encoding="utf-8")
        print(f"wrote {out}")
        if args.load:
            launchctl_load(out)
            print(f"loaded {label}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
