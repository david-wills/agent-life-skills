#!/usr/bin/env python3
"""Half-hourly heartbeat: ingest new transcripts and snapshot quota. No model calls."""
import os
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lib
import collect
import collect_codex
import import_aggregates
import quota
import report

con = lib.connect()
lib.init(con)
collect.ingest_all(con)
collect.attribute(con)
try:
    collect_codex.ingest(con, verbose=False)
except Exception as e:
    print(f"{time.strftime('%F %T')} codex ingest failed: {e}", file=sys.stderr)
# Keep the copy of the exporter on the shared volume current, so the other
# machine never runs a stale version of it.
try:
    inbox = lib.cfg("token_tracker.inbox", None)
    if inbox and os.path.isdir(inbox):
        src = os.path.join(os.path.dirname(os.path.abspath(__file__)), "export_aggregates.py")
        dst = os.path.join(inbox, "export_aggregates.py")
        if open(src, "rb").read() != (open(dst, "rb").read() if os.path.exists(dst) else b""):
            shutil.copyfile(src, dst)
            print(f"{time.strftime('%F %T')} refreshed exporter on shared volume")
except Exception as e:
    print(f"{time.strftime('%F %T')} exporter refresh failed: {e}", file=sys.stderr)

# counts-only exports dropped by other machines on the shared volume
try:
    n = import_aggregates.watch(con, verbose=False)
    if n:
        print(f"{time.strftime('%F %T')} imported {n} export(s) from inbox")
except Exception as e:
    print(f"{time.strftime('%F %T')} inbox watch failed: {e}", file=sys.stderr)

try:
    quota.record(con, quota.fetch())
    hit = quota.detect_reset(con)
    if hit:
        when = time.strftime("%-I:%M%p", time.localtime(hit["prev_ts"] / 1000))
        now = time.strftime("%-I:%M%p", time.localtime(hit["ts"] / 1000))
        report.post(
            f"⚠︎ **Claude weekly quota was reset out-of-band**\n"
            f"· weekly utilization fell **{hit['from_pct']:.0f}% → {hit['to_pct']:.0f}%** "
            f"between {when} and {now}, with the reset time unchanged "
            f"({hit['resets_at'][:10]}).\n"
            f"· That is not a window roll — it looks like a credit or plan "
            f"adjustment on Anthropic's side.\n"
            f"· Quota percentages are uncalibrated until usage rebuilds.")
        quota.mark_alerted(con, "quota-reset", hit["ts"])
        print(f"{time.strftime('%F %T')} posted quota-reset alert")
except Exception as e:
    print(f"{time.strftime('%F %T')} quota sample failed: {e}", file=sys.stderr)
n = con.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
print(f"{time.strftime('%F %T')} ok, {n} messages tracked")
con.close()
