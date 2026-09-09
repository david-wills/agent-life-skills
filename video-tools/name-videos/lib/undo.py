#!/usr/bin/env python3
"""Revert a rename log produced by rename.py --apply."""
import os, sys, json
log = json.load(open(sys.argv[1]))
n = 0
for m in reversed(log):
    if os.path.exists(m["to"]) and not os.path.exists(m["from"]):
        os.rename(m["to"], m["from"]); n += 1
        print(f"reverted {os.path.basename(m['to'])} -> {os.path.basename(m['from'])}")
print(f"{n}/{len(log)} reverted")
