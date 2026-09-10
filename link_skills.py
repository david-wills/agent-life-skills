#!/usr/bin/env python3
"""Link every skill in this repo into a skills directory so a runtime can find it.

Usage:
  link_skills.py                              # dry run against ~/.claude/skills
  link_skills.py --apply
  link_skills.py --target /path/to/skills --apply
  link_skills.py --only name-videos reading-list --apply

Claude Code, and any runtime that scans <dir>/<skill>/SKILL.md, expects skills
one level deep. This repo keeps them two levels deep (<package>/<skill>) so each
package can carry its own README. A symlink per skill bridges the two. The link
points at the skill's real directory inside the clone, so its scripts still find
the repo's lib/ by walking up from their resolved path; copying a skill out of
the tree breaks that.

An entry that already points at the right place is left alone. Anything else
at that name is reported and skipped, never replaced.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent
SKIP_DIRS = {"lib", "tests"}
DEFAULT_TARGET = "~/.claude/skills"


def skills(only: list[str] | None = None) -> list[Path]:
    """Every <package>/<skill> directory holding a SKILL.md."""
    found = []
    for skill_md in sorted(REPO.glob("*/*/SKILL.md")):
        package = skill_md.parents[1].name
        if package.startswith((".", "_")) or package in SKIP_DIRS:
            continue
        if only and skill_md.parent.name not in only:
            continue
        found.append(skill_md.parent)
    return found


def plan(target: Path, only: list[str] | None = None) -> list[tuple[str, Path, Path]]:
    """(action, link, source) per skill: link | ok | conflict."""
    out = []
    for src in skills(only):
        link = target / src.name
        if link.is_symlink() or link.exists():
            same = link.is_symlink() and Path(os.path.realpath(link)) == src
            out.append(("ok" if same else "conflict", link, src))
        else:
            out.append(("link", link, src))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--target", default=DEFAULT_TARGET, help=f"skills directory (default {DEFAULT_TARGET})")
    ap.add_argument("--only", nargs="+", metavar="SKILL", help="link just these skill names")
    ap.add_argument("--apply", action="store_true", help="create the links; without it, print the plan")
    args = ap.parse_args()

    target = Path(args.target).expanduser()
    steps = plan(target, args.only)
    if not steps:
        print("no skills found" + (f" matching {args.only}" if args.only else ""))
        return 1

    conflicts = 0
    for action, link, src in steps:
        rel = src.relative_to(REPO)
        if action == "ok":
            print(f"ok        {link}  -> {rel}")
        elif action == "conflict":
            conflicts += 1
            what = "symlink to elsewhere" if link.is_symlink() else ("directory" if link.is_dir() else "file")
            print(f"CONFLICT  {link}  is a {what}; remove it to link {rel}")
        elif args.apply:
            target.mkdir(parents=True, exist_ok=True)
            link.symlink_to(src)
            print(f"linked    {link}  -> {rel}")
        else:
            print(f"would link {link}  -> {rel}")

    if not args.apply and any(a == "link" for a, _, _ in steps):
        print("\nDRY RUN. Re-run with --apply to create the links.")
    return 1 if conflicts else 0


if __name__ == "__main__":
    raise SystemExit(main())
