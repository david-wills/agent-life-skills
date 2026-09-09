#!/usr/bin/env python3
"""Revert a rename log produced by rename.py --apply.

Usage:
  undo.py <data_root>/name-videos/logs/rename-<stamp>.json             # revert, newest move first
  undo.py <data_root>/name-videos/logs/rename-<stamp>.json --dry-run   # show what would be reverted

A move is reverted only when its target still exists and its original name is
free; anything else is reported and left alone.
"""
import os, sys, json, argparse

def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("log", help="undo log written by rename.py --apply")
    ap.add_argument("--dry-run", action="store_true", help="print the reverts without touching any file")
    a = ap.parse_args()
    with open(a.log) as fh:
        log = json.load(fh)
    n = 0
    for m in reversed(log):
        src, dst = m["to"], m["from"]
        if not os.path.exists(src):
            print(f"skip   {os.path.basename(src)}  (no longer exists)"); continue
        if os.path.exists(dst) and not (os.path.samefile(src, dst)):
            print(f"skip   {os.path.basename(src)}  (original name is taken: {os.path.basename(dst)})"); continue
        if a.dry_run:
            print(f"would revert {os.path.basename(src)} -> {os.path.basename(dst)}"); n += 1; continue
        os.rename(src, dst); n += 1
        print(f"reverted {os.path.basename(src)} -> {os.path.basename(dst)}")
    print(f"{n}/{len(log)} {'would be ' if a.dry_run else ''}reverted")

if __name__ == "__main__":
    main()
