#!/usr/bin/env python3
"""List videos in the drop folders that still need naming."""
import os, sys, json, argparse
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from classify import is_named

# Shared helpers live in the repo-root lib/ (CONVENTIONS.md 2). Walk up to find it
# rather than hardcoding a path: skills are reached through a symlink. The Drive
# mount name embeds a work address, so it lives in the gitignored config.local.json.
from pathlib import Path as _P  # noqa: E402
_LIB = next(p / "lib" for p in _P(__file__).resolve().parents if (p / "lib" / "read_secret.py").is_file())
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))
from skill_config import cfg  # noqa: E402


EXT = {".mp4", ".mov", ".m4v"}
BASE = os.path.join(
    os.path.expanduser(cfg("paths.drive_root")),
    # Edit to your drop folder. CUTDOWNS beneath it always takes the SPOTLIGHT prefix.
    "Shared drives/Marketing/Videos/REELS")
DROPS = [BASE, os.path.join(BASE, "CUTDOWNS")]

def scan(folders):
    todo, skipped = [], []
    for d in folders:
        if not os.path.isdir(d): continue
        for f in sorted(os.listdir(d)):
            p = os.path.join(d, f)
            if not os.path.isfile(p) or os.path.splitext(f)[1].lower() not in EXT:
                continue
            ok, why = is_named(f)
            rec = {"path": p, "filename": f, "folder": d, "reason": why,
                   "forced_prefix": "SPOTLIGHT" if os.path.basename(d) == "CUTDOWNS" else None}
            (skipped if ok else todo).append(rec)
    return todo, skipped

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("folders", nargs="*", default=None)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    todo, skipped = scan(a.folders or DROPS)
    if a.json:
        print(json.dumps({"todo": todo, "skipped": skipped}, indent=1)); sys.exit()
    print(f"NEEDS NAMING ({len(todo)}):")
    for r in todo:
        tag = f"[{r['forced_prefix']}] " if r["forced_prefix"] else ""
        print(f"  {tag}{r['filename']}   <- {r['reason']}")
    print(f"\nalready named, skipped: {len(skipped)}")
