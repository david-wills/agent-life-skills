#!/usr/bin/env python3
"""Apply a rename plan. Dry-run by default; writes an undo log when applied.

Usage:
  rename.py plan.json            # dry run: print the table, touch nothing
  rename.py plan.json --apply    # execute, then write <data_root>/name-videos/logs/rename-<stamp>.json

The plan is a JSON list of {"path", "new_name", "confidence", "reason"}; new_name
carries no extension, the original one is kept. A plan in which two items
resolve to the same target is refused as a whole -- half of it applying and
half skipping is worse than neither. Renames that only change letter case are
allowed (via a temporary name) even on a case-insensitive filesystem.
"""
import os, sys, json, argparse, datetime
from collections import defaultdict
from pathlib import Path

# Shared helpers live in lib/ at the repo root; walk up so this works from any cwd.
_LIB = next((p / "lib" for p in Path(__file__).resolve().parents if (p / "lib" / "skill_config.py").is_file()), None)
if _LIB is None:
    raise SystemExit("cannot find the repo-root lib/ directory; run from a clone of the repo, not a copied file")
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))
from skill_config import data_root  # noqa: E402

ILLEGAL = str.maketrans({c: None for c in '/:*?"<>|$'})


def logdir():
    """Undo logs: <data_root>/name-videos/logs (gitignored)."""
    return str(data_root() / "name-videos" / "logs")

def safe(name):
    return " ".join(name.translate(ILLEGAL).split()).strip(" .")

def _case_only(src, dst):
    """True when src and dst name the same file and differ only by letter case."""
    if os.path.abspath(src).lower() != os.path.abspath(dst).lower():
        return False
    return os.path.exists(dst) and os.path.samefile(src, dst)

def _rename(src, dst):
    if _case_only(src, dst):
        # A direct rename is a no-op on a case-insensitive volume; go via a temp name.
        tmp = dst + ".case-tmp"
        os.rename(src, tmp)
        os.rename(tmp, dst)
    else:
        os.rename(src, dst)

def build(plan):
    """-> (moves, problems, collisions). Collisions non-empty means refuse the plan."""
    moves, problems = [], []
    for item in plan:
        src = item["path"]
        ext = os.path.splitext(src)[1]
        dst = os.path.join(os.path.dirname(src), safe(item["new_name"]) + ext)
        if not os.path.exists(src):          problems.append((src, "source missing")); continue
        if os.path.abspath(src) == os.path.abspath(dst): problems.append((src, "no change")); continue
        if os.path.exists(dst) and not _case_only(src, dst):
            problems.append((src, f"target exists: {os.path.basename(dst)}")); continue
        moves.append((src, dst, item.get("confidence", "?")))
    # Compare case-insensitively: the shared drives this runs against are, and a
    # plan that is only distinct by case would silently lose a file.
    by_target = defaultdict(list)
    for src, dst, _ in moves:
        by_target[os.path.abspath(dst).lower()].append((dst, src))
    collisions = [(os.path.basename(pairs[0][0]), [src for _, src in pairs])
                  for pairs in by_target.values() if len(pairs) > 1]
    return moves, problems, collisions

def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("plan", help="JSON plan: [{path, new_name, confidence, reason}, ...]")
    ap.add_argument("--apply", action="store_true", help="execute the renames and write an undo log")
    a = ap.parse_args()
    with open(a.plan) as fh:
        plan = json.load(fh)
    moves, problems, collisions = build(plan)

    if collisions:
        print("REFUSED: the plan gives more than one file the same name.")
        for target, srcs in collisions:
            print(f"  {target}")
            for s in srcs:
                print(f"      <- {os.path.basename(s)}")
        print("Fix the plan and re-run; nothing was renamed.")
        sys.exit(1)

    for src, dst, conf in moves:
        print(f"[{conf:6}] {os.path.basename(src)}\n      -> {os.path.basename(dst)}")
    for src, why in problems:
        print(f"[SKIP  ] {os.path.basename(src)}  ({why})")

    if not a.apply:
        print(f"\nDRY RUN. {len(moves)} would be renamed, {len(problems)} skipped."
              f"\nRe-run with --apply to execute.")
        return

    os.makedirs(logdir(), exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    log = os.path.join(logdir(), f"rename-{stamp}.json")
    done = []
    for src, dst, _ in moves:
        try:
            _rename(src, dst); done.append({"from": src, "to": dst})
        except OSError as e:
            print(f"FAILED {os.path.basename(src)}: {e}")
    with open(log, "w") as fh:
        json.dump(done, fh, indent=1)
    print(f"\nRenamed {len(done)}. Undo log: {log}")
    print(f"Undo with: python3 {os.path.join(os.path.dirname(os.path.abspath(__file__)),'undo.py')} {log}")

if __name__ == "__main__": main()
