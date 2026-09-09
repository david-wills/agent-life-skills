#!/usr/bin/env python3
"""List videos in the drop folder(s) that still need naming.

Usage:
  scan.py                       # the folder in config `paths.video_drop` + its CUTDOWNS subfolder
  scan.py /path/to/folder ...   # explicit folders, no config needed
  scan.py --json                # machine-readable {"todo": [...], "skipped": [...]}
"""
import os, sys, json, argparse
from pathlib import Path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from classify import is_named

# Shared helpers live in lib/ at the repo root; walk up so this works from any cwd.
_LIB = next((p / "lib" for p in Path(__file__).resolve().parents if (p / "lib" / "skill_config.py").is_file()), None)
if _LIB is None:
    raise SystemExit("cannot find the repo-root lib/ directory; run from a clone of the repo, not a copied file")
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))
from skill_config import cfg  # noqa: E402


EXT = {".mp4", ".mov", ".m4v"}
# House convention: a CUTDOWNS subfolder beneath the drop folder always takes the
# SPOTLIGHT prefix. Change or delete this rule for a different convention.
CUTDOWNS = "CUTDOWNS"

def default_folders():
    """The configured drop folder plus its CUTDOWNS subfolder, or None if unconfigured."""
    drop = cfg("paths.video_drop", None)
    if not drop:
        return None
    drop = os.path.expanduser(drop)
    return [drop, os.path.join(drop, CUTDOWNS)]

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
                   "forced_prefix": "SPOTLIGHT" if os.path.basename(d) == CUTDOWNS else None}
            (skipped if ok else todo).append(rec)
    return todo, skipped

def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("folders", nargs="*",
                    help="folders to scan; default is config paths.video_drop and its CUTDOWNS subfolder")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of the readable list")
    a = ap.parse_args()
    folders = [os.path.expanduser(f) for f in a.folders] or default_folders()
    if not folders:
        raise SystemExit("no folder given and paths.video_drop is not set in config.json; "
                         "pass a folder on the command line or set that key")
    missing = [d for d in folders if not os.path.isdir(d)]
    if len(missing) == len(folders):
        raise SystemExit("none of the folders exist: " + ", ".join(folders))
    todo, skipped = scan(folders)
    if a.json:
        print(json.dumps({"todo": todo, "skipped": skipped}, indent=1)); return
    print(f"NEEDS NAMING ({len(todo)}):")
    for r in todo:
        tag = f"[{r['forced_prefix']}] " if r["forced_prefix"] else ""
        print(f"  {tag}{r['filename']}   <- {r['reason']}")
    print(f"\nalready named, skipped: {len(skipped)}")

if __name__ == "__main__":
    main()
