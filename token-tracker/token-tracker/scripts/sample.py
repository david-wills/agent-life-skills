#!/usr/bin/env python3
"""Half-hourly heartbeat: ingest new transcripts and snapshot quota. No model calls.

This is the one script to schedule. Every 30 minutes it:
  1. ingests new Claude transcripts and attributes new sessions (collect.py)
  2. ingests Codex rollouts and their rate-limit block (collect_codex.py)
  3. if `token_tracker.inbox` is set: refreshes the copy of export_aggregates.py
     kept in that folder, then imports any counts-only export another machine
     dropped there (import_aggregates.py --watch)
  4. takes one quota sample (quota.py) and posts an alert if the weekly
     utilization fell without the window rolling

Each step is isolated: a failure is logged and the rest still run. Without a
Claude Code subscription login step 4 logs one line and the rest is unaffected.

Usage:
  sample.py            # run one heartbeat
"""
import argparse
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


def _log(msg, err=False):
    print(f"{time.strftime('%F %T')} {msg}", file=sys.stderr if err else sys.stdout)


def refresh_exporter(inbox):
    """Keep the exporter in the shared inbox current so other machines never run a stale copy."""
    src = os.path.join(os.path.dirname(os.path.abspath(__file__)), "export_aggregates.py")
    dst = os.path.join(inbox, "export_aggregates.py")
    with open(src, "rb") as fh:
        want = fh.read()
    have = b""
    if os.path.exists(dst):
        with open(dst, "rb") as fh:
            have = fh.read()
    if want != have:
        shutil.copyfile(src, dst)
        _log("refreshed exporter in the inbox folder")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.parse_args()

    con = lib.connect()
    lib.init(con)
    collect.ingest(con)
    collect.attribute(con)
    try:
        collect_codex.ingest(con, verbose=False)
    except Exception as e:
        _log(f"codex ingest failed: {e}", err=True)

    # Other machines drop counts-only exports here; optional.
    inbox = lib.cfg("token_tracker.inbox", None)
    if inbox and os.path.isdir(os.path.expanduser(inbox)):
        inbox = os.path.expanduser(inbox)
        try:
            refresh_exporter(inbox)
        except Exception as e:
            _log(f"exporter refresh failed: {e}", err=True)
        try:
            n = import_aggregates.watch(con, folder=inbox, verbose=False)
            if n:
                _log(f"imported {n} export(s) from inbox")
        except Exception as e:
            _log(f"inbox watch failed: {e}", err=True)

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
            _log("posted quota-reset alert")
    except RuntimeError as e:                 # no subscription login: counts still work
        _log(f"quota sample skipped: {e}", err=True)
    except Exception as e:
        _log(f"quota sample failed: {e}", err=True)
    n = con.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    _log(f"ok, {n} messages tracked")
    con.close()


if __name__ == "__main__":
    main()
