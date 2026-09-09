#!/usr/bin/env python3
"""Apply a rename plan. Dry-run by default; writes an undo log when applied."""
import os, sys, json, argparse, datetime, pathlib

ILLEGAL = str.maketrans({c: None for c in '/:*?"<>|$'})
LOGDIR  = str(pathlib.Path(__file__).resolve().parent.parent / "_state" / "logs")

def safe(name):
    return " ".join(name.translate(ILLEGAL).split()).strip(" .")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("plan")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    plan = json.load(open(a.plan))
    moves, problems = [], []
    for item in plan:
        src = item["path"]
        ext = os.path.splitext(src)[1]
        dst = os.path.join(os.path.dirname(src), safe(item["new_name"]) + ext)
        if not os.path.exists(src):          problems.append((src, "source missing")); continue
        if os.path.abspath(src) == os.path.abspath(dst): problems.append((src, "no change")); continue
        if os.path.exists(dst):              problems.append((src, f"target exists: {os.path.basename(dst)}")); continue
        moves.append((src, dst, item.get("confidence", "?")))

    for src, dst, conf in moves:
        print(f"[{conf:6}] {os.path.basename(src)}\n      -> {os.path.basename(dst)}")
    for src, why in problems:
        print(f"[SKIP  ] {os.path.basename(src)}  ({why})")

    if not a.apply:
        print(f"\nDRY RUN. {len(moves)} would be renamed, {len(problems)} skipped."
              f"\nRe-run with --apply to execute.")
        return

    os.makedirs(LOGDIR, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    log = os.path.join(LOGDIR, f"rename-{stamp}.json")
    done = []
    for src, dst, _ in moves:
        try:
            os.rename(src, dst); done.append({"from": src, "to": dst})
        except OSError as e:
            print(f"FAILED {os.path.basename(src)}: {e}")
    json.dump(done, open(log, "w"), indent=1)
    print(f"\nRenamed {len(done)}. Undo log: {log}")
    print(f"Undo with: python3 {os.path.join(os.path.dirname(os.path.abspath(__file__)),'undo.py')} {log}")

if __name__ == "__main__": main()
